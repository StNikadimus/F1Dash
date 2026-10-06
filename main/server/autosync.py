"""VOYO AUTO SYNC - stream instances, LIVE DATA DELAY and the automatic sync state.

AUTO SYNC is not a second sync engine: the offset is the one SyncManager computes from its
anchors (median of the precise ones, outlier rejection, confidence classes - server/sync.py).
This module adds what that engine did not know:

* **Stream instances** (StreamTracker): every VOYO stream / recording the server observes gets
  its own ``stream_instance_id`` and the server wall-clock time it started. A new instance starts
  when the video (asset / media id), the recording (its length) or - for a live stream - the
  player's timeline (a reload: ``loadstart`` / ``emptied`` and the position reset) changes.
  Seeking, pausing and buffering never start one. A new instance inherits no offset and no
  stream-start mark; a reopened recording (same video, same length) is the same timeline, so its
  saved sync (anchors of that video + F1 session) is valid again.
* **LIVE DATA DELAY** (LiveDataDelay): how far behind real time the F1 data arrives - the server
  receive time minus the F1 message timestamp of every live feed message, median over 60 s,
  with its spread. Needs the server clock to be NTP-synchronised (a wrong clock shows up as a
  negative / implausible value and is flagged).
* **The state** (evaluate): SEARCHING -> CALIBRATING -> LOCKED, UNSTABLE when observations
  disagree, HOLD while VOYO is paused / buffering / seeking (never taken for drift).

Why the stream start alone is not enough: VOYO's picture cannot be read (DRM), its playlists carry
no program-date-time and its metadata no UTC (README "Is currentTime F1 time?"). The wall-clock
time a stream starts says when the server saw position 0:00 - not which F1 moment that frame
shows: that differs by the broadcaster's / CDN's latency (LIVE) or is unrelated (a recording
watched weeks later). So the stream start is the BASE anchor, F1 events matched to VOYO positions
(Event Sync, L, countdown, session clock ...) are the CALIBRATION; for a live stream the latency
measured on a calibrated stretch is learned and gives the next instance a CALIBRATING estimate.
"""
from __future__ import annotations

import math
import time
import uuid
from collections import deque
from dataclasses import asdict, dataclass
from statistics import median
from typing import Any, Optional


# ---------------------------------------------------------------------------
# LIVE DATA DELAY
# ---------------------------------------------------------------------------
class LiveDataDelay:
    """Server receive time - F1 message timestamp of the live feed messages (seconds)."""

    WINDOW_S = 60.0
    STABLE_SPREAD_S = 0.35
    MIN_SAMPLES = 20

    def __init__(self) -> None:
        self.samples: deque = deque()            # (monotonic s, delay s)

    def add(self, mono: float, delay_s: float) -> None:
        if not math.isfinite(delay_s) or abs(delay_s) > 600:
            return
        self.samples.append((mono, delay_s))
        while self.samples and mono - self.samples[0][0] > self.WINDOW_S:
            self.samples.popleft()
        while len(self.samples) > 4000:
            self.samples.popleft()

    def snapshot(self, mono: Optional[float] = None) -> dict:
        mono = time.monotonic() if mono is None else mono
        recent = [d for t, d in self.samples if mono - t <= self.WINDOW_S]
        if not recent:
            return {"state": "NO DATA", "seconds": None, "current": None, "spread": None, "samples": 0,
                    "history": [], "clockWarning": False}
        stable = median(recent)
        spread = 1.4826 * median(abs(d - stable) for d in recent)
        current = median(recent[-10:])
        last_age = mono - self.samples[-1][0]
        state = ("STALE" if last_age > 30 else "SETTLING" if len(recent) < self.MIN_SAMPLES
                 else "STABLE" if spread <= self.STABLE_SPREAD_S else "NOISY")
        # one value per 10 s for the last minute (oldest first) - the "observed" list
        history = []
        for k in range(5, -1, -1):
            seg = [d for t, d in self.samples if k * 10 <= mono - t < (k + 1) * 10]
            if seg:
                history.append(round(median(seg), 1))
        return {"state": state, "seconds": round(stable, 1), "current": round(current, 1),
                "spread": round(spread, 2), "samples": len(recent), "history": history,
                # the server clock behind F1's: a negative delay is impossible
                "clockWarning": stable < -0.5}

    def stable_seconds(self) -> Optional[float]:
        s = self.snapshot()
        return s["seconds"] if s["state"] == "STABLE" else None


