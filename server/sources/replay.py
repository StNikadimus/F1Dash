"""Replay of recorded sessions.

Supported inputs
----------------
* ``*.jsonl`` / ``*.jsonl.gz`` - recordings written by this server in live mode
  (one ``{"t": iso, "topic": ..., "data": ...}`` object per line)
* ``*.json`` / ``*.json.gz`` - a JSON list of ``{"timestamp": iso, "updates": {topic: data}}``
  (format used by the MIT-licensed matteocelani/f1-telemetry recordings; the
  bundled sample uses it)
* an F1 archive session path, e.g. ``2026/2026-03-29_Japanese_Grand_Prix/2026-03-29_Race/``,
  or ``latest``. The official archive (livetiming.formula1.com/static) is free
  and - after a session - also contains Position.z and CarData.z.
"""
from __future__ import annotations

import asyncio
import gzip
import json
import logging
import re
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Optional

import httpx

from ..config import DATA_DIR, resolve_path
from ..telemetry import parse_utc
from .base import Sink, Source
from .f1_live import _local_to_utc

log = logging.getLogger("replay")

ARCHIVE = "https://livetiming.formula1.com/static/"
ARCHIVE_TOPICS = [
    "Heartbeat", "SessionInfo", "SessionStatus", "SessionData", "ExtrapolatedClock", "LapCount",
    "TrackStatus", "DriverList", "TimingData", "TimingAppData", "TimingStats", "RaceControlMessages",
    "WeatherData", "TeamRadio", "Position.z", "CarData.z",
    # 2026: pit flags (InPit / PitOut) often arrive only in TimingDataF1, and the official pit-lane
    # times are the fallback signal for the pit-lane reconstruction (missing topics are skipped)
    "TimingDataF1", "PitLaneTimeCollection",
    # lap completions (a second signal for the lap / sector progress of qualifying and practice)
    "LapSeries",
]
# small topics downloaded as soon as the session is known (before SYNC): the session clock,
# Q1 / Q2 / Q3 and start / end - the session-aware SYNC options need them
META_TOPICS = ["Heartbeat", "SessionInfo", "SessionStatus", "SessionData", "ExtrapolatedClock",
               "RaceControlMessages"]
LINE_RE = re.compile(r"^(\d+):(\d{2}):(\d{2})\.(\d{3})(.*)$")


@dataclass(slots=True)
class Event:
    t: datetime
    topic: str
    data: Any
    snap: bool = False


def _open_text(path: Path):
    if path.suffix == ".gz":
        return gzip.open(path, "rt", encoding="utf-8-sig")
    return open(path, "r", encoding="utf-8-sig")


def load_file(path: Path) -> list[Event]:
    events: list[Event] = []
    name = path.name.lower()
    if ".jsonl" in name:
        with _open_text(path) as fh:
            for n, line in enumerate(fh, 1):
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                    t = parse_utc(rec.get("t"))
                    if t is None or not isinstance(rec.get("topic"), str):
                        raise ValueError("missing t/topic")
                    events.append(Event(t, rec["topic"], rec.get("data"), bool(rec.get("snap"))))
                except ValueError as exc:
                    log.warning("%s:%d malformed line skipped (%s)", path.name, n, exc)
    else:
        with _open_text(path) as fh:
            data = json.load(fh)
        for rec in data if isinstance(data, list) else []:
            t = parse_utc(rec.get("timestamp"))
            upd = rec.get("updates")
            if t is None or not isinstance(upd, dict):
                continue
            for topic, payload in upd.items():
                events.append(Event(t, topic, payload))
    events.sort(key=lambda e: e.t)
    return events


async def _latest_archive_path(http: httpx.AsyncClient) -> str:
    now = datetime.now(timezone.utc)
    best = None
    for year in (now.year, now.year - 1):
        r = await http.get(f"{ARCHIVE}{year}/Index.json")
        if r.status_code != 200:
            continue
        idx = json.loads(r.content.decode("utf-8-sig"))
        for m in idx.get("Meetings") or []:
            for s in m.get("Sessions") or []:
                end = _local_to_utc(s.get("EndDate"), s.get("GmtOffset"))
                if s.get("Path") and end and end < now - timedelta(minutes=30):
                    if best is None or end > best[0]:
                        best = (end, s["Path"])
        if best:
            return best[1]
    raise RuntimeError("no finished session found in the archive index")


