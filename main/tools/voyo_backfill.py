"""BACKFILL - record the sessions of the current / last Grand Prix that are not on the disk yet, from VOYO's
recordings, ONE AT A TIME (turned on and off on the /disk page).

It is not a second recorder: the server VOYO player (tools/voyo_server_player.py) runs it in its own loop,
with the same SessionRecorder (tools/voyo_session.py: find the session's recording on the event page,
verify it plays, record it - a finished recording from 0:00 to its end), the same screen recorder and the
same packages on the disk (key ``<kind>-<episode id>-<yyyymmdd of the scheduled start>``, exactly the key the
live recording would have used). The live recording always comes first:

* nothing is started while a live session window is open, nor when the next one would open before this
  recording is over (EXPECTED_S); a live window that opens anyway stops it (the live session is recorded,
  the stopped one is said and can be done again);
* the sessions are done in ORDER, never two at once; one is started only when the one before is finished,
  saved and checked;
* a session already on the disk is not recorded again and never overwritten: COMPLETE -> "completed
  (already on the disk)", otherwise "skipped" (a partial recording is kept as it is - delete it on /disk to
  record it again);
* not on VOYO / not held (cancelled, no such session on this weekend) / an error: said, and on to the next.

"Completed" only after the dashboard server closed the package and its own completeness check said
COMPLETE (server/voyo_recording.capture_check) - never on the player's word alone.

The job (which Grand Prix, every session's status, reason, times, attempts) is kept in the player's state
file, so a restart neither forgets it nor shows a false state: a session that was being recorded when the
player stopped is "failed: interrupted" (its partial recording stays on the disk, not counted as done).
"""
from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from typing import Callable, Optional

from server import voyo_episodes as ve

ORDER = ("practice1", "practice2", "practice3", "sprint_qualifying", "sprint", "qualifying", "race")
# how long a session's recording usually runs (the episode with VOYO's build-up): a live session must not
# begin before it is over - else it waits
EXPECTED_S = {"practice1": 80 * 60, "practice2": 80 * 60, "practice3": 80 * 60, "sprint_qualifying": 70 * 60,
              "sprint": 70 * 60, "qualifying": 90 * 60, "race": 150 * 60}
LIMIT_S = {k: 2 * v + 1800 for k, v in EXPECTED_S.items()}        # never longer than this (a stuck player)
FIND_S = 300                     # the event page must give an answer (found / not found) within this
MAX_FAILS = 2                    # the recording failed this often (wrong video, not playable): given up
VERIFY_S = 120                   # the server's completeness check must answer within this after the close
DONE_STATES = ("completed", "skipped", "unavailable", "failed")


def iso(t: Optional[float]) -> Optional[str]:
    return datetime.fromtimestamp(t, timezone.utc).isoformat(timespec="seconds") if t else None


def pick_meeting(windows: list, now: float) -> Optional[str]:
    """The Grand Prix to backfill: the latest one whose first session has begun (this weekend or the last)."""
    first: dict = {}
    for w in windows:
        try:
            t = datetime.fromisoformat(w["start"]).timestamp()
        except (KeyError, TypeError, ValueError):
            continue
        first[w.get("meeting")] = min(first.get(w.get("meeting"), t), t)
    begun = [(t, m) for m, t in first.items() if m and t <= now]
    return max(begun)[1] if begun else None


def new_job(run: str, meeting: str, windows: list, now: float) -> dict:
    sessions = []
    for kind in ORDER:
        w = next((w for w in windows if w.get("meeting") == meeting and w.get("kind") == kind), None)
        sessions.append({"kind": kind, "label": ve.KIND_LABELS[kind], "session_name": (w or {}).get("session_name"),
                         "start": (w or {}).get("start"), "open_until": (w or {}).get("open_until"),
                         "status": "pending", "reason": "", "attempts": 0, "started_at": None, "finished_at": None,
                         "key": None, "episode_id": None})
    return {"run": run, "meeting": meeting, "state": "running", "created_at": iso(now), "updated_at": iso(now),
            "finished_at": None, "current": None, "note": "", "sessions": sessions}


