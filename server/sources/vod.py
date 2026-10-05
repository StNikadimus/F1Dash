"""VOD source: one complete historical session, shown at whatever time the
VOYO recording is at (seekable in both directions).

* session: chosen automatically from the VOYO page title (OpenF1 sessions,
  see server/openf1.py), or fixed with ``[vod] session_key`` / ``--vod KEY``;
* data: the official F1 live-timing archive of that session
  (livetiming.formula1.com/static/<path>, public, no login) - the same raw
  topics as live, so every panel (tyres, pit, penalties, positions,
  telemetry, race control, weather) works;
* the engine calls :meth:`ensure` every tick with the synchronised target
  time; the source feeds the events around it into the timeline. Long jumps
  restore a pre-computed topic checkpoint (every 60 s) instead of replaying
  the whole session.
"""
from __future__ import annotations

import asyncio
import bisect
import json
import logging
import math
import pickle
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

import httpx

from ..feedstate import FeedState
from ..openf1 import (ClockStart, OpenF1Client, OpenF1Error, RefEvents, merge_refs, ref_events_from_openf1,
                      status_series_starts)
from ..telemetry import parse_utc
from .base import Sink, Source
from .f1_live import _local_to_utc
from ..session_phases import build_timeline
from .replay import ARCHIVE, META_TOPICS, Event, load_archive

log = logging.getLogger("vod")

CKPT_EVERY_MS = 60_000
AHEAD_MS = 8_000           # events fed ahead of the target (position look-ahead for the map)
JUMP_MS = 120_000          # a forward jump larger than this restores a checkpoint instead of feeding


def _ms(e: Event) -> float:
    return e.t.timestamp() * 1000


async def archive_session_path(year: int, session_key: int, cache_dir: Path) -> tuple[str, dict]:
    """Archive path of a session from the season Index.json (key = OpenF1 session_key)."""
    f = cache_dir / str(year) / "Index.json"
    idx = None
    if f.exists() and time.time() - f.stat().st_mtime < 6 * 3600:
        try:
            idx = json.loads(f.read_text(encoding="utf-8-sig"))
        except ValueError:
            idx = None
    if idx is None:
        async with httpx.AsyncClient(timeout=30, headers={"User-Agent": "f1-tv-dashboard/1.0"},
                                     follow_redirects=True) as http:
            r = await http.get(f"{ARCHIVE}{year}/Index.json")
        if r.status_code != 200:
            if f.exists():
                idx = json.loads(f.read_text(encoding="utf-8-sig"))
            else:
                raise RuntimeError(f"F1 archive index {year} not available (HTTP {r.status_code})")
        else:
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_bytes(r.content)
            idx = json.loads(r.content.decode("utf-8-sig"))
    for m in idx.get("Meetings") or []:
        for s in m.get("Sessions") or []:
            if s.get("Key") == session_key and s.get("Path"):
                start = _local_to_utc(s.get("StartDate"), s.get("GmtOffset"))
                end = _local_to_utc(s.get("EndDate"), s.get("GmtOffset"))
                info = {"session_key": session_key, "meeting_key": m.get("Key"), "session_name": s.get("Name"),
                        "session_type": s.get("Type"), "meeting_name": m.get("Name"),
                        "location": m.get("Location"), "gmt_offset": s.get("GmtOffset"),
                        "country_code": (m.get("Country") or {}).get("Code"),
                        "date_start": start.isoformat() if start else None,
                        "date_end": end.isoformat() if end else None}
                return s["Path"], info
    raise RuntimeError(f"session {session_key} not in the F1 archive index {year}")


async def archive_catalog(year: int, cache_dir: Path) -> dict:
    """SELECT SESSION fallback when OpenF1 is unreachable: the season's F1 archive index."""
    f = cache_dir / str(year) / "Index.json"
    if not f.exists() or time.time() - f.stat().st_mtime > 6 * 3600:
        async with httpx.AsyncClient(timeout=30, headers={"User-Agent": "f1-tv-dashboard/1.0"},
                                     follow_redirects=True) as http:
            r = await http.get(f"{ARCHIVE}{year}/Index.json")
        if r.status_code == 200:
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_bytes(r.content)
        elif not f.exists():
            raise RuntimeError(f"HTTP {r.status_code}")
    idx = json.loads(f.read_text(encoding="utf-8-sig"))
    now = datetime.now(timezone.utc)
    meetings = []
    for m in idx.get("Meetings") or []:
        sess = []
        for s in m.get("Sessions") or []:
            start = _local_to_utc(s.get("StartDate"), s.get("GmtOffset"))
            if s.get("Key") and s.get("Path") and start and start < now:
                sess.append({"session_key": s["Key"], "session_name": s.get("Name"),
                             "date_start": start.isoformat()})
        if sess and "test" not in str(m.get("Name") or "").lower():
            meetings.append({"meeting_key": m.get("Key"), "name": m.get("Name"),
                             "country_code": (m.get("Country") or {}).get("Code"),
                             "date": sess[0]["date_start"][:10], "sessions": sess})
    meetings.sort(key=lambda e: e["date"])
    return {"year": year, "meetings": meetings, "source": "f1-archive"}