async def load_archive(path: str, cache_dir: Path, topics: Optional[list[str]] = None,
                       progress: Optional[Callable[[str], None]] = None) -> list[Event]:
    async with httpx.AsyncClient(timeout=60, headers={"User-Agent": "f1-tv-dashboard/1.0"},
                                 follow_redirects=True) as http:
        if path == "latest":
            path = await _latest_archive_path(http)
            log.info("Latest archived session: %s", path)
        path = path.strip("/") + "/"
        target = cache_dir / path
        target.mkdir(parents=True, exist_ok=True)
        files: dict[str, Path] = {}
        for topic in topics or ARCHIVE_TOPICS:
            f = target / f"{topic}.jsonStream"
            if not f.exists():
                # streamed to a temporary file: an interrupted download never leaves a truncated cache file
                tmp = f.with_suffix(".part")
                done, last = 0, 0.0
                async with http.stream("GET", f"{ARCHIVE}{path}{topic}.jsonStream") as r:
                    if r.status_code != 200:
                        log.info("Archive topic %s not available (HTTP %s)", topic, r.status_code)
                        continue
                    total = int(r.headers.get("content-length") or 0)
                    with open(tmp, "wb") as fh:
                        async for chunk in r.aiter_bytes(1 << 16):
                            fh.write(chunk)
                            done += len(chunk)
                            if progress and time.monotonic() - last > 0.5:
                                last = time.monotonic()
                                progress(f"{topic} {done / 1e6:.0f}" + (f"/{total / 1e6:.0f}" if total else "") + " MB")
                tmp.replace(f)
                log.info("Downloaded %s (%.1f MB)", f.name, done / 1e6)
            files[topic] = f
    if progress:
        progress("reading the downloaded data")
    # reading + parsing 100+ MB of JSON lines takes seconds: never on the event loop
    # (the dashboard, the remote keys and the VOYO clock must stay responsive meanwhile)
    return await asyncio.to_thread(_parse_archive, files)


def _parse_archive(files: dict[str, Path]) -> list[Event]:
    raw = {topic: f.read_text(encoding="utf-8-sig") for topic, f in files.items()}
    # Base time: Heartbeat messages carry absolute UTC
    base: Optional[datetime] = None
    for line in raw.get("Heartbeat", "").splitlines():
        m = LINE_RE.match(line.strip())
        if not m:
            continue
        try:
            utc = parse_utc(json.loads(m.group(5)).get("Utc"))
        except ValueError:
            continue
        if utc:
            off = timedelta(hours=int(m.group(1)), minutes=int(m.group(2)), seconds=int(m.group(3)),
                            milliseconds=int(m.group(4)))
            base = utc - off
            break
    base = base or datetime.now(timezone.utc)

    events: list[Event] = []
    for topic, text in raw.items():
        for line in text.splitlines():
            m = LINE_RE.match(line.strip())
            if not m:
                continue
            try:
                data = json.loads(m.group(5))
            except ValueError:
                continue
            off = timedelta(hours=int(m.group(1)), minutes=int(m.group(2)), seconds=int(m.group(3)),
                            milliseconds=int(m.group(4)))
            events.append(Event(base + off, topic, data))
    events.sort(key=lambda e: e.t)
    return events


def _session_start(events: list[Event]) -> Optional[datetime]:
    """Time of the first 'Started' session status in a recording."""
    for e in events:
        d = e.data if isinstance(e.data, dict) else {}
        if (e.topic == "SessionStatus" and d.get("Status") == "Started") or \
                (e.topic == "SessionInfo" and d.get("SessionStatus") == "Started"):
            return e.t
    return None