class Backfill:
    """One backfill job driven step by step from the player's loop. ``player``: open_session(target, url),
    session_tick(), close_session(why), ``session`` (the SessionRecorder), event_url(). ``verify(key)``: the
    package on the disk as the dashboard server sees it - None (no such package) or {"status", "check",
    "issues"}; raises OSError when the server cannot be asked."""

    def __init__(self, player, all_windows: Callable[[float], list], verify: Callable[[str], Optional[dict]],
                 load: Callable[[], Optional[dict]], save: Callable[[Optional[dict]], None],
                 target_cls=None, log: Callable[[str], None] = print) -> None:
        if target_cls is None:
            from tools.voyo_session import Target as target_cls  # noqa: N813
        self.player, self.all_windows, self.verify, self._save, self.log = player, all_windows, verify, save, log
        self.Target = target_cls
        self.ctl = {"enabled": False, "run": None, "stop": 0}
        self.got_ctl = False                     # nothing is decided before the server said what it wants
        self.active: Optional[str] = None        # the kind being done now (the player's session is ours)
        self._session = None                     # the SessionRecorder it opened
        self._stop_base = 0
        self._fails = 0
        self._in_failed = False
        self._verify_until = 0.0
        self._stopped_why = ""
        self._saved = None
        job = load()
        self.job = job if isinstance(job, dict) and isinstance(job.get("sessions"), list) else None
        if self.job is not None:                 # a restart: what was being done is not done
            for s in self.job["sessions"]:
                if s.get("status") in ("recording", "verifying"):
                    s.update(status="failed", finished_at=iso(time.time()),
                             reason="interrupted: the server VOYO player stopped while it was being recorded "
                                    "(a partial recording may be on the disk; it is not counted as done)")
            self.job["current"] = None
            self.save()

    # ------------------------------------------------------------------ control (from the /disk page)
    def control(self, ctl: Optional[dict]) -> None:
        if not isinstance(ctl, dict):
            return
        run = ctl.get("run")
        self.ctl = {"enabled": bool(ctl.get("enabled")), "run": str(run)[:40] if run else None,
                    "stop": int(ctl.get("stop") or 0)}
        self.got_ctl = True

    def job_note_unsupported(self) -> None:
        """The player does not find recordings itself (episode_select = "off" / when != "schedule")."""
        self.unsupported = True

    def busy(self) -> bool:
        """A backfill session holds the player (its recording, or the check after it)."""
        return self.active is not None

    def owns(self, session) -> bool:
        """``session`` is the backfill's own (not a live one opened while its check was still going on)."""
        return session is not None and self.active is not None and session is self._session

    # ------------------------------------------------------------------ helpers
    def _s(self, kind: Optional[str]) -> Optional[dict]:
        return next((s for s in (self.job or {}).get("sessions", []) if s["kind"] == kind), None)

    def _mark(self, s: dict, status: str, reason: str = "", now: Optional[float] = None) -> None:
        s["status"], s["reason"] = status, reason[:300]
        if status in DONE_STATES:
            s["finished_at"] = iso(now or time.time())
        self.log(f"BACKFILL {self.job['meeting']} {s['label']}: {status.upper()}" + (f" - {reason}" if reason else ""))

    def save(self) -> None:
        if self.job is not None:
            snap = json.dumps(self.job, sort_keys=True)
            if snap == self._saved:
                return
            self._saved = snap
            self.job["updated_at"] = iso(time.time())
        try:
            self._save(self.job)
        except OSError as exc:
            self.log(f"backfill: could not save its state ({exc})")

    def report(self) -> Optional[dict]:
        if self.job is None:
            if getattr(self, "unsupported", False) and self.ctl["enabled"]:
                return {"run": self.ctl["run"], "state": "unsupported", "sessions": [], "done": 0, "total": 0,
                        "enabled": True, "note": "backfill needs the player to find recordings itself "
                                                 '([voyo.server_player] episode_select = "auto", when = "schedule")'}
            return None
        j = dict(self.job)
        j["current"] = self.active and ve.KIND_LABELS.get(self.active)
        j["done"] = sum(1 for s in j["sessions"] if s["status"] in DONE_STATES)
        j["total"] = len(j["sessions"])
        j["enabled"] = self.ctl["enabled"] and self.ctl["run"] == j.get("run")
        return j

    # ------------------------------------------------------------------ one step
    def step(self, now: float, live_window: Optional[dict], upcoming: list) -> None:
        ctl, job = self.ctl, self.job
        if not self.got_ctl and self.active is None:
            return
        if ctl["enabled"] and ctl["run"] and self.active is None and (job is None or job.get("run") != ctl["run"]):
            wins = self.all_windows(now)
            meeting = pick_meeting(wins, now)
            if meeting is None:
                self.job = {"run": ctl["run"], "meeting": None, "state": "finished", "created_at": iso(now),
                            "updated_at": iso(now), "finished_at": iso(now), "current": None, "sessions": [],
                            "note": "no Grand Prix found in the F1 schedule (not reachable?) - nothing to do"}
            else:
                self.job = new_job(ctl["run"], meeting, wins, now)
                self.log(f"BACKFILL started for {meeting}")
            self._stop_base = ctl["stop"]
            job = self.job
            self.save()
        if job is None:
            return
        if self.active is not None:
            self._drive(now, live_window)
            self.save()
            return
        if job["state"] == "finished":
            return
        if not ctl["enabled"] or ctl["run"] != job["run"]:
            job["state"], job["note"] = "paused", "turned off on /disk - no session is started"
            self.save()
            return
        job["state"] = "running"
        if live_window:
            job["note"] = "waiting: a live session is on (" + " ".join(
                x for x in (live_window.get("meeting"), live_window.get("session_name")) if x) + ")"
            self.save()
            return
        url = self.player.event_url()
        if not url:
            job["note"] = "waiting: no VOYO event page set (/disk VOYO / event_url)"
            self.save()
            return
        for s in job["sessions"]:
            if s["status"] != "pending":
                continue
            if not s.get("start"):
                self._mark(s, "skipped", "not in this weekend's F1 schedule", now)
                continue
            start = datetime.fromisoformat(s["start"]).timestamp()
            if (s.get("open_until") or start) > now:
                self._mark(s, "skipped", "not over yet - the live recording records it", now)
                continue
            nxt = min((w["open_from"] for w in upcoming if w.get("open_from", 0) > now), default=None)
            if nxt is not None and nxt < now + EXPECTED_S[s["kind"]]:
                job["note"] = (f"waiting: a live session begins at {iso(nxt)} - {s['label']} would not be over "
                               "before it")
                self.save()
                return
            self._start(s, start, url, now)
            self.save()
            return
        job.update(state="finished", finished_at=iso(now), current=None)
        n = {st: sum(1 for x in job["sessions"] if x["status"] == st) for st in DONE_STATES}
        job["note"] = ", ".join(f"{v} {k}" for k, v in n.items() if v) or "nothing to do"
        self.log(f"BACKFILL {job['meeting']} finished: {job['note']}")
        self.save()

    def _start(self, s: dict, start: float, url: str, now: float) -> None:
        s.update(status="recording", reason="finding it on the VOYO event page", attempts=s["attempts"] + 1,
                 started_at=iso(now), finished_at=None, key=None, episode_id=None)
        s.pop("checked", None)
        self.active, self._fails, self._in_failed, self._stopped_why = s["kind"], 0, False, ""
        self._stop_base = self.ctl["stop"]
        self.job["note"] = ""
        self.log(f"BACKFILL {self.job['meeting']} {s['label']}: looking for it on VOYO")
        self.player.open_session(self.Target(s["kind"], self.job["meeting"], s.get("session_name"), start,
                                             now + LIMIT_S[s["kind"]]), url)
        self._session = self.player.session

    def _close(self, why: str) -> None:
        try:
            self.player.close_session(why)
        except Exception as exc:  # noqa: BLE001 - never leave the player half open
            self.log(f"backfill: closing the session failed ({exc})")

    def _end(self, s: dict, status: str, reason: str, now: float) -> None:
        self._mark(s, status, reason, now)
        self.active = None

    def _on_disk(self, s: dict, now: float, earlier: bool) -> bool:
        """The recording is on the disk already (before this backfill touched it): never recorded again. When
        the server cannot be asked, nothing is recorded either (it could be there)."""
        try:
            v = self.verify(s["key"])
        except OSError as exc:
            self._end(s, "failed", f"could not ask the dashboard server whether it is on the disk ({exc}) - can retry", now)
            return True
        if v is None:
            return False
        if v.get("check") == "COMPLETE":
            self._end(s, "completed", "already on the disk" + (" (recorded earlier)" if earlier else ""), now)
        else:
            self._end(s, "skipped", f"a recording of it is on the disk already ({v.get('check') or 'not checked'}) "
                                    "- not overwritten; delete it on /disk to record it again", now)
        return True

    def _drive(self, now: float, live_window: Optional[dict]) -> None:
        s = self._s(self.active)
        if s is None:
            self.active = None
            return
        if s["status"] == "verifying":
            self._verify(s, now)
            return
        ss = self.player.session
        if ss is None or ss is not self._session:
            self._end(s, "failed", "the recording ended unexpectedly - can retry", now)
            return
        stop = ""
        if live_window:
            stop = "stopped: a live session began (" + " ".join(
                x for x in (live_window.get("meeting"), live_window.get("session_name")) if x) + ") - the live one comes first"
        elif self.ctl["stop"] != self._stop_base:
            stop = "stopped on /disk"
        elif s.get("started_at") and now - datetime.fromisoformat(s["started_at"]).timestamp() > LIMIT_S[s["kind"]]:
            stop = f"stopped: still not over after {LIMIT_S[s['kind']] // 60} min"
        if stop:
            was = ss.state
            self._close(stop)
            if was in ("RECORDING", "DONE") or ss.recording_s > 0:
                self._stopped_why = stop
                s.update(status="verifying", reason=stop)
                self._verify_until = now + VERIFY_S
            elif live_window:
                s.update(status="pending", reason="interrupted by a live session before it was recorded - done later")
                self.active = None
            else:
                self._end(s, "failed", stop + " - can retry", now)
            return
        self.player.session_tick()
        ss = self.player.session
        if ss is None:
            self._end(s, "failed", "the recording ended unexpectedly - can retry", now)
            return
        if ss.key:
            s["key"], s["episode_id"] = ss.key, (ss.episode or {}).get("id")
        st = ss.state
        sel = ss.selection or {}
        if st in ("NOT_FOUND", "AMBIGUOUS") and sel:
            self._close("backfill: not on VOYO")
            self._end(s, "unavailable", f"not available on VOYO: {sel.get('reason') or st.lower()}; skipped", now)
            return
        if st == "FAILED":
            if not self._in_failed:
                self._in_failed = True
                self._fails += 1
            if self._fails >= MAX_FAILS:
                self._close("backfill: failed")
                self._end(s, "failed", f"{ss.problem or 'the recording could not be confirmed'} - can retry", now)
            return
        self._in_failed = False
        if st == "DISCOVERING" and not sel and now - datetime.fromisoformat(s["started_at"]).timestamp() > FIND_S:
            self._close("backfill: event page not readable")
            self._end(s, "failed", "the VOYO event page could not be read (Chrome / network / sign-in?) - can retry", now)
            return
        if st == "OPENING" and ss.key and not s.get("checked"):
            s["checked"] = True                   # before anything is played or recorded
            if self._on_disk(s, now, earlier=True):
                self._close("backfill: already on the disk")
            return
        if st in ("VERIFYING", "RECORDING"):
            s["reason"] = "recording" if st == "RECORDING" else "checking that the right video plays"
        if st == "DONE":
            why, recorded = ss.end_reason or "done", ss.recording_s
            self._close(why)
            if why.startswith("already recorded"):
                if not self._on_disk(s, now, earlier=True):
                    self._end(s, "skipped", "recorded earlier by this player but not on the disk any more - "
                                            "not recorded again", now)
                return
            if not s.get("key") or recorded <= 0:
                self._end(s, "failed", f"nothing was recorded: {why} - can retry", now)
                return
            s.update(status="verifying", reason="saved - waiting for the server's completeness check")
            self._verify_until = now + VERIFY_S

    def _verify(self, s: dict, now: float) -> None:
        """After the close: the package as the dashboard server stored and checked it."""
        try:
            v = self.verify(s["key"])
        except OSError as exc:
            v, err = None, str(exc)
        else:
            err = ""
        check = (v or {}).get("check")
        if check == "COMPLETE" and not self._stopped_why:
            self._end(s, "completed", f"recorded and checked: COMPLETE ({s['key']})", now)
            return
        if now < self._verify_until and (v is None or check is None or check == "INCOMPLETE") and not self._stopped_why:
            return                                # late segments / the check still on its way
        if self._stopped_why:
            why = f"{self._stopped_why}; a partial recording ({check or 'not checked'}) is kept on the disk"
        elif v is None:
            why = "no recording on the disk after the close" + (f" ({err})" if err else "")
        else:
            why = f"recorded but {check or 'not checked'}: " + ("; ".join(v.get("issues") or []) or "no detail")
        self._end(s, "failed", why + " - delete it on /disk to record it again", now)
