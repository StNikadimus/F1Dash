"""Records raw feed messages to gzip JSON-lines files for later replay."""
from __future__ import annotations

import gzip
import json
import logging
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

log = logging.getLogger("recorder")


class Recorder:
    def __init__(self, directory: Path) -> None:
        self.dir = directory
        self.dir.mkdir(parents=True, exist_ok=True)
        self._fh = None
        self._session: Optional[str] = None
        self._pending: list[str] = []
        self._last_flush = time.monotonic()

    def set_session(self, path: Optional[str], name: str) -> None:
        if not path or path == self._session:
            return
        self.close()
        self._session = path
        safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", path.strip("/").replace("/", "__"))
        file = self.dir / f"{safe}.jsonl.gz"
        self._fh = gzip.open(file, "at", encoding="utf-8")
        log.info("Recording session '%s' to %s", name, file)
        for line in self._pending:
            self._fh.write(line)
        self._pending.clear()

    def write(self, topic: str, data: Any, ts: Optional[datetime], snapshot: bool) -> None:
        rec = {"t": (ts or datetime.now(timezone.utc)).isoformat(), "topic": topic, "data": data}
        if snapshot:
            rec["snap"] = True
        line = json.dumps(rec, separators=(",", ":")) + "\n"
        if self._fh is None:
            if len(self._pending) < 20000:
                self._pending.append(line)
            return
        self._fh.write(line)
        now = time.monotonic()
        if now - self._last_flush > 5:
            self._fh.flush()
            self._last_flush = now

    def close(self) -> None:
        if self._fh is not None:
            try:
                self._fh.close()
            except Exception:  # noqa: BLE001
                log.exception("Error closing recording")
            self._fh = None