# ---------------------------------------------------------------------------
# Stream instances
# ---------------------------------------------------------------------------
@dataclass
class StreamInstance:
    id: str
    asset: str
    live: bool
    duration: Optional[float]
    title: Optional[str]
    media_id: Optional[str]
    options_fp: Optional[str]
    first_seen_wall: float                 # server wall clock (epoch s) when the instance appeared
    origin_wall: Optional[float]           # server wall clock when position 0:00 played (if seen from it)
    origin_how: str
    reason: str
    session_key: Any = None                # F1 identity, attached once the session is known
    meeting: Optional[str] = None
    session_name: Optional[str] = None
    last_wall: float = 0.0
    last_pb: float = 0.0
    resumed: bool = False
    load_id: Optional[str] = None          # the VOYO page load the samples came from (probe)

    def to_json(self) -> dict:
        return asdict(self)


def sample_fingerprint(s) -> dict:
    page = s.page or {}
    dur = s.duration if s.duration is not None and math.isfinite(s.duration) and s.duration < 1e7 else None
    return {"asset": s.asset, "live": dur is None, "duration": round(dur, 1) if dur else None,
            "title": page.get("media_title") or page.get("title"), "media_id": page.get("media_id"),
            "options_fp": page.get("options_fp"), "load_id": page.get("load_id")}


class StreamTracker:
    """Which VOYO stream instance the samples belong to."""

    RESTART_JUMP_S = 30.0      # live: the position falls back by more than this at a (re)load
    RESUME_TOL_S = 10.0        # a stream continuing across a server restart

    def __init__(self, store: Optional[dict] = None) -> None:
        self.current: Optional[StreamInstance] = None
        self.store = store if store is not None else {}
        self._last_mono: Optional[float] = None
        self._last_pb: Optional[float] = None

    def _new(self, s, fp: dict, wall: float, reason: str) -> StreamInstance:
        playing = not s.paused and s.rate > 0
        origin, how = (wall - s.pb / max(s.rate, 0.01), "seen from its beginning") if playing and s.pb < 60 else \
            (None, f"joined at position {s.pb:.0f} s - its start time was not observed")
        return StreamInstance(uuid.uuid4().hex[:12], fp["asset"], fp["live"], fp["duration"], fp["title"],
                              fp["media_id"], fp["options_fp"], wall, origin, how, reason, last_wall=wall,
                              last_pb=s.pb)

    def _resume(self, s, fp: dict, wall: float) -> Optional[StreamInstance]:
        """The same stream after a server restart: a recording (same video, same length) is the same
        timeline; a live stream only if its position continued as the clock did."""
        best = None
        for d in (self.store.get("instances") or {}).values():
            if d.get("asset") != fp["asset"] or bool(d.get("live")) != fp["live"]:
                continue
            if not fp["live"]:
                if d.get("duration") and fp["duration"] and abs(d["duration"] - fp["duration"]) <= 5:
                    best = d if best is None or d.get("last_wall", 0) > best.get("last_wall", 0) else best
            else:
                expect = float(d.get("last_pb", 0)) + (wall - float(d.get("last_wall", 0)))
                if wall - float(d.get("last_wall", 0)) < 6 * 3600 and abs(s.pb - expect) <= self.RESUME_TOL_S:
                    best = d
        if best is None:
            return None
        keys = StreamInstance.__dataclass_fields__.keys()
        inst = StreamInstance(**{k: best.get(k) for k in keys if k in best})
        inst.resumed = True
        return inst

    def observe(self, s, info: dict, wall: float) -> Optional[str]:
        """Returns the reason when this sample starts a new instance (else None)."""
        fp = sample_fingerprint(s)
        cur = self.current
        reason = None
        events = {e.get("type") for e in (s.events or [])}
        if cur is None:
            res = self._resume(s, fp, wall)
            if res is not None:
                self.current = res
                self._touch(s, wall)
                return "resumed"
            reason = "first stream seen"
        elif fp["asset"] != cur.asset:
            reason = "another VOYO video / stream"
        elif fp["media_id"] and cur.media_id and fp["media_id"] != cur.media_id:
            reason = "another media id on the same page"
        elif not cur.live and fp["duration"] and cur.duration and cur.duration - fp["duration"] > 5:
            reason = "another recording (shorter) on the same page"
        elif fp["options_fp"] and cur.options_fp and fp["options_fp"] != cur.options_fp and \
                events & {"loadstart", "emptied"}:
            reason = "the player loaded different playback options"
        elif cur.live and self._reloaded(s, fp, events):
            reason = "live stream restarted (player / page reload)"
        if reason:
            self.current = self._new(s, fp, wall, reason)
            self.current.load_id = fp["load_id"]
        elif cur is not None:
            self._refine(cur, fp)
        self._touch(s, wall)
        return reason

    @staticmethod
    def _refine(cur: StreamInstance, fp: dict) -> None:
        """Metadata that arrives after the first samples (the length loads later; a live DVR
        window reports a growing length - that is live, not another recording)."""
        if fp["load_id"]:
            cur.load_id = fp["load_id"]
        for k in ("title", "media_id", "options_fp"):
            if fp[k] and not getattr(cur, k):
                setattr(cur, k, fp[k])
        if fp["duration"]:
            if cur.duration is None and not cur.reason.startswith("live") and cur.live and not cur.resumed:
                cur.live, cur.duration = False, fp["duration"]          # a recording whose length just loaded
            elif cur.duration is not None and fp["duration"] > cur.duration + 5:
                # the first known length stays the reference: a length that keeps growing past it is
                # a live stream's DVR window, not another recording
                cur.live, cur.duration = True, None

    def _reloaded(self, s, fp: dict, events: set) -> bool:
        """LIVE: the timeline of the player started again. A recording's positions are absolute
        (a reload of it is the same timeline); a seek / pause / buffering never counts."""
        cur = self.current
        if events & {"seeking", "seeked"}:
            return False
        page_reload = bool(fp["load_id"] and cur.load_id and fp["load_id"] != cur.load_id)
        reset = self._last_pb is not None and s.pb < self._last_pb - self.RESTART_JUMP_S
        return page_reload or (reset and bool(events & {"loadstart", "emptied"}))

    def _touch(self, s, wall: float) -> None:
        self._last_mono, self._last_pb = s.mono, s.pb
        if self.current is not None:
            self.current.last_wall, self.current.last_pb = wall, s.pb