def ref_events_from_archive(events: list[Event]) -> RefEvents:
    """Fallback reference events from the archive itself (same clock as OpenF1)."""
    ev = RefEvents(source="archive")
    laps: dict[str, int] = {}
    clock = ClockStart()
    for e in events:
        d = e.data if isinstance(e.data, dict) else None
        if d is None:
            continue
        if e.topic == "SessionStatus":
            st = d.get("Status")
            if st == "Started":
                ev.add_start(_ms(e), "SessionStatus")
            elif st == "Finished" and not any(abs(x - _ms(e)) < 2500 for x in ev.finishes):
                ev.finishes.append(_ms(e))
        elif e.topic == "SessionData" and d.get("StatusSeries") is not None:
            # the F1 archive has no SessionStatus topic: lights out / session start is the
            # StatusSeries entry SessionStatus "Started" with its own millisecond Utc
            for u, st in status_series_starts(d.get("StatusSeries")):
                if u is None:
                    ev.parse_errors += 1
                elif st == "Started":
                    ev.add_start(u, "SessionData.StatusSeries")
                elif st == "Finished" and not any(abs(x - u) < 2500 for x in ev.finishes):
                    ev.finishes.append(u)
        elif e.topic == "ExtrapolatedClock":
            from ..telemetry import parse_utc
            u = parse_utc(d.get("Utc")) if d.get("Utc") else None
            t = clock.feed(u.timestamp() * 1000 if u else _ms(e), d.get("Remaining"), d.get("Extrapolating"))
            if t is not None:
                ev.add_start(t, "ExtrapolatedClock ±1 s", approx=True)
        elif e.topic == "RaceControlMessages":
            msgs = d.get("Messages")
            for m in (msgs.values() if isinstance(msgs, dict) else msgs if isinstance(msgs, list) else []):
                if isinstance(m, dict) and isinstance(m.get("Message"), str):
                    ev.add_notice(_ms(e), m["Message"].upper())
        elif e.topic in ("TimingData", "TimingDataF1") and isinstance(d.get("Lines"), dict):
            for num, line in d["Lines"].items():
                if not isinstance(line, dict) or "NumberOfLaps" not in line:
                    continue
                try:
                    n = int(line["NumberOfLaps"])
                except (TypeError, ValueError):
                    continue
                prev = laps.get(num)
                laps[num] = max(n, prev or 0)
                if prev is not None and n > prev and not line.get("InPit") and not line.get("PitOut"):
                    ev.add_crossing(num, _ms(e), n)
    ev.finishes.sort()
    return ev


