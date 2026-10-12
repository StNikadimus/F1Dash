"""BACKFILL switch of the /disk page (the server side). The work itself is done by the server VOYO player
(tools/voyo_backfill.py, in its own loop - the one Chrome, the one recorder): this keeps what /disk wants
and what the player last said, in ``<data>/backfill.json``, so a restart of either side shows the truth.

    wanted   {"enabled": bool, "run": <id of the job>, "stop": <counter of STOP NOW presses>}
    job      the player's last report (Grand Prix, every session's status / reason / times / attempts)

The player gets ``wanted`` with every heartbeat answer and reports its job with every heartbeat. A run is
one job: turning it on again while it runs (or is paused) continues the same run - never a second worker;
after a finished run, ON starts a new one (sessions already on the disk are then just "completed")."""
from __future__ import annotations

import json
import logging
import secrets
import threading
import time
from pathlib import Path
from typing import Optional

log = logging.getLogger("backfill")
STATUSES = ("pending", "recording", "verifying", "completed", "skipped", "unavailable", "failed")


class BackfillControl:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._lock = threading.Lock()
        self.wanted = {"enabled": False, "run": None, "stop": 0}
        self.job: Optional[dict] = None
        self.job_at: Optional[float] = None               # wall time of the player's last report
        try:
            d = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(d, dict):
                w = d.get("wanted") or {}
                self.wanted = {"enabled": bool(w.get("enabled")), "run": w.get("run") or None,
                               "stop": int(w.get("stop") or 0)}
                self.job = d.get("job") if isinstance(d.get("job"), dict) else None
                self.job_at = d.get("job_at")
        except (OSError, ValueError, TypeError):
            pass

    def _save(self) -> None:
        tmp = self.path.with_suffix(".tmp")
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp.write_text(json.dumps({"wanted": self.wanted, "job": self.job, "job_at": self.job_at}, indent=1),
                           encoding="utf-8")
            tmp.replace(self.path)
        except OSError as exc:
            log.warning("backfill state not saved: %s", exc)

    # ------------------------------------------------------------------ the /disk page
    def set(self, enabled: bool) -> dict:
        with self._lock:
            if enabled and not self.wanted["enabled"]:
                job = self.job or {}
                if not self.wanted["run"] or (job.get("run") == self.wanted["run"] and job.get("state") == "finished"):
                    self.wanted["run"] = time.strftime("%Y%m%d-%H%M%S") + "-" + secrets.token_hex(2)   # a new job
                log.info("backfill turned ON on the /disk page (run %s)", self.wanted["run"])
            elif not enabled and self.wanted["enabled"]:
                log.info("backfill turned OFF on the /disk page (run %s)", self.wanted["run"])
            self.wanted["enabled"] = bool(enabled)
            self._save()
            return dict(self.wanted)

    def stop_now(self) -> dict:
        with self._lock:
            self.wanted["stop"] += 1
            self.wanted["enabled"] = False
            log.info("backfill STOP NOW on the /disk page (run %s)", self.wanted["run"])
            self._save()
            return dict(self.wanted)

    # ------------------------------------------------------------------ the player
    def control(self) -> dict:
        return dict(self.wanted)

    def report(self, job) -> None:
        """The player's job, from its heartbeat. Only the current run's report is kept."""
        if not isinstance(job, dict) or not job.get("run") or job.get("run") != self.wanted["run"]:
            return
        clean = _clean(job)
        with self._lock:
            changed = clean != self.job
            self.job, self.job_at = clean, time.time()
            if clean.get("state") == "finished" and self.wanted["enabled"]:
                self.wanted["enabled"] = False                     # done: the switch goes back to OFF
                log.info("backfill run %s finished: %s", clean.get("run"), clean.get("note") or "")
                changed = True
            if changed:
                self._save()

    def public(self, player_age: Optional[float]) -> dict:
        """For /disk: the switch, the state in words, the current session, progress and every session."""
        job = self.job if self.job and self.job.get("run") == self.wanted["run"] else None
        alive = player_age is not None and player_age < 90
        st, cur = (job or {}).get("state"), (job or {}).get("current")
        if not self.wanted["enabled"]:
            state = "stopping" if cur and alive else "finished" if st == "finished" else "disabled"
        elif not alive:
            state = "waiting for the player"
        elif job is None:
            state = "starting"
        else:
            state = "processing" if cur else "finished" if st == "finished" else \
                "unsupported" if st == "unsupported" else "enabled"
        return {"enabled": self.wanted["enabled"], "run": self.wanted["run"], "state": state,
                "player_alive": alive, "job": job,
                "job_age_s": None if self.job_at is None else round(time.time() - float(self.job_at), 1)}


def _clean(job: dict) -> dict:
    """Only the fields /disk shows, short and typed (it comes from another process)."""
    def s(v, n=300):
        return None if v is None else str(v)[:n]
    sessions = []
    for x in (job.get("sessions") or [])[:12]:
        if not isinstance(x, dict):
            continue
        sessions.append({"kind": s(x.get("kind"), 24), "label": s(x.get("label"), 40),
                         "session_name": s(x.get("session_name"), 60), "start": s(x.get("start"), 40),
                         "status": x.get("status") if x.get("status") in STATUSES else "pending",
                         "reason": s(x.get("reason")) or "", "attempts": int(x.get("attempts") or 0),
                         "started_at": s(x.get("started_at"), 40), "finished_at": s(x.get("finished_at"), 40),
                         "key": s(x.get("key"), 64), "episode_id": s(x.get("episode_id"), 16)})
    return {"run": s(job.get("run"), 40), "meeting": s(job.get("meeting"), 80), "state": s(job.get("state"), 20),
            "current": s(job.get("current"), 40), "note": s(job.get("note")) or "", "created_at": s(job.get("created_at"), 40),
            "updated_at": s(job.get("updated_at"), 40), "finished_at": s(job.get("finished_at"), 40),
            "done": int(job.get("done") or 0), "total": int(job.get("total") or len(sessions)), "sessions": sessions}