# ---------------------------------------------------------------------------
# State evaluation
# ---------------------------------------------------------------------------
HOLD_STATES = ("PAUSED", "BUFFERING", "SEEKING", "STALE", "ENDED")
CONF_BASE = {"HIGH": 0.95, "MEDIUM": 0.8, "MANUAL": 0.7, "LOW": 0.3}


def evaluate(*, enabled: bool, video: bool, instance: Optional[StreamInstance], clock_state: str,
             session: Optional[dict], mapping, k_applied: Optional[float], observations: int,
             outliers: int, pending: Optional[dict], latency_note: Optional[str]) -> dict:
    """AUTO SYNC state from the existing mapping (no second calculation of the offset)."""
    m = mapping
    st, why = "SEARCHING", ""
    if not enabled:
        st, why = "OFF", "sync disabled in the configuration"
    elif instance is None or clock_state in ("NONE",):
        why = "no VOYO stream observed yet (VOYO window + clock bridge)"
    elif not video:
        why = "the VOYO playback clock is not used (fixed delay mode)"
    elif not session:
        why = "F1 session not identified"
    elif clock_state in HOLD_STATES:
        st, why = "HOLD", f"VOYO {clock_state.lower()} - not drift; the sync holds and resumes with the video"
    elif pending:
        st, why = "UNSTABLE", (f"a new observation disagrees by {pending.get('shift', 0):+.2f} s - keep old / use "
                               "new in the SYNC menu")
    elif m.offset is None:
        why = "no F1 reference yet - Event Sync / L at lights out / countdown calibrate it"
    elif observations == 0:
        if m.source == "stream-origin" and m.confidence in ("HIGH", "MEDIUM", "MANUAL"):
            st, why = "LOCKED", "saved stream origin of this video + session"
        else:
            st, why = "CALIBRATING", ("estimate only (" + (m.method or m.source) + ") - one F1 reference "
                                      "(Event Sync / L / countdown) locks it")
    elif m.confidence == "LOW" or (outliers and outliers >= observations):
        st, why = "UNSTABLE", m.reason
    elif k_applied is not None and abs(m.offset - k_applied) > 0.5:
        st, why = "CALIBRATING", f"moving to the new offset ({m.offset - k_applied:+.2f} s left)"
    else:
        st, why = "LOCKED", m.reason
    conf = 0.0
    if st == "LOCKED":
        spread = m.deviation or 0.0
        conf = CONF_BASE.get(m.confidence, 0.5) * (1 - min(spread / 2.0, 0.5))
    elif st == "CALIBRATING":
        conf = 0.35 if observations else 0.2
    elif st in ("UNSTABLE", "HOLD"):
        conf = 0.2 if st == "UNSTABLE" else 0.0
    out = {"state": st, "reason": why, "confidence": round(conf, 2), "observations": observations,
           "outliers": outliers, "latencyNote": latency_note}
    return out
