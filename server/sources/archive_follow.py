"""Public-archive follower for car positions / telemetry (no F1 TV token).

Background (verified in the source of other open-source clients, see README
section "Car positions without F1 TV"): on the live SignalR socket F1 accepts
a ``Subscribe`` for ``Position.z`` / ``CarData.z`` from anonymous clients but
never sends those topics. The same data is published without authentication
in the static archive::

    https://livetiming.formula1.com/static/<SessionInfo.Path>Position.z.jsonStream
    https://livetiming.formula1.com/static/<SessionInfo.Path>CarData.z.jsonStream

After a session this is certain (replay mode uses it). *During* a session
``SessionInfo.ArchiveStatus`` reports "Generating"; whether these files are
already readable and how far behind they are is not documented. This module
therefore simply *tries*: while a session runs and the socket delivers no
positions, it polls the archive with HTTP Range requests and feeds every new
line - real data only, with its original timestamps. If the files are not
available (403/404) nothing is shown and the map keeps saying N/A.
"""
from __future__ import annotations

import asyncio
import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

import httpx

log = logging.getLogger("archive_follow")

ARCHIVE = "https://livetiming.formula1.com/static/"
LINE_RE = re.compile(r"^(\d+):(\d{2}):(\d{2})\.(\d{3})(.*)$")
TOPICS = ("Position.z", "CarData.z")
INITIAL_TAIL = 12          # on first contact only the newest lines are replayed


@dataclass
class StreamCursor:
    offset: int = 0
    remainder: bytes = b""
    first: bool = True
    missing_since: Optional[float] = None
    lines: int = 0


def split_stream_chunk(cursor: StreamCursor, chunk: bytes) -> list[str]:
    """Append ``chunk`` to the cursor and return the complete new lines."""
    data = cursor.remainder + chunk
    parts = data.split(b"\n")
    cursor.remainder = parts.pop()               # incomplete last line (may be b"")
    out = []
    for raw in parts:
        line = raw.decode("utf-8-sig", errors="replace").strip()
        if line:
            out.append(line)
    return out


def parse_stream_line(line: str) -> Optional[Any]:
    """``HH:MM:SS.mmm<json>`` -> decoded JSON payload (a base64 string for .z topics)."""
    import json
    m = LINE_RE.match(line.lstrip("﻿"))
    if not m:
        return None
    try:
        return json.loads(m.group(5))
    except ValueError:
        return None


@dataclass
class ArchiveFollower:
    """Polls the archive streams of the running session."""
    poll_seconds: float = 3.0
    cursors: dict[str, StreamCursor] = field(default_factory=dict)
    path: Optional[str] = None
    state: str = "idle"                 # idle | probing | following | unavailable
    last_data: float = 0.0

    def reset(self, path: Optional[str]) -> None:
        self.path = path
        self.cursors = {t: StreamCursor() for t in TOPICS}
        self.state = "idle"

    async def poll(self, http: httpx.AsyncClient, emit: Callable[[str, Any], Any]) -> None:
        if not self.path:
            return
        got_any = False
        for topic in TOPICS:
            cur = self.cursors.setdefault(topic, StreamCursor())
            if cur.missing_since and time.monotonic() - cur.missing_since < 60:
                continue                            # re-check missing files once a minute
            url = f"{ARCHIVE}{self.path}{topic}.jsonStream"
            headers = {"Range": f"bytes={cur.offset}-"} if cur.offset else {}
            try:
                r = await http.get(url, headers=headers)
            except httpx.HTTPError as exc:
                log.debug("archive poll %s failed: %s", topic, exc)
                continue
            if r.status_code in (403, 404):
                if cur.missing_since is None:
                    log.info("Archive stream %s not (yet) published for %s (HTTP %s)",
                             topic, self.path, r.status_code)
                cur.missing_since = time.monotonic()
                continue
            if r.status_code == 416:                # nothing new
                cur.missing_since = None
                continue
            if r.status_code not in (200, 206):
                log.debug("archive poll %s: HTTP %s", topic, r.status_code)
                continue
            cur.missing_since = None
            body = r.content
            if r.status_code == 200 and cur.offset:
                if len(body) <= cur.offset:          # server ignored Range and nothing is new
                    continue
                body = body[cur.offset:]
            cur.offset += len(body)
            lines = split_stream_chunk(cur, body)
            if cur.first:
                cur.first = False
                if lines:
                    log.info("Archive stream %s is readable during the session (%d lines so far) "
                             "- using it for %s", topic, len(lines),
                             "car positions" if topic == "Position.z" else "car telemetry")
                lines = lines[-INITIAL_TAIL:]
            for line in lines:
                payload = parse_stream_line(line)
                if payload is None:
                    continue
                cur.lines += 1
                got_any = True
                res = emit(topic, payload)
                if asyncio.iscoroutine(res):
                    await res
        if got_any:
            self.last_data = time.monotonic()
            self.state = "following"
        elif all(c.missing_since for c in self.cursors.values()):
            self.state = "unavailable"
        elif self.state == "idle":
            self.state = "probing"
