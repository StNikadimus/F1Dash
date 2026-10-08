"""Activity log of the server - what it is doing and what happened (the /disk page).

A JSON-lines file (``<data>/logs/activity.jsonl``) fed by the server's own log records: everything
from the recording / mode / player side, warnings and errors from the rest. Entries older than
``keep_hours`` (48 h) are deleted - the file is rewritten without them every few minutes, so it
never grows beyond two days.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from pathlib import Path
from typing import Optional

# loggers whose INFO lines are activity (the rest only from WARNING up)
ACTIVITY_LOGGERS = ("voyo-rec", "voyo-player", "mode", "disk", "recorder", "app", "security")
PRUNE_EVERY_S = 600.0


class ActivityLog:
    def __init__(self, path: Path, keep_hours: float = 48.0, clock=time.time) -> None:
        self.path = Path(path)
        self.keep_s = float(keep_hours) * 3600
        self.now = clock
        self._lock = threading.Lock()
        self._last_prune = 0.0
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.prune()

    def add(self, text: str, level: str = "INFO", source: str = "server", **extra) -> None:
        rec = {"t": round(self.now(), 3), "level": level, "source": source, "text": str(text)[:1000], **extra}
        line = json.dumps(rec, separators=(",", ":"), ensure_ascii=False, default=str) + "\n"
        with self._lock:
            try:
                with open(self.path, "a", encoding="utf-8") as fh:
                    fh.write(line)
            except OSError:
                return
        if time.monotonic() - self._last_prune > PRUNE_EVERY_S:
            self.prune()

    def _read(self) -> list[dict]:
        out = []
        try:
            with open(self.path, encoding="utf-8") as fh:
                for line in fh:
                    try:
                        out.append(json.loads(line))
                    except ValueError:
                        continue
        except OSError:
            pass
        return out

    def prune(self) -> int:
        """Delete entries older than keep_hours (rewrites the file). Returns how many were removed."""
        self._last_prune = time.monotonic()
        cutoff = self.now() - self.keep_s
        with self._lock:
            rows = self._read()
            keep = [r for r in rows if float(r.get("t") or 0) >= cutoff]
            if len(keep) == len(rows):
                return 0
            tmp = self.path.with_name(self.path.name + ".tmp")
            try:
                with open(tmp, "w", encoding="utf-8") as fh:
                    for r in keep:
                        fh.write(json.dumps(r, separators=(",", ":"), ensure_ascii=False, default=str) + "\n")
                os.replace(tmp, self.path)
            except OSError:
                return 0
            return len(rows) - len(keep)

    def entries(self, limit: int = 500, min_level: str = "INFO") -> list[dict]:
        """Newest first, only the last keep_hours."""
        order = {"DEBUG": 0, "INFO": 1, "WARNING": 2, "ERROR": 3, "CRITICAL": 4}
        floor = order.get(min_level.upper(), 1)
        cutoff = self.now() - self.keep_s
        with self._lock:
            rows = self._read()
        rows = [r for r in rows if float(r.get("t") or 0) >= cutoff and order.get(r.get("level"), 1) >= floor]
        return rows[::-1][:max(1, int(limit))]


class ActivityHandler(logging.Handler):
    """Feeds the activity log from the normal Python logging."""

    def __init__(self, activity: ActivityLog) -> None:
        super().__init__(logging.INFO)
        self.activity = activity

    def emit(self, record: logging.LogRecord) -> None:
        if record.levelno < logging.WARNING and not record.name.startswith(ACTIVITY_LOGGERS):
            return
        try:
            self.activity.add(record.getMessage(), record.levelname, record.name)
        except Exception:  # noqa: BLE001 - logging must never fail
            pass


def install(path: Path, keep_hours: float = 48.0) -> ActivityLog:
    act = ActivityLog(path, keep_hours)
    root = logging.getLogger()
    for h in list(root.handlers):
        if isinstance(h, ActivityHandler):
            root.removeHandler(h)
    root.addHandler(ActivityHandler(act))
    return act


def fmt_age(seconds: Optional[float]) -> str:
    if seconds is None:
        return "-"
    s = int(seconds)
    if s < 90:
        return f"{s} s"
    if s < 5400:
        return f"{s // 60} min"
    return f"{s // 3600} h {s % 3600 // 60:02d} min"