class ReplaySource(Source):
    mode = "replay"

    def __init__(self, cfg: dict[str, Any]) -> None:
        self.src = str(cfg.get("source") or "")
        self.speed = max(0.1, float(cfg.get("speed", 1.0)))
        raw = str(cfg.get("start_offset", "auto")).strip().lower()
        # "auto" = jump to 10 s before the session actually starts (recordings
        # often begin 20+ minutes before lights out); a number = seconds to skip
        self.start_offset: Optional[float] = None if raw in ("auto", "") else float(raw)
        self.loop = bool(cfg.get("loop", True))
        self._t0: Optional[datetime] = None
        self._wall0 = time.time()
        self._mono0 = time.monotonic()

    def now(self) -> datetime:
        if self._t0 is None:
            return datetime.now(timezone.utc)
        return self._t0 + timedelta(seconds=(time.monotonic() - self._mono0) * self.speed)

    def map_time(self, ts: datetime) -> int:
        if self._t0 is None:
            return int(ts.timestamp() * 1000)
        return int((self._wall0 + (ts - self._t0).total_seconds() / self.speed) * 1000)

    async def _load(self) -> list[Event]:
        p = resolve_path(self.src)
        if p.exists():
            log.info("Loading replay file %s", p)
            return await asyncio.to_thread(load_file, p)
        if self.src == "latest" or re.match(r"^\d{4}/", self.src):
            return await load_archive(self.src, DATA_DIR / "archive_cache")
        raise FileNotFoundError(f"Replay source not found: {self.src}")

    async def run(self, sink: Sink) -> None:
        sink.set_status(state="loading", detail=f"Loading replay {self.src}")
        try:
            events = await self._load()
        except Exception as exc:  # noqa: BLE001
            log.error("Replay load failed: %s", exc)
            sink.set_status(state="error", detail=f"Replay load failed: {exc}")
            while True:
                await asyncio.sleep(3600)
        if not events:
            sink.set_status(state="error", detail="Replay contains no messages")
            return
        log.info("Replay loaded: %d messages, %s -> %s", len(events), events[0].t, events[-1].t)
        while True:
            await self._play(sink, events)
            if not self.loop:
                sink.set_status(state="finished", detail="Replay finished")
                return
            sink.set_status(state="finished", detail="Replay finished - restarting in 10 s")
            await asyncio.sleep(10)

    async def _play(self, sink: Sink, events: list[Event]) -> None:
        await sink.begin_snapshot()
        first = events[0].t
        if self.start_offset is None:
            started = _session_start(events)
            start = max(first, started - timedelta(seconds=10)) if started else first
            if started:
                log.info("Replay: skipping %d min of pre-session data (session started %s); "
                         "set replay.start_offset = 0 to play from the very beginning",
                         int((start - first).total_seconds() // 60), started.strftime("%H:%M:%S UTC"))
        else:
            start = first + timedelta(seconds=self.start_offset)
        i = 0
        # Fast-forward: apply everything before the start offset instantly,
        # skipping the high-rate streams (only their latest value matters).
        last_z: dict[str, Event] = {}
        while i < len(events) and events[i].t < start:
            e = events[i]
            if e.topic.endswith(".z") or e.topic in ("Position", "CarData"):
                last_z[e.topic] = e
            else:
                await sink.feed(e.topic, e.data, e.t, snapshot=e.snap)
            i += 1
        self._t0, self._mono0, self._wall0 = start, time.monotonic(), time.time()
        for e in last_z.values():
            await sink.feed(e.topic, e.data, e.t)
        sink.set_status(state="playing", detail=f"Replay x{self.speed:g}", total=len(events))
        processed = 0
        while i < len(events):
            e = events[i]
            due = (e.t - self._t0).total_seconds() / self.speed
            wait = due - (time.monotonic() - self._mono0)
            if wait > 0:
                await asyncio.sleep(min(wait, 5.0))
                continue
            await sink.feed(e.topic, e.data, e.t, snapshot=e.snap)
            i += 1
            processed += 1
            if processed % 500 == 0:
                await asyncio.sleep(0)       # never starve the event loop