class VodSource(Source):
    mode = "vod"
    speed = 1.0

    def __init__(self, cfg: dict[str, Any], openf1: OpenF1Client, cache_dir: Path) -> None:
        raw = str(cfg.get("session_key", "auto")).strip().lower()
        self.auto = raw in ("", "auto")
        self.openf1 = openf1
        self.cache_dir = cache_dir
        self.requested: Optional[int] = None if self.auto else int(raw)
        self.preload = bool(cfg.get("preload_data", False))
        self._go = asyncio.Event()          # set by the engine once the video has a time (sync)
        self.state = "waiting"              # waiting | loading | ready | error
        self.reason = "waiting for the VOYO page title (session detection)" if self.auto else "loading"
        self.session: Optional[dict] = None
        self.detection: Optional[dict] = None
        self.events: list[Event] = []
        self._times: list[float] = []
        self.ckpts: list[tuple[float, int, bytes]] = []
        self.ref = RefEvents()
        self._fed_idx: Optional[int] = None
        self._fed_ms = -math.inf
        self._wake = asyncio.Event()
        self.on_loaded: Optional[Callable[[dict, RefEvents], None]] = None
        # session metadata (start, time zone) known before the data is downloaded: the time sync
        # can be set up while a large race archive is still downloading
        self.on_meta: Optional[Callable[[dict, Optional[RefEvents]], None]] = None
        self.loaded_key: Optional[int] = None

    # time base: presentation = F1 epoch ms (no virtual clock)
    def now(self) -> datetime:
        return datetime.now(timezone.utc)

    @property
    def waiting_for_sync(self) -> bool:
        return self.state == "waiting_sync"

    def allow_load(self) -> None:
        """The video has a time now: the session data may be downloaded."""
        self._go.set()

    def invalidate(self) -> None:
        """The engine emptied the timeline: the next ensure() rebuilds the state from a checkpoint."""
        self._fed_idx, self._fed_ms = None, -math.inf

    def unload(self) -> None:
        """Another VOYO video: the data of the previous session must not stay on screen."""
        self._go.clear()
        self.requested = None
        self.loaded_key = None
        self.session = None
        self.events, self._times, self.ckpts = [], [], []
        self.ref = RefEvents()
        self._fed_idx, self._fed_ms = None, -math.inf
        self.state, self.reason = "waiting", "new VOYO video - detecting its session"

    def request(self, session_key: int, why: str) -> None:
        if session_key != self.requested or session_key != self.loaded_key:
            log.info("VOD session requested: %s (%s)", session_key, why)
            if session_key != self.requested:
                self._go.clear()                # a new session waits for its own sync
            self.requested = session_key
            self._wake.set()

    # ------------------------------------------------------------------ loading
    async def run(self, sink: Sink) -> None:
        while True:
            if self.requested is None or self.requested == self.loaded_key:
                sink.set_status(state=self.state, detail=self.reason)
                self._wake.clear()
                await self._wake.wait()
                continue
            key = self.requested
            try:
                await self._load(key, sink)
            except Exception as exc:  # noqa: BLE001
                log.error("VOD session %s could not be loaded: %s - retrying in 60 s", key, exc)
                self.state, self.reason = "error", f"session {key}: {exc} (retrying in 60 s)"
                sink.set_status(state=self.state, detail=self.reason)
                self._wake.clear()
                try:                                    # a new request interrupts the wait
                    await asyncio.wait_for(self._wake.wait(), 60)
                except asyncio.TimeoutError:
                    pass
                continue
            sink.set_status(state=self.state, detail=self.reason)

    async def _load(self, key: int, sink: Sink) -> None:
        self.state, self.reason = "loading", f"loading session {key}"
        sink.set_status(state="loading", detail=self.reason)
        sess: Optional[dict] = None
        try:
            sess = await self.openf1.session(key)
        except OpenF1Error as exc:
            log.warning("%s - using the F1 archive index for the session metadata", exc)
        year = int((sess or {}).get("year") or 0) or None
        if year is None:
            year = datetime.now(timezone.utc).year
        path, info = None, None
        for y in (year, year - 1):
            try:
                path, info = await archive_session_path(y, key, self.cache_dir)
                break
            except (RuntimeError, httpx.HTTPError, ValueError) as exc:
                last = exc
        if path is None:
            raise RuntimeError(f"no F1 archive data ({last})")
        session = {**info, **{k: v for k, v in (sess or {}).items() if v is not None}}
        if sess and info.get("meeting_name"):
            session["meeting_name"] = info["meeting_name"]
        what = f"{session.get('meeting_name')} {session.get('session_name')}"
        # small requests first: lap times / race control for the L and S sync keys
        of1_ref: Optional[RefEvents] = None
        try:
            laps = await self.openf1.laps(session)
            rc = await self.openf1.race_control(session)
            of1_ref = ref_events_from_openf1(laps, rc)
            if of1_ref.empty():
                of1_ref = None
        except OpenF1Error as exc:
            log.warning("%s - lap crossings from the F1 archive will be the sync reference", exc)
        # the session's structure (Q1/Q2/Q3 / practice clock, phase start / end markers) from the
        # small official timing topics - so the session-aware SYNC options work before the big download
        timeline = None
        try:
            small = await load_archive(path, self.cache_dir, topics=META_TOPICS)
            timeline = build_timeline(small, session.get("session_type"), session.get("session_name"))
            if timeline.phases:
                log.info("VOD: %s structure: %s", what, ", ".join(
                    f"{p.label} {'%d:%02d' % divmod(p.duration // 1000, 60) if p.duration else '?'}"
                    for p in timeline.phases))
        except Exception as exc:  # noqa: BLE001 - optional (the SYNC menu then offers the other methods)
            log.warning("VOD: session clock / phases not available yet (%s)", exc)
        if self.requested != key:
            return
        # lights out / session start from the small archive topics too (SessionData.StatusSeries,
        # ExtrapolatedClock): L works before the big download even when OpenF1 has nothing
        arch_meta = ref_events_from_archive(small) if timeline is not None else None
        meta_ref = merge_refs(of1_ref, arch_meta if arch_meta is not None and not arch_meta.empty() else None)
        meta_ref.timeline = timeline
        log.info("VOD: sync reference before the download: %d start(s) %s, %d lap crossings (%s)",
                 len(meta_ref.starts), [meta_ref.start_src.get(t) for t in meta_ref.starts],
                 sum(len(v) for v in meta_ref.crossings.values()), meta_ref.source)
        self.session = None
        if self.on_meta:
            self.on_meta(session, meta_ref)
        if not self.preload and not self._go.is_set():
            # the big download waits until the video has a time (SYNC) - nothing is loaded before
            self.state = "waiting_sync"
            self.reason = f"{what}: data loads after SYNC (set the video time)"
            sink.set_status(state="waiting", detail=self.reason)
            log.info("VOD: %s", self.reason)
            while not self._go.is_set():
                if self.requested != key:
                    return                          # another session was chosen meanwhile
                try:
                    await asyncio.wait_for(self._go.wait(), 1.0)
                except asyncio.TimeoutError:
                    pass
            self.state = "loading"
        self.reason = f"downloading {what}"
        sink.set_status(state="loading", detail=self.reason)

        def progress(text: str) -> None:
            if self.requested == key:
                self.reason = f"downloading {what}: {text}"

        events = await load_archive(path, self.cache_dir, progress=progress)
        self.reason = f"preparing {what} ({len(events)} messages)"
        log.info("VOD: %s", self.reason)
        if not events:
            raise RuntimeError("archive contains no data")
        ckpts, arch_ref = await asyncio.to_thread(self._prepare, events)
        ref = merge_refs(of1_ref, arch_ref)
        full_tl = await asyncio.to_thread(build_timeline, events, session.get("session_type"),
                                          session.get("session_name"))
        ref.timeline = full_tl if full_tl.phases or timeline is None else timeline
        self.events, self.ckpts, self.ref, self.session = events, ckpts, ref, session
        self._times = [_ms(e) for e in events]
        self._fed_idx, self._fed_ms = None, -math.inf
        self.loaded_key = key
        self.state = "ready"
        self.reason = f"{session.get('meeting_name')} · {session.get('session_name')} ({len(events)} messages, " \
                      f"{ref.count()} sync reference events from {ref.source})"
        log.info("VOD ready: %s", self.reason)
        if self.on_loaded:
            self.on_loaded(session, ref)

    @staticmethod
    def _prepare(events: list[Event]) -> tuple[list, RefEvents]:
        fs = FeedState()
        ckpts: list[tuple[float, int, bytes]] = []
        next_ck = -math.inf
        for i, e in enumerate(events):
            t = _ms(e)
            if t >= next_ck:
                ckpts.append((t, i, pickle.dumps(fs.topics, pickle.HIGHEST_PROTOCOL)))
                next_ck = t + CKPT_EVERY_MS
            if not (e.topic.endswith(".z") or e.topic in ("Position", "CarData")):
                try:
                    fs.apply(e.topic, e.data, e.snap, t)
                except Exception:  # noqa: BLE001
                    pass
        return ckpts, ref_events_from_archive(events)

    # ------------------------------------------------------------------ serving
    def span(self) -> Optional[tuple[float, float]]:
        if not self._times:
            return None
        return self._times[0], self._times[-1]

    def ensure(self, target_ms: float, ingest: Callable, timeline) -> bool:
        """Feed the timeline around ``target_ms``. Returns True if the state was rebuilt."""
        if self.state != "ready" or not self.events or not math.isfinite(target_ms):
            return False
        reset = False
        if self._fed_idx is None or target_ms < timeline.oldest_ms() - 1 or target_ms > self._fed_ms + JUMP_MS:
            self._restore(target_ms, ingest, timeline)
            reset = True
        limit = target_ms + AHEAD_MS
        i = self._fed_idx
        while i < len(self.events) and self._times[i] <= limit:
            e = self.events[i]
            ingest(e.topic, e.data, self._times[i], e.snap)
            i += 1
        self._fed_idx = i
        self._fed_ms = max(self._fed_ms, limit)
        return reset

    def _restore(self, target_ms: float, ingest: Callable, timeline) -> None:
        pos = bisect.bisect_right([c[0] for c in self.ckpts], target_ms - 10_000) - 1
        t, idx, blob = self.ckpts[max(0, pos)]
        timeline.reset()
        for topic, data in pickle.loads(blob).items():
            ingest(topic, data, t, True)
        self._fed_idx = idx
        self._fed_ms = t
        # feed up to the target and fix the timeline's rebuild base at the checkpoint time
        while self._fed_idx < len(self.events) and self._times[self._fed_idx] <= t:
            e = self.events[self._fed_idx]
            ingest(e.topic, e.data, self._times[self._fed_idx], e.snap)
            self._fed_idx += 1
        timeline.advance(t)
        log.debug("VOD state restored from checkpoint %s for target %s", t, target_ms)
