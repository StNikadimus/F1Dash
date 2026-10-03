"""Engine: glues a data source, the timeline, the sync engine, the normalizer,
track geometry and the hub.

    Source --(raw topic messages, clock A timestamps)--> Timeline (buffer)
    SyncEngine (VOYO playback clock / fixed delay / live) --> target time on clock A
    Timeline.advance(target) --> state at that instant --> Normalizer --> Hub --> dashboards
"""
from __future__ import annotations

import asyncio
import logging
import math
import time
from datetime import datetime, timezone
from typing import Any, Optional

from .config import DATA_DIR
from .diagnostics import FeedDiagnostics, format_report
from .hub import Hub
from .models import Availability
from .normalizer import Normalizer
from .sources.base import Source
from .openf1 import OpenF1Client, OpenF1Error, RefEvents, match_session, parse_title
from .sync import SyncManager, VoyoSample
from .telemetry import POS_FRESH_MS, parse_utc
from .timeline import INF, Timeline
from .pitlane import (PitLaneCollector, Reconstruction, collect_from_events, deviation_from, needs_reconstruction,
                      reconstruct, traversals_from_samples)
from .pitlane_seed import seed_pitlane
from .track import OutlineLearner, TrackGeometry, TrackProvider, outline_problem
from .track_match import (MIN_SAMPLES, _Grid, _densify, check_outline, fit_reference, known_layouts,
                          reference_id, reference_points)

log = logging.getLogger("engine")

TICK = 0.1                  # s, sync/timeline tick (10 Hz)
STATE_INTERVAL = 0.25       # s, max rate of state patches
TEL_INTERVAL = 0.25         # s, max rate of telemetry messages
SYNC_INTERVAL = 0.5         # s, sync status messages
SNAPSHOT_BACKDATE_MS = 1500  # a subscribe snapshot is older than the updates that follow it
CLOCK_WARN_MS = 2000.0       # PC clock vs F1 timestamps (incl. latency) beyond this: warn in the log
FEED_STALE_S = 25.0
PIT_VALIDATE_M = 8.0         # a live pit pass within this RMS distance (m) of the loaded pit lane confirms it
MAP_REPORT_S = 300.0         # map validation summary in the log          # live socket open but silent this long (F1 sends a Heartbeat every 15 s): DELAYED


class Engine:
    def __init__(self, cfg: dict[str, Any], source: Source, tracks: TrackProvider, hub: Hub,
                 token_configured: bool = False) -> None:
        self.cfg = cfg
        self.source = source
        self.tracks = tracks
        self.hub = hub
        sync_cfg = cfg.get("sync") or {}
        self.timeline = Timeline(float(sync_cfg.get("buffer_seconds", 120)), on_topic=self._on_topic,
                                 map_time=source.map_time,
                                 hold_max_seconds=float(sync_cfg.get("max_hold_seconds", 1800)))
        self.timeline.track_latency = source.mode == "live"
        # position samples streamed ahead of the target so the map can be drawn exactly at video time
        self.pos_lookahead_ms = max(0.0, float(sync_cfg.get("position_lookahead_ms", 2500)))
        self.vod = source.mode == "vod"
        self.sync = SyncManager(sync_cfg, bool((cfg.get("voyo") or {}).get("enabled")),
                                float(cfg["source"].get("delay_seconds", 0.0)), source.speed,
                                DATA_DIR / "sync_calibration.json", vod=self.vod)
        self.openf1: Optional[OpenF1Client] = getattr(source, "openf1", None)
        self._detect_task: Optional[asyncio.Task] = None
        self._detected_for: Optional[str] = None
        self._detected_dur = 0.0
        self.media: dict = {"state": "waiting", "media_id": None, "manual": False, "label": None,
                            "session_key": None, "candidates": [], "how": "",
                            "reason": "waiting for a VOYO video (open the recording in the VOYO window)"}
        if self.vod and not source.auto:
            self.media.update(state="manual", manual=True, session_key=source.requested,
                              reason="session fixed in the configuration ([vod] session_key / --vod KEY)")
        if self.vod:
            source.on_loaded = self._vod_loaded
            source.on_meta = self._vod_meta
        self.normalizer = Normalizer(int(cfg["dashboard"].get("race_control_max", 60)))
        self.availability = Availability(token_configured=token_configured)
        self.geometry: Optional[TrackGeometry] = None
        self.outline_learner = OutlineLearner()
        # known circuit layout fitted onto the car positions (server/track_match.py)
        self._ref_samples: list = []
        self._ref_task: Optional[asyncio.Task] = None
        self._ref_last = 0.0
        self._ref_note: Optional[str] = None
        self._report_armed = 0.0
        self._check_last = 0.0
        self._seed_task: Optional[asyncio.Task] = None
        self._seed_key: Optional[int] = None
        self._pit_status: dict = {}
        self._pit_validation: dict = {}
        self._map_report_last = -1e9
        # pit lane (cache-first): live/replay learn incrementally, VOD from the loaded archive
        self.pit_collector: Optional[PitLaneCollector] = None
        self._pit_ctx: dict = {}
        self._pit_task: Optional[asyncio.Task] = None
        self._pit_last_poll = 0.0
        if not self.vod:
            self.timeline.on_position_entry = self._pit_position
        self._dirty = True
        self._tel_dirty = False
        self._session_id: Optional[tuple] = None
        self._track_id: Optional[tuple] = None
        self._track_task: Optional[asyncio.Task] = None
        self._in_pit: dict[str, bool] = {}
        self._laps: dict[str, Optional[int]] = {}
        self._order: list[str] = []
        self._status: dict[str, Any] = {"state": "starting", "mode": source.mode}
        self._last_rx: Optional[float] = None       # monotonic time of the last live-socket message
        self._stale_shown: Optional[int] = None
        self._clock_warned = -1e9
        self.diag = FeedDiagnostics()
        self.sc_keys = [str(k) for k in ((cfg.get("f1_tv") or {}).get("safety_car_position_keys") or [])]
        self._schedule: Optional[dict] = None
        self._parse_errors: dict[str, float] = {}
        self._last_full = 0.0
        self._target_ms: float = INF
        self._rate = source.speed
        self._clock_sent: Optional[tuple] = None
        self._selected: Optional[str] = None

    # state views (the state *at the target time*)
    @property
    def feed_state(self):
        return self.timeline.feed

    @property
    def positions(self):
        return self.timeline.positions

    @property
    def cardata(self):
        return self.timeline.cardata

    # ------------------------------------------------------------------ Sink API
    def _src_now_ms(self) -> float:
        return self.source.now().timestamp() * 1000

    def _feed_offset_ms(self) -> Optional[float]:
        """LIVE: receive time (clock B) minus F1 event time (clock A) of the feed messages -
        network latency plus the offset of this computer's clock. None until measured."""
        lat = self.timeline.receive_latency_s() if self.source.mode == "live" else None
        if lat is None or abs(lat) > 6 * 3600:
            return None
        return lat * 1000

    def _live_f1_now_ms(self) -> float:
        """F1 time of the live edge: wall clock corrected by the measured feed offset, so a
        wrong PC clock moves neither the session clock nor the pit / DNF timers."""
        off = self._feed_offset_ms()
        return self._src_now_ms() - (off or 0.0)

    def _snapshot_ms(self, receive_ms: float, heartbeat_ms: Optional[float] = None) -> float:
        """The subscribe result carries no timestamp: it is F1's state when it was sent, i.e.
        receive time minus the measured offset - never earlier, or it would be shown before
        its content happened (a delayed / video-synced board would see the future). Before
        anything is measured (first connect, nothing shown yet) a fixed backdate keeps it
        ahead of the updates that follow it - and never before F1's own last heartbeat in the
        snapshot (a PC clock that is behind)."""
        off = self._feed_offset_ms()
        if off is not None:
            return receive_ms - off
        est = receive_ms - SNAPSHOT_BACKDATE_MS
        return max(est, heartbeat_ms) if heartbeat_ms is not None else est

    async def feed(self, topic: str, data: Any, ts: Optional[datetime], snapshot: bool = False,
                   origin: str = "feed") -> None:
        receive_ms = self._src_now_ms()                         # clock B
        event_ms = ts.timestamp() * 1000 if ts else None        # clock A (None: unknown)
        if origin == "feed":
            self._last_rx = time.monotonic()
        if self.source.mode in ("live", "replay"):
            self.diag.observe(topic, data, snapshot, origin, time.monotonic())
        if snapshot and self.source.mode == "live":
            # (ts of a live snapshot: the Heartbeat Utc it contains, if any - see F1LiveSource)
            event_ms = self._snapshot_ms(receive_ms, event_ms)
        self._ingest(topic, data, event_ms, snapshot, origin, receive_ms)

    def _ingest(self, topic: str, data: Any, event_ms: Optional[float], snapshot: bool,
                origin: str = "feed", receive_ms: Optional[float] = None) -> None:
        if receive_ms is None:
            receive_ms = event_ms if event_ms is not None else self._src_now_ms()
        try:
            self.timeline.ingest(topic, data, event_ms, receive_ms, snapshot, origin)
            self.sync.observe_feed(topic, data, event_ms, snapshot)
            if self.pit_collector is not None and event_ms is not None and \
                    topic in ("TimingData", "TimingDataF1", "PitLaneTimeCollection"):
                self.pit_collector.timing(topic, data, event_ms)
        except Exception:  # noqa: BLE001 - one bad packet must never kill the feed
            self._parse_error(topic)
            return
        if topic in ("Position.z", "Position"):
            if self.availability.positions_source != origin or not self.availability.positions:
                if not self.availability.positions:
                    log.info("Receiving car positions (%s)", "public archive stream" if origin == "archive"
                             else "data feed")
                self.availability.positions = True
                self.availability.positions_source = origin
                self._dirty = True
        elif topic in ("CarData.z", "CarData"):
            if self.availability.car_data_source != origin or not self.availability.car_data:
                if not self.availability.car_data:
                    log.info("Receiving car telemetry")
                self.availability.car_data = True
                self.availability.car_data_source = origin
                self._dirty = True

    async def begin_snapshot(self) -> None:
        if self.source.mode == "live":
            # reconnect: keep the buffer (the video may still be behind), mark the reset point
            now = self._src_now_ms()
            self.timeline.mark_reset(self._snapshot_ms(now) - 1, now)
        else:
            # replay loop / new simulated race: time starts over
            self.timeline.reset()
            self.sync.on_source_reset()
            self._reset_side_state()
            self.hub.broadcast({"type": "pos_reset"})

    def set_status(self, **status: Any) -> None:
        self._status = {"mode": self.source.mode, **status}
        if status.get("state") == "connected":
            self._last_rx = time.monotonic()        # a fresh connection counts as fresh data
            self.diag.reset_connection(self._last_rx)
        self._stale_shown = None
        self._push_status()

    def _check_feed_age(self, mono: float) -> None:
        """LIVE: socket open but no message from F1 for FEED_STALE_S -> state "stale" (DELAYED),
        so the board never looks healthy without fresh data; back to "connected" when data flows."""
        st = self._status.get("state")
        if self.source.mode != "live" or st not in ("connected", "stale") or self._last_rx is None:
            return
        off = self._feed_offset_ms()
        if off is not None and abs(off) > CLOCK_WARN_MS and mono - self._clock_warned > 600:
            # the live edge is corrected for it; a delayed / video-synced board is not (its
            # shown moment is this computer's clock minus the delay)
            self._clock_warned = mono
            log.warning("This computer's clock differs from F1's by about %+.1f s (feed latency included) - "
                        "sync the Windows clock (Settings > Time > Sync now)", off / 1000)
        age = mono - self._last_rx
        if age > FEED_STALE_S:
            shown = int(age // 5)                    # refresh the "no data for N s" every 5 s
            if st != "stale" or shown != self._stale_shown:
                if st != "stale":
                    log.warning("No data from F1 live timing for %.0f s (connection still open)", age)
                self._status = {**self._status, "state": "stale", "feed_age_s": round(age),
                                "detail": f"no data from F1 for {age:.0f} s"}
                self._stale_shown = shown
                self._push_status()
        elif st == "stale":
            log.info("F1 live timing data flowing again")
            self._status = {k: v for k, v in self._status.items() if k != "feed_age_s"}
            self._status.update(state="connected", detail="Connected")
            self._stale_shown = None
            self._push_status()

    def set_schedule(self, schedule: Optional[dict]) -> None:
        self._schedule = schedule
        self._push_status()

    # ------------------------------------------------------------------ VOYO clock / sync commands
    def voyo_sample(self, sample: VoyoSample) -> None:
        self.sync.update(sample, self._src_now_ms())
        if not self.vod:
            return
        page = sample.page or {}
        mid = page.get("media_id") or sample.asset
        if mid != self.media.get("media_id"):
            self._new_media(mid, page)
        if self.source.auto and not self.media.get("manual"):
            key = "|".join(str(page.get(k, "")) for k in ("media_id", "title", "og_title", "media_title", "url_path"))
            dur = max(sample.duration or 0, float(sample.meta.get("length") or 0))
            if (key != self._detected_for or (dur and not self._detected_dur)) and \
                    (self._detect_task is None or self._detect_task.done()):
                self._detected_for = key
                self._detected_dur = dur
                self._detect_task = asyncio.create_task(self._detect_session(dict(page), dur))

    # ------------------------------------------------------------------ AUTO MEDIA SYNC (which session?)
    # Session detection only answers "which F1 session is this VOYO video"; it never
    # touches the time sync ("which F1 instant is currentTime") - that is SyncManager.
    def _new_media(self, media_id: str, page: dict) -> None:
        if self.media.get("media_id") is not None and self.source.loaded_key is not None and self.source.auto:
            # another video: never keep showing the previous video's session
            log.info("AUTO MEDIA SYNC: new VOYO video - previous session unloaded")
            self.source.unload()
            self.timeline.reset()
            self._reset_side_state()
            self.hub.broadcast({"type": "pos_reset"})
            self.sync.initialize(None, None)
        remembered = (self.sync.store.data.get("media_sessions") or {}).get(media_id)
        self.media = {"state": "detecting", "media_id": media_id, "manual": False, "label": None,
                      "session_key": None, "reason": "reading the VOYO title", "how": "", "candidates": [],
                      "title": page.get("media_title") or page.get("og_title") or page.get("title")}
        self._detected_for = None
        self._detected_dur = 0.0
        if not self.source.auto and self.source.requested:
            self.media.update(state="manual", manual=True, session_key=self.source.requested,
                              reason="session fixed in the configuration ([vod] session_key / --vod KEY)")
        elif remembered:
            self.media.update(state="manual", manual=True, session_key=int(remembered),
                              reason="session you chose earlier for this VOYO video")
            self.source.request(int(remembered), "remembered for this media")

    def media_state(self) -> dict:
        m = dict(self.media)
        src = self.source if self.vod else None
        if src is not None:
            sess = src.session or {}
            if m.get("session_key") and src.loaded_key == m["session_key"] and src.state == "ready":
                m["data"] = "loaded"
                m["label"] = f"{sess.get('meeting_name') or sess.get('location')} — {sess.get('session_name')}"
                m["country_code"] = sess.get("country_code")
                m["year"] = str(sess.get("date_start") or "")[:4]
            elif m.get("session_key") and src.state == "waiting_sync":
                m["data"] = "waiting_sync"
                m["data_reason"] = "data loads after SYNC (set the video time)"
            elif m.get("session_key") and src.state == "error":
                m["data"] = "error"
                m["data_reason"] = src.reason
            elif m.get("session_key"):
                m["data"] = "loading"
                m["data_reason"] = src.reason
            else:
                m["data"] = None
        m["candidates"] = [{"session_key": c.get("session_key"), "session_name": c.get("session_name"),
                            "meeting": c.get("meeting_name") or c.get("location"),
                            "date": str(c.get("date_start") or "")[:10]} for c in m.get("candidates") or []]
        return m

    async def _detect_session(self, page: dict, video_seconds: float) -> None:
        """VOYO title / media title / URL -> OpenF1 session -> load it. Never guesses."""
        info = parse_title(page.get("media_title"), page.get("og_title"), page.get("title"), page.get("url_path"),
                           published=page.get("published"))
        vod_cfg = self.cfg.get("vod") or {}
        now_year = datetime.now(timezone.utc).year
        years = [info.year] if info.year else [now_year, now_year - 1]
        sessions: list = []
        meetings: list = []
        errors = []
        for y in years:
            try:
                ys = await self.openf1.sessions(y)
                ym = await self.openf1.meetings(y)
                sessions += ys
                meetings += ym
            except OpenF1Error as exc:
                errors.append(f"{y}: {exc}")
        if not sessions:
            self._media_failed("failed", f"OpenF1 not reachable ({'; '.join(errors) or 'no sessions'})", [])
            self._detected_for = None            # retry with a later sample
            await asyncio.sleep(30)
            return
        for mt in meetings:                       # sessions carry no meeting name; add it for the selector
            for sx in sessions:
                if sx.get("meeting_key") == mt.get("meeting_key"):
                    sx.setdefault("meeting_name", mt.get("meeting_name"))
        det = match_session(info, sessions, meetings, video_seconds=video_seconds or None,
                            bare_title_is_race=bool(vod_cfg.get("bare_gp_title_is_race", True)),
                            race_min_video_seconds=float(vod_cfg.get("race_min_video_seconds", 8100)),
                            assume_recent_days=float(vod_cfg.get("assume_recent_season_days", 21)))
        if self.media.get("manual"):
            return                                # you chose meanwhile - your choice wins
        if det.session is None:
            self._media_failed(det.status, det.reason, det.candidates)
            return
        key = int(det.session["session_key"])
        self.media.update(state="detected", session_key=key, reason=det.reason, how=det.how,
                          candidates=det.candidates)
        self.sync.detect_reason = f"detected automatically: {det.reason} ({det.how})"
        log.info("AUTO MEDIA SYNC: %s -> session_key %s (%s)", det.reason, key, det.how)
        self.source.request(key, "VOYO title")

    def _media_failed(self, status: str, reason: str, candidates: list) -> None:
        self.media.update(state=status if status in ("ambiguous", "failed") else "failed", session_key=None,
                          reason=reason, how="", candidates=candidates)
        self.sync.detect_reason = f"AUTO MEDIA SYNC {'AMBIGUOUS' if status == 'ambiguous' else 'FAILED'}: {reason}"
        log.warning("AUTO MEDIA SYNC %s: %s", status.upper(), reason)

    def select_session(self, session_key: int) -> None:
        """Manual session choice (SELECT SESSION / API) - overrides detection, remembered per video."""
        if not self.vod:
            return
        self.media.update(state="manual", manual=True, session_key=int(session_key),
                          reason="selected by you", how="")
        mid = self.media.get("media_id")
        if mid:
            ms = self.sync.store.data.setdefault("media_sessions", {})
            ms[mid] = int(session_key)
            self.sync.store.save()
        self.sync.detect_reason = f"selected manually (session_key {session_key})"
        self.source.request(int(session_key), "manual")

    async def media_catalog(self, year: int) -> dict:
        """Grands Prix and sessions of a season for SELECT SESSION (OpenF1, else the F1 archive index)."""
        now = datetime.now(timezone.utc)
        try:
            sessions = await self.openf1.sessions(year)
            meetings = {m.get("meeting_key"): m for m in await self.openf1.meetings(year)}
            source = "openf1"
        except OpenF1Error as exc:
            from .sources.vod import archive_catalog
            try:
                return await archive_catalog(year, self.source.cache_dir)
            except Exception as exc2:  # noqa: BLE001
                return {"year": year, "meetings": [], "error": f"{exc}; F1 archive: {exc2}"}
        out: dict = {}
        for sx in sessions:
            start = parse_utc(sx.get("date_start"))
            if not start or start > now or sx.get("is_cancelled"):
                continue
            mk = sx.get("meeting_key")
            mt = meetings.get(mk) or {}
            e = out.setdefault(mk, {"meeting_key": mk, "name": mt.get("meeting_name") or sx.get("location"),
                                    "country_code": sx.get("country_code"), "date": str(sx.get("date_start"))[:10],
                                    "sessions": []})
            e["sessions"].append({"session_key": sx.get("session_key"), "session_name": sx.get("session_name"),
                                  "date_start": sx.get("date_start")})
        meetings_out = sorted(out.values(), key=lambda e: e["date"])
        return {"year": year, "meetings": [m for m in meetings_out if "test" not in (m["name"] or "").lower()],
                "source": source}

    def _vod_meta(self, session: dict, ref: Optional[RefEvents]) -> None:
        """Session identified, its data not loaded yet: the SYNC menu works already (start time,
        time zone for Manual Exact Time / countdown, OpenF1 lap times for L / S). The same
        session_key later in _vod_loaded keeps the anchors."""
        self.sync.initialize(session, ref)
        log.info("VOD: session %s identified (%s %s, start %s, %s)", session.get("session_key"),
                 session.get("meeting_name"), session.get("session_name"), session.get("date_start"),
                 f"{ref.count()} OpenF1 reference events for L/S" if ref and ref.count() else "no OpenF1 lap reference yet")

    def _vod_loaded(self, session: dict, ref: RefEvents) -> None:
        if self._pit_task is not None and not self._pit_task.done():
            self._pit_task.cancel()
        if self.tracks.learn_pitlane:
            self._pit_task = asyncio.create_task(self._pit_offline(session))
        self.timeline.reset()
        self._reset_side_state()
        self.hub.broadcast({"type": "pos_reset"})
        self._session_id = None
        self.sync.initialize(session, ref)

    def set_selected(self, num: Optional[str]) -> None:
        self._selected = num

    def sync_command(self, name: str, arg: Optional[str]) -> Optional[str]:
        """Remote SYNC_* commands. Returns a short toast text."""
        mono, now = time.monotonic(), self._src_now_ms()
        if name == "SYNC_PLUS":
            return self.sync.adjust(self.sync.step, mono, now)
        if name == "SYNC_MINUS":
            return self.sync.adjust(-self.sync.step, mono, now)
        if name == "SYNC_ADJUST":
            try:
                v = float(arg or "")
            except ValueError:
                return None
            if not math.isfinite(v) or abs(v) > 600:
                return None
            return self.sync.adjust(v, mono, now)
        if name == "SYNC_RESYNC":
            return self.sync.resync(mono, now)
        if name == "SYNC_MARK":
            num = self._selected if self._selected in self._order else (self._order[0] if self._order else None)
            label = num or "?"
            drv = (self.feed_state.get("DriverList") or {}).get(num or "", {})
            if isinstance(drv, dict) and drv.get("Tla"):
                label = f"{drv['Tla']}"
            return self.sync.add_event_anchor("lap", mono, now, num, label)
        if name == "SYNC_START":
            return self.sync.add_event_anchor("start", mono, now, None, "START")
        if name == "SYNC_CONFIRM":
            return self.sync.confirm(mono)
        if name == "SYNC_CLEAR":
            return self.sync.clear_anchor()
        if name == "SYNC_PIN":
            return self.sync.add_manual_anchor(None, mono, now)
        if name == "SYNC_KEEP_OLD":
            return self.sync.keep_old()
        if name == "SYNC_USE_NEW":
            return self.sync.use_new()
        return None

    # SYNC menu actions (HTTP API) - each returns {"ok", "result", "state"}
    def sync_action(self, action: str, value: Any = None) -> dict:
        mono, now = time.monotonic(), self._src_now_ms()
        sm = self.sync
        extra: dict = {}
        if action == "capture":
            extra = sm.capture(mono, now)
            text = "captured"
        elif action == "countdown":
            from .sync import parse_countdown
            secs = parse_countdown(str(value or ""))
            if secs is None:
                return {"ok": False, "error": "countdown format: 23:47, 00:23:47 or 23m 47s"}
            text = sm.add_countdown_anchor(secs, mono, now, str(value).strip()[:12])
        elif action == "exact":
            dt = parse_utc(str(value or ""))
            if dt is None:
                return {"ok": False, "error": "f1_time (ISO UTC) expected"}
            text = sm.add_manual_anchor(dt.timestamp() * 1000, mono, now, text=str(value)[:40])
        elif action == "clock":
            # "Q2|remaining|07:32" or "FP2|elapsed|17:43" - the session clock you read on the TV
            from .sync import parse_countdown
            parts = str(value or "").split("|")
            if len(parts) != 3:
                return {"ok": False, "error": "clock: 'PHASE|remaining|MM:SS' or 'PHASE|elapsed|MM:SS' expected"}
            secs = parse_countdown(parts[2])
            if secs is None:
                return {"ok": False, "error": "time format: 07:32, 1:02:03 or 7m 32s"}
            text = sm.add_clock_anchor(parts[0].strip().upper(), parts[1].strip().lower(), secs, mono, now,
                                       parts[2].strip()[:12])
        elif action == "marker":
            text = sm.add_marker_anchor(str(value or "").strip().upper()[:40], mono, now)
        elif action == "auto":
            extra = sm.auto_sync(mono)
            text = extra["message"]
        elif action == "estimate":
            if value in (None, ""):
                text = sm.set_estimate(None)
            else:
                try:
                    lead = float(value)
                except (TypeError, ValueError):
                    return {"ok": False, "error": "lead seconds expected"}
                if not math.isfinite(lead) or abs(lead) > 6 * 3600:
                    return {"ok": False, "error": "lead out of range"}
                text = sm.set_estimate(lead)
        elif action == "clear":
            text = sm.clear_anchor()
        elif action == "keep_old":
            text = sm.keep_old()
        elif action == "use_new":
            text = sm.use_new()
        elif action == "resync":
            text = sm.resync(mono, now)
        elif action == "select_session":
            try:
                key = int(value)
            except (TypeError, ValueError):
                return {"ok": False, "error": "session_key expected"}
            if not self.vod:
                return {"ok": False, "error": "only for recordings (VOD mode)"}
            self.select_session(key)
            text = f"Manual session selected ({key}) - SYNC REQUIRED"
        else:
            return {"ok": False, "error": "unknown action"}
        failed = text.startswith("SYNC:") if isinstance(text, str) else False
        if self.vod and action in ("countdown", "exact", "estimate", "use_new", "resync", "clock", "marker"):
            self._log_data_check(action, text)
        self.hub.set_sync(self.sync_status())
        return {"ok": not failed, "result": text, **extra, "state": self.sync_status()}

    def _log_data_check(self, action: str, text: Any) -> None:
        """One diagnostic line after a sync action: where the video now points vs the loaded data."""
        from .sync import utc_str
        sm, mono = self.sync, time.monotonic()
        off = sm.mapping.offset if sm.mapping is not None else None
        tgt = (sm.clock.pb_at(mono) + off) * 1000 if off is not None and sm.clock.last is not None else None
        span = self.source.span()
        log.info("SYNC CHECK (%s): %s | video -> %s | session data %s | source %s (%s)",
                 action, text, f"{utc_str(tgt)[:12]} UTC" if tgt is not None and math.isfinite(tgt) else "no time",
                 f"{utc_str(span[0])[:8]}-{utc_str(span[1])[:8]} UTC" if span else "NOT LOADED",
                 self.source.state, self.source.reason)

    def sync_status(self) -> dict:
        st = self.sync.status(time.monotonic(), self._src_now_ms(), self.timeline)
        if not self.vod and self.source.mode == "live":
            # a VOYO *recording* followed in LIVE mode: live timing only keeps the last minutes,
            # so a time hours/days ago can never have data - say so instead of an empty board
            tgt = self._target_ms
            behind = (self._src_now_ms() - tgt) / 1000 if math.isfinite(tgt) else None
            if behind is not None and behind > 3600 and self.sync.clock.last is not None:
                st["flags"].append("RECORDING_IN_LIVE_MODE")
                st["modeNote"] = (f"The video is {behind / 3600:.1f} h behind live - it is a recording, but the "
                                  "dashboard runs in LIVE mode (live timing has no old data). Close everything and "
                                  "start with launch.bat vod.")
                if not getattr(self, "_live_rec_logged", False):
                    self._live_rec_logged = True
                    log.warning("RECORDING IN LIVE MODE: the VOYO video is %.1f h behind live - restart with "
                                "'launch.bat vod' (python main.py --vod)", behind / 3600)
            else:
                self._live_rec_logged = False
        if self.vod:
            st["media"] = self.media_state()
            span = self.source.span()
            tgt = self._target_ms
            if span:
                st["dataSpan"] = [round(span[0]), round(span[1])]        # epoch ms (F1 time)
                st["dataShownMs"] = round(tgt) if math.isfinite(tgt) else None
                st["dataDrivers"] = len((self.feed_state.get("DriverList") or {}))
                st["dataTimingLines"] = len(((self.feed_state.get("TimingData") or {}).get("Lines") or {}))
            if span and st.get("synced") and math.isfinite(tgt) and (tgt < span[0] - 60_000 or tgt > span[1] + 60_000):
                # the sync points outside the recorded session: say it instead of showing an empty board
                from .sync import utc_str
                st["flags"].append("OUTSIDE_SESSION_DATA")
                st["dataNote"] = (f"The video maps to {utc_str(tgt)[:8]} UTC, but the session data covers "
                                  f"{utc_str(span[0])[:5]}–{utc_str(span[1])[:5]} UTC. Check the sync "
                                  "(time zone, 24/12-hour) or the session.")
        return st

    # ------------------------------------------------------------------
    def order(self) -> list[str]:
        return list(self._order)

    def _push_status(self) -> None:
        delay = self.sync.delay if self.sync.active == "DELAY" else 0
        extra = {}
        auth = getattr(self.source, "auth", None)
        if auth is not None:
            # state / product only - never the token
            info = auth.public_info()
            extra["f1tv"] = {"subscription": info["subscription"], "state": info["state"], "product": info["product"]}
        self.hub.set_status({**self._status, **extra, "delay": delay, "next_session": self._schedule})

    def _reset_side_state(self) -> None:
        log.info("Resetting session state")
        self.outline_learner = OutlineLearner()
        self._ref_samples = []
        self.availability.positions = False
        self.availability.car_data = False
        self.availability.positions_source = None
        self.availability.car_data_source = None
        self.availability.positions_age_s = None
        self._session_id = None
        self._in_pit.clear()
        self._laps.clear()
        self.hub.reset_diff()
        self._dirty = True

    def _parse_error(self, topic: str) -> None:
        now = time.monotonic()
        if now - self._parse_errors.get(topic, 0) > 30:        # at most one log line per topic / 30 s
            self._parse_errors[topic] = now
            log.exception("Parser error in topic %s (further errors suppressed for 30 s)", topic)

    def _on_topic(self, topic: str) -> None:
        """Called by the timeline whenever a topic is applied to the shown state."""
        if topic in ("SessionInfo", "SessionStatus"):
            self._check_session()

    def _check_session(self) -> None:
        si = self.feed_state.get("SessionInfo") or {}
        sid = (si.get("Key"), si.get("Path"))
        if sid == (None, None):
            return
        if self._session_id is not None and sid != self._session_id:
            log.info("Session changed: %s -> %s", self._session_id, sid)
            self.timeline.clear_state(keep=("SessionInfo",))
            if not self.timeline.replaying:
                self._reset_side_state()
        if sid != self._session_id:
            self._session_id = sid
            meeting = (si.get("Meeting") or {}).get("Name")
            log.info("Session: %s - %s (%s) status=%s", meeting, si.get("Name"), si.get("Path"),
                     si.get("SessionStatus"))
            rec = getattr(self.source, "recorder", None)
            if rec:
                rec.set_session(si.get("Path"), f"{meeting} {si.get('Name')}")
        circuit = (si.get("Meeting") or {}).get("Circuit") or {}
        path = si.get("Path") or ""
        year = int(path[:4]) if path[:4].isdigit() else None
        tid = (circuit.get("Key"), year)
        if tid != self._track_id and tid[0] is not None:
            self._track_id = tid
            if self._track_task and not self._track_task.done():
                self._track_task.cancel()
            self._track_task = asyncio.create_task(self._load_track(tid[0], year, circuit.get("ShortName")))

    async def _load_track(self, key: int, year: Optional[int], name: Optional[str], rebuild: bool = False) -> None:
        override = getattr(self.source, "geometry", None)
        if override is not None:
            geo = override
        else:
            geo = await self.tracks.load(key, year, name)
        self.geometry = geo
        # (a rebuild of the outline never restarts or resets the pit-lane learning)
        if geo is not None and not self.vod and not rebuild:
            self._pit_start_learning(geo, key, year, name)
        if not self.vod and not rebuild:
            # the pit lane is circuit geometry: load it now (cache, else the F1 archive) - live
            # pit stops of this session only validate it
            self._pit_seed_maybe(key, year, name or (geo.name if geo else ""), geo)
        last = getattr(self, "_pit_last", None)
        if geo is not None and self.vod and last and last[0] == key:
            geo.pitlane_info = {**(geo.pitlane_info or {}), **last[2], "state": last[1]}
        self.hub.set_track(geo.to_dict() if geo else None)
        self._log_map_report("track loaded")

    # ------------------------------------------------------------------ pit lane (server/pitlane.py)
    def _pit_publish(self, key: Optional[int], state: str, rec: Optional[Reconstruction] = None,
                     passes: Optional[list] = None, variant: Optional[dict] = None,
                     cached: Optional[dict] = None, note: str = "") -> None:
        """Status for the map legend + details for the debug overlay (G)."""
        passes = passes or []
        acc = [t for t in passes if t.accepted]
        extra = {"passes": len(acc), "rejected": len(passes) - len(acc), "note": note}
        self._pit_last = (key, state, extra)          # re-applied when the circuit outline loads later
        geo = self.geometry
        if geo is not None and geo.circuit_key == key:
            if variant is not None:
                self.tracks.attach_pitlane(geo, None, variant, state, extra)
            else:
                geo.pitlane_info = {**(geo.pitlane_info or {}), "state": state, **extra}
            self.hub.set_track(geo.to_dict())

        def thin(pts: list, k: int = 2) -> list:
            return [[round(p[0]), round(p[1])] for p in pts[::k]]
        meta = getattr(self, "_pit_meta", {}) or {}
        shown = variant or cached or {}
        cl = rec.centerline if rec else shown.get("centerline")
        self.hub.set_pit_debug({
            "circuit": key, "circuit_name": meta.get("name") if meta.get("key") == key else None,
            "season": meta.get("year") if meta.get("key") == key else None,
            "cache": {k: shown.get(k) for k in ("status", "confidence", "id", "seasons", "traversals",
                                                "valid_until", "source")} if shown else None,
            "entry": list(cl[0]) if cl else None, "exit": list(cl[-1]) if cl else None,
            "state": state, "note": note,
            "confidence": rec.confidence if rec else (variant or cached or {}).get("confidence"),
            "spread_m": rec.spread_m if rec else None,
            "used": len(rec.used) if rec else 0,
            "centerline": [list(p) for p in rec.centerline] if rec else None,
            "cached": (cached or variant or {}).get("centerline"),
            "pit_in_frac": rec.pit_in_frac if rec else (variant or {}).get("pit_in_frac"),
            "pit_out_frac": rec.pit_out_frac if rec else (variant or {}).get("pit_out_frac"),
            "passes": [{"num": t.num, "accepted": t.accepted, "reason": t.reason, "notes": t.notes,
                        "how": t.how, "raw": thin(t.raw), "path": thin(t.pts, 1)}
                       for t in passes[-40:] if t.num != "cache"],
        })

    def _pit_needs(self, key: Optional[int], year: Optional[int]) -> tuple[bool, Optional[dict]]:
        """Cache-first: reconstruct only when nothing verified is cached for this season."""
        return needs_reconstruction(self.tracks.pitcache, key, year)

    async def _pit_apply(self, key: int, year: Optional[int], name: str, session_key: Optional[int],
                         passes: list, cached: Optional[dict], source: str, prior: Optional[dict] = None) -> None:
        """Passes -> reconstruction -> cache (never over a different cached layout) -> map.
        ``prior``: the provisional variant that existed before this session - its stored passes
        are combined with the new ones (passes stored during this session are not counted twice)."""
        pool = list(passes)
        if prior and prior.get("status") == "provisional":
            pool += traversals_from_samples(self.tracks.pitcache.samples(key, prior.get("id")))
        have_track = self.geometry is not None or self._pit_ctx.get("have_track", False)
        # geometry work (0.1-0.5 s for many passes) never on the event loop: no lag spike on the TV
        rec = await asyncio.to_thread(reconstruct, pool, have_track)
        acc = [t for t in passes if t.accepted]
        if rec is None or not rec.drawable:
            why = rec.note if rec else ("no complete pass through the pit lane yet" if not passes else
                                        f"{len(acc)} usable of {len(passes)} passes")
            state = "cached" if cached else "learning"
            log.info("Pit lane circuit %s: not reliable yet (%s)%s", key, why,
                     " - keeping the cached one" if cached else "")
            self._pit_publish(key, state, rec, passes, cached, cached, why)
            return
        variant, what = self.tracks.pitcache.store(key, name, year, session_key, rec, source)
        log.info("Pit lane circuit %s: %s (%s, %s)", key, what, rec.confidence, rec.note)
        self._pit_publish(key, "cached" if what.startswith("confirmed") else "reconstructed",
                          rec, passes, variant, cached, what)

    async def _pit_offline(self, session: dict) -> None:
        """VOD: the whole session is loaded - find every pit-lane pass at once (in a thread)."""
        src = self.source
        events = src.events
        si = next((e.data for e in events if e.topic == "SessionInfo" and isinstance(e.data, dict)
                   and (e.data.get("Meeting") or {}).get("Circuit")), None) or {}
        circuit = (si.get("Meeting") or {}).get("Circuit") or {}
        key = circuit.get("Key") or session.get("circuit_key")
        if key is None:
            return
        key = int(key)
        path = si.get("Path") or ""
        year = int(path[:4]) if path[:4].isdigit() else (int(str(session.get("date_start"))[:4])
                                                         if str(session.get("date_start"))[:4].isdigit() else None)
        name = circuit.get("ShortName") or session.get("location") or ""
        self._pit_meta = {"key": key, "name": name, "year": year}
        need, cached = self._pit_needs(key, year)
        if not need:
            log.info("Pit lane circuit %s: cached (%s, %s passes) - no reconstruction needed", key,
                     cached.get("confidence"), cached.get("traversals"))
            self._pit_publish(key, "cached", None, [], cached, cached, "cached")
            return
        self._pit_publish(key, "learning", None, [], cached, cached, "reading pit-lane passes of this session")
        geo = self.geometry if self.geometry is not None and self.geometry.circuit_key == key else \
            await self.tracks.load(key, year, name)
        self._pit_ctx = {"key": key, "have_track": geo is not None}
        passes = await asyncio.to_thread(collect_from_events, events, geo.points if geo else None)
        if src.events is not events:
            return                                   # another session was loaded meanwhile
        log.info("Pit lane circuit %s: %d complete pass(es) found in the session, %d usable", key,
                 len(passes), sum(t.accepted for t in passes))
        await self._pit_apply(key, year, name, session.get("session_key"), passes, cached,
                              "F1 position data (session archive)", prior=cached)

    def _pit_start_learning(self, geo: TrackGeometry, key: int, year: Optional[int], name: Optional[str]) -> None:
        """Live / replay: learn from the passes as they happen - only if the cache needs it."""
        if not self.tracks.learn_pitlane or geo.source == "test":
            self.pit_collector = None
            return
        self._pit_meta = {"key": key, "name": name or geo.name, "year": year}
        need, cached = self._pit_needs(key, year)
        if not need:
            self.pit_collector = None
            log.info("Pit lane circuit %s: cached (%s) - drawn at once", key, cached.get("confidence"))
            self._pit_publish(key, "cached", None, [], cached, cached, "cached - no reconstruction needed")
            return
        self.pit_collector = PitLaneCollector(geo.points)
        self._pit_ctx = {"key": key, "year": year, "name": name or geo.name, "cached": cached, "prior": cached,
                         "have_track": True}
        if cached is None:
            geo.pitlane_info = {"state": "learning", "passes": 0}
        log.info("Pit lane circuit %s: %s - learning from pit-lane passes", key,
                 "provisional cache" if cached else "not cached yet")

    def _pit_position(self, entry: dict) -> None:
        if self.pit_collector is not None:
            self.pit_collector.position_entry(entry)

    def _pit_poll(self) -> Optional[asyncio.Task]:
        """Called every 2 s: new complete passes -> reconstruction in the background."""
        col = self.pit_collector
        if self._pit_task is not None and not self._pit_task.done():
            return None                                   # the previous one is still running
        new = col.poll(self.timeline.latest_event_ms)
        if not new:
            return None
        for t in new:
            log.info("Pit lane pass car %s: %s", t.num, "usable" if t.accepted else f"rejected - {t.reason}")
        self._pit_task = asyncio.get_event_loop().create_task(self._pit_live_apply(list(col.traversals)))
        return self._pit_task

    def _pit_seed_maybe(self, key: int, year: Optional[int], name: str, geo: Optional[TrackGeometry]) -> None:
        if not self.tracks.learn_pitlane or self.source.mode == "test" or (geo is not None and geo.source == "test"):
            return
        cached = self.tracks.pitcache.lookup(key, year)
        if cached is not None and cached.get("status") == "verified":
            return                                  # drawn from the cache at once
        if self._seed_task is not None and not self._seed_task.done() and self._seed_key == key:
            return
        self._seed_key = key
        self._pit_meta = {"key": key, "name": name, "year": year}
        self._seed_task = asyncio.get_event_loop().create_task(self._pit_seed(key, year, name, geo, cached))

    async def _pit_seed(self, key: int, year: Optional[int], name: str, geo: Optional[TrackGeometry],
                        cached: Optional[dict]) -> None:
        si = self.feed_state.get("SessionInfo") or {}
        self._pit_status = {"key": key, "state": "loading", "note": "loading the pit lane from the F1 archive"}
        if cached is None:
            self._pit_publish(key, "loading", None, [], None, None, "loading the pit lane from the F1 archive")

        def progress(text: str) -> None:
            self._pit_status = {"key": key, "state": "loading", "note": text}
        try:
            passes, sess, note = await seed_pitlane(key, year, geo.points if geo else None,
                                                    DATA_DIR / "archive_cache", exclude_path=si.get("Path"),
                                                    progress=progress)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            passes, sess, note = None, None, f"{type(exc).__name__}: {exc}"
        if self._track_id is None or self._track_id[0] != key:
            return                                  # another circuit meanwhile
        if passes is None:
            now = self.tracks.pitcache.lookup(key, year)
            self._pit_status = {"key": key, "state": "cached" if now else "unavailable", "note": note}
            if now is None:
                log.warning("Pit lane geometry unavailable for circuit %s: %s", key, note)
                self._pit_publish(key, "unavailable", None, [], None, None, f"unavailable: {note}")
            return
        await self._pit_apply(key, sess["year"], name, None, passes, cached, f"F1 archive: {sess['name']} {sess['year']}")
        now = self.tracks.pitcache.lookup(key, year)
        self._pit_status = {"key": key, "state": "cached" if now else "unavailable", "note": note}
        if self._pit_ctx.get("key") == key:
            self._pit_ctx["cached"] = now           # live passes now validate this one
        self._log_map_report("pit lane loaded")

    async def _pit_live_apply(self, passes: list) -> None:
        ctx = self._pit_ctx
        known = self.tracks.pitcache.lookup(ctx["key"], ctx.get("year"))
        if known is not None and known.get("status") == "verified":
            # a verified pit lane is circuit geometry: live passes only validate it, never replace it
            new = [t for t in passes if t.accepted and t.num != "cache"][-5:]
            for t in new:
                dev = deviation_from(known, t)
                ok = dev is not None and dev <= PIT_VALIDATE_M
                self._pit_validation = {"passes": self._pit_validation.get("passes", 0) + 1,
                                        "agree": self._pit_validation.get("agree", 0) + (1 if ok else 0),
                                        "last_dev_m": None if dev is None else round(dev, 1)}
                log.info("Pit lane circuit %s: live pass of car %s %s the loaded pit lane (deviation %s m)",
                         ctx["key"], t.num, "matches" if ok else "does NOT match",
                         "?" if dev is None else f"{dev:.1f}")
            self.pit_collector.traversals.clear()
            return
        si = self.feed_state.get("SessionInfo") or {}
        await self._pit_apply(ctx["key"], ctx.get("year"), ctx.get("name") or "", si.get("Key"), passes,
                              ctx.get("cached"), "F1 position data (live)", prior=ctx.get("prior"))
        ctx["cached"] = self.tracks.pitcache.lookup(ctx["key"], ctx.get("year"))
        need, _ = self._pit_needs(ctx["key"], ctx.get("year"))
        if not need:
            log.info("Pit lane circuit %s: verified - learning stops", ctx["key"])
            self.pit_collector = None

    # ------------------------------------------------------------------ learning
    def _learn(self, sample: dict) -> None:
        if self.source.mode == "test":
            return
        geo = self.geometry
        if not (self.tracks.learn_outline and self._track_id and self._track_id[0] is not None):
            return
        # positions of cars on the track (not in the pit lane): what any drawn outline is checked
        # against, and what a known layout is fitted onto
        for num, x, y, on_track in sample["cars"]:
            if on_track and not self._in_pit.get(num, False):
                self._ref_samples.append((x, y))
        if len(self._ref_samples) > 24000:
            del self._ref_samples[::2]              # keep the spread, halve the count
        if geo is None:
            for num, x, y, _ in sample["cars"]:
                self.outline_learner.observe(num, x, y, self._laps.get(num), self._in_pit.get(num, False))
            if self.outline_learner.result:
                pts = self.outline_learner.result
                why = outline_problem(pts)
                if why:
                    # a lap with holes in the position stream: drawn it would miss part of the track
                    log.info("Learned lap outline rejected (%s) - learning another lap", why)
                    self.outline_learner = OutlineLearner()
                    return
                if "learned" in self.tracks.rejected(self._track_id[0]):
                    return
                self.geometry = self.tracks.save_outline(self._track_id[0], self._circuit_name(), pts)
                self.tracks.attach_pitlane(self.geometry, self._track_id[1])
                self.hub.set_track(self.geometry.to_dict())

    def _circuit_name(self) -> str:
        si = self.feed_state.get("SessionInfo") or {}
        return ((si.get("Meeting") or {}).get("Circuit") or {}).get("ShortName") or ""

    def _ref_hint(self) -> Optional[str]:
        si = self.feed_state.get("SessionInfo") or {}
        meeting = si.get("Meeting") or {}
        return reference_id(self._circuit_name(), meeting.get("Location"), meeting.get("Name"))

    def _ref_poll(self, mono: float) -> None:
        """The outline is kept honest by the car positions (worker thread, every 20 s at most):

        * a circuit you chose yourself: that known layout is fitted onto the positions;
        * no outline / only a learned lap: the known layout of the circuit is fitted - if the
          name is unknown or its layout does not fit, every bundled layout is tried;
        * any other outline (MultiViewer, a fitted layout) is checked every minute: when the
          cars drive where it has no track (part missing, another layout), it is replaced by
          the known layout that fits.
        The pit lane is never touched."""
        if self._track_id is None or self._track_id[0] is None:
            return
        if (self._ref_task is not None and not self._ref_task.done()) or mono - self._ref_last < 20:
            return
        if len(self._ref_samples) < MIN_SAMPLES:
            return
        key = self._track_id[0]
        geo = self.geometry
        choice = self.tracks.choice(key)
        if choice:
            if geo is not None and geo.source == "reference" and (geo.info or {}).get("ref_id") == choice \
                    and not (geo.info or {}).get("unaligned"):
                return
            job = ("choice", choice)
        elif geo is None or geo.source == "learned":
            if "reference" in self.tracks.rejected(key):
                return
            hint = self._ref_hint()
            if hint is None:
                return                              # the session does not say which layout: never guessed
            job = ("identify", hint)
        elif geo.source in ("multiviewer", "reference"):
            if mono - self._check_last < 60:
                return
            self._check_last = mono
            job = ("check", None)
        else:
            return
        self._ref_last = mono
        self._ref_task = asyncio.get_event_loop().create_task(self._ref_job(job, list(self._ref_samples)))

    async def _ref_job(self, job: tuple, samples: list) -> None:
        kind, arg = job
        key = self._track_id[0] if self._track_id else None
        if key is None:
            return
        if kind == "check":
            geo = self.geometry
            if geo is None:
                return
            chk = await asyncio.to_thread(check_outline, geo.points, samples)
            if chk.ok is not False:
                if geo.info.get("check") != chk.reason and chk.ok:
                    geo.info = {**geo.info, "check": chk.reason}
                return
            hint = self._ref_hint()
            log.warning("Track map of circuit %s (%s) does not match the cars: %s - %s", key, geo.source, chk.reason,
                        f"fitting the known layout {hint} of this circuit" if hint else
                        "no known layout for this circuit name (choose it with CHOOSE CIRCUIT)")
            if hint is None:
                self._ref_note = f"the drawn track does not match the cars: {chk.reason}"
                return
            rid, fit = hint, await asyncio.to_thread(fit_reference, reference_points(hint), samples)
            note = f"replaced the {geo.source} outline: {chk.reason}"
        elif kind == "choice":
            ref = reference_points(arg)
            if ref is None:
                return
            fit = await asyncio.to_thread(fit_reference, ref, samples)
            rid, note = arg, "the circuit you chose"
        else:
            ref = reference_points(arg)
            if ref is None:
                return
            rid, fit = arg, await asyncio.to_thread(fit_reference, ref, samples)
            note = "known layout of this circuit (from the session's circuit name)"
        if self._track_id is None or self._track_id[0] != key:
            return                                  # another circuit meanwhile
        if not fit.ok or rid is None:
            msg = f"{rid or arg or 'no known layout'}: {fit.reason}"
            if self._ref_note != msg:
                self._ref_note = msg
                log.info("Track layout for circuit %s: %s", key, msg)
            return
        if kind == "check" and self.geometry is not None and self.geometry.source == "reference" and \
                (self.geometry.info or {}).get("ref_id") == rid:
            return                                  # the same fit again: nothing better found
        self.geometry = self.tracks.save_reference(key, self._circuit_name(), rid, fit)
        self.geometry.year = self._track_id[1]
        self.geometry.info["note"] = note
        self.tracks.attach_pitlane(self.geometry, self._track_id[1])     # (read only: the cached pit lane)
        self._ref_note = None
        log.info("Track map of circuit %s: known layout %s (%s; %s)", key, rid, fit.reason, note)
        self.hub.set_track(self.geometry.to_dict())

    def circuit_identity(self) -> dict:
        """Which circuit this is - from the session's own metadata only (F1's Meeting.Circuit.Key,
        circuit short name, location, season); ``layout`` = the matching known layout id."""
        si = self.feed_state.get("SessionInfo") or {}
        meeting = si.get("Meeting") or {}
        circuit = meeting.get("Circuit") or {}
        key, year = self._track_id if self._track_id else (circuit.get("Key"), None)
        return {"circuit_key": key, "name": circuit.get("ShortName"), "meeting": meeting.get("Name"),
                "location": meeting.get("Location"), "season": year, "layout": self._ref_hint(),
                "chosen": self.tracks.choice(key) if key is not None else None}

    def map_report(self) -> dict:
        """Is the map right? Track and pit-lane geometry present, and do the live car positions
        fall on them (same F1 coordinate system for track, pit lane, cars and safety car)."""
        geo = self.geometry
        ident = self.circuit_identity()
        rep: dict = {"identity": ident}
        if geo is None:
            rep["track"] = {"ok": False, "detail": "NOT AVAILABLE" + (f" ({self._ref_note})" if self._ref_note else "")}
        else:
            src = {"multiviewer": f"MultiViewer {geo.year or ''}".strip(), "learned": "learned from one lap",
                   "test": "test circuit"}.get(geo.source, f"known layout {(geo.info or {}).get('ref_id')} fitted to the cars")
            rep["track"] = {"ok": True, "detail": f"{src} ({len(geo.points)} points)", "source": geo.source,
                            "rotation_deg": geo.rotation}
        pit = geo.pitlane if geo is not None else None
        pinfo = (geo.pitlane_info or {}) if geo is not None else {}
        status = self._pit_status if self._pit_status.get("key") == ident.get("circuit_key") else {}
        if pit:
            rep["pit_lane"] = {"ok": True, "detail": f"{pinfo.get('source') or 'cache'} - {pinfo.get('confidence')}, "
                                                     f"{pinfo.get('traversals')} passes",
                               "validation": dict(self._pit_validation)}
        else:
            rep["pit_lane"] = {"ok": False, "detail": "NOT AVAILABLE - " + (status.get("note") or pinfo.get("note")
                                                                         or "no pit-lane geometry for this circuit yet")}
        # positions vs geometry
        drivers = self._driver_numbers()
        now = self._now_pres_ms()
        fresh = {k: v for k, v in self.positions.freshness(now).items() if v["fresh"] and k in drivers}
        if geo is None or not fresh:
            rep["positions"] = {"ok": None, "detail": "no geometry" if geo is None else "no fresh Position.z data",
                                "mapped": 0, "with_position": len(fresh), "drivers": len(drivers)}
        else:
            pts = _densify(geo.points) + (_densify(pit) if pit else [])
            grid = _Grid([tuple(p) for p in pts])
            dist = {k: grid.nearest(*self.positions.latest[k][1:3])[0] / 10 for k in fresh}
            mapped = [k for k, d in dist.items() if d <= 50]
            off = sorted((d, k) for k, d in dist.items() if d > 50)
            ds = sorted(dist.values())
            ok = len(mapped) == len(dist)
            rep["positions"] = {"ok": ok, "mapped": len(mapped), "with_position": len(dist), "drivers": len(drivers),
                                "median_m": round(ds[len(ds) // 2], 1),
                                "detail": (f"OK - median {ds[len(ds) // 2]:.1f} m from the track" if ok else
                                           f"{len(off)} car(s) more than 50 m off the drawn track/pit lane: " +
                                           ", ".join(f"#{k} {d:.0f} m" for d, k in off[:5]))}
        sc = self.map_info()["safety_car"]
        rep["safety_car"] = {"ok": sc.get("available"), "detail": "Position.z key " + str(sc.get("key"))
                             if sc.get("available") else sc.get("reason")}
        return rep

    def _log_map_report(self, why: str = "") -> None:
        try:
            r = self.map_report()
        except Exception:  # noqa: BLE001
            self._parse_error("map-report")
            return
        i = r["identity"]
        p = r["positions"]
        lines = [f"Track: {i.get('meeting') or '?'} - {i.get('name') or '?'} (circuit_key {i.get('circuit_key')}, "
                 f"season {i.get('season')}, layout {i.get('layout') or 'unknown'}"
                 + (f", your choice {i['chosen']}" if i.get("chosen") else "") + ")" + (f" [{why}]" if why else ""),
                 f"Track geometry: {'OK - ' if r['track']['ok'] else ''}{r['track']['detail']}",
                 f"Pit lane geometry: {'OK - ' if r['pit_lane']['ok'] else ''}{r['pit_lane']['detail']}",
                 f"Position mapping: {p['detail']}",
                 f"Drivers mapped: {p['mapped']}/{p['with_position']} with a fresh position ({p['drivers']} drivers)",
                 f"Safety car position: {'OK - ' if r['safety_car']['ok'] else 'not available - '}{r['safety_car']['detail']}"]
        for line in lines:
            log.info("%s", line)

    def set_track_choice(self, ref_id: Optional[str]) -> str:
        """Choose the circuit layout yourself (an id of known_layouts()) or "auto"."""
        if self._track_id is None or self._track_id[0] is None:
            return "No circuit known yet"
        key, year = self._track_id
        if ref_id in (None, "", "auto"):
            self.tracks.set_choice(key, None)
            self._ref_last = 0.0
            if self._track_task and not self._track_task.done():
                self._track_task.cancel()
            self._track_task = asyncio.get_event_loop().create_task(
                self._load_track(key, year, self._circuit_name(), rebuild=True))
            return "Track layout: automatic"
        if ref_id not in {e["id"] for e in known_layouts()}:
            return "Unknown layout"
        self.tracks.set_choice(key, ref_id)
        self._ref_last = 0.0                        # fit it at the next poll
        self._ref_note = None
        n = len(self._ref_samples)
        if n < MIN_SAMPLES:
            # no car positions to fit it onto yet: show the layout as it is (north up, no cars on it)
            ref = reference_points(ref_id)
            if ref:
                self.geometry = TrackGeometry(key, self._circuit_name(), year, "reference", [list(p) for p in ref],
                                              info={"ref_id": ref_id, "chosen": True, "unaligned": True,
                                                    "note": "not aligned yet - waiting for car positions"})
                self.hub.set_track(self.geometry.to_dict())
        return (f"Track layout {ref_id} chosen - fitted onto the car positions "
                + ("now" if n >= MIN_SAMPLES else f"as soon as enough positions are in ({n}/{MIN_SAMPLES})"))

    def track_choices(self) -> dict:
        geo = self.geometry
        key = self._track_id[0] if self._track_id else None
        return {"circuit_key": key, "circuit": self._circuit_name(), "choice": self.tracks.choice(key) if key else None,
                "source": geo.source if geo else None, "info": dict(geo.info) if geo else {},
                "note": self._ref_note, "layouts": known_layouts()}

    def track_report(self) -> str:
        """"Track map wrong": the first request arms it, a second one within 8 s rebuilds the
        outline of this circuit (cached outline deleted, the source shown rejected for it). The
        pit lane - its cache and its learning - is not touched."""
        if self._track_id is None or self._track_id[0] is None:
            return "No circuit known yet"
        now = time.monotonic()
        if now - self._report_armed > 8:
            self._report_armed = now
            return "Track map wrong? Press again within 8 s to rebuild it (the pit lane is kept)"
        self._report_armed = 0.0
        key, year = self._track_id
        src = self.geometry.source if self.geometry is not None else None
        self.tracks.reset_circuit(key, src)
        self.geometry = None
        self.outline_learner = OutlineLearner()
        self._ref_note = None
        self._ref_last = 0.0
        self.hub.set_track(None)
        if self._track_task and not self._track_task.done():
            self._track_task.cancel()
        self._track_task = asyncio.get_event_loop().create_task(
            self._load_track(key, year, self._circuit_name(), rebuild=True))
        return (f"Track map of {self._circuit_name() or key} cleared ({src or 'none'} rejected) - rebuilding "
                "from the car positions; the pit lane is kept")

    # ------------------------------------------------------------------ time helpers
    def _target_dt(self) -> datetime:
        if math.isfinite(self._target_ms):
            return datetime.fromtimestamp(self._target_ms / 1000, timezone.utc)
        if self.source.mode == "live":
            return datetime.fromtimestamp(self._live_f1_now_ms() / 1000, timezone.utc)
        return self.source.now()

    def _pres_ms(self) -> float:
        """Target on the browser's presentation time base (what pos samples use)."""
        return float(self.source.map_time(self._target_dt()))

    # ------------------------------------------------------------------ loops
    def tick(self) -> None:
        """One sync step: pick the target time, bring the state there, stream positions."""
        mono = time.monotonic()
        prev_target, prev_rate = self._target_ms, self._rate
        tgt = self.sync.target(mono, self._src_now_ms())
        target_ms = tgt.ms
        if target_ms is None and self.vod:
            # UNSYNCED recording: no time -> no session data on screen (nothing is guessed, and the
            # large download waits for the sync too - see VodSource.waiting_for_sync)
            if self.timeline.events or self.feed_state.get("SessionInfo"):
                self.timeline.reset()
                self.source.invalidate()
                self._reset_side_state()
                self.hub.broadcast({"type": "pos_reset"})
            target_ms = -INF
        elif target_ms is None:
            target_ms = prev_target if math.isfinite(prev_target) else -INF
        elif self.vod and self.source.waiting_for_sync:
            log.info("VOD: the video has a time now - loading the session data")
            self.source.allow_load()
        restored = False
        if self.vod and math.isfinite(target_ms):
            restored = self.source.ensure(target_ms, self._vod_ingest, self.timeline)
        # a forward jump (seek, resync) larger than 3 s is a discontinuity for the map too
        expected = prev_target + TICK * 1000 * max(prev_rate, 0) * 1.5
        # (switching VOYO -> DELAY after a lost clock is continuous and is no jump)
        jump = tgt.jump or restored or (math.isfinite(target_ms) and math.isfinite(prev_target)
                                        and target_ms > expected + 3000)
        res = self.timeline.advance(target_ms, self.pos_lookahead_ms)
        if restored:
            res.discontinuity = True
        self._target_ms, self._rate = target_ms, tgt.rate
        if res.state_changed:
            self._dirty = True
        if res.telemetry_changed:
            self._tel_dirty = True
        if tgt.rate != prev_rate:
            self._dirty = True          # session clock extrapolation speed changed (pause/play)
        if res.discontinuity or (jump and math.isfinite(target_ms)):
            refill = (res.replay_positions + res.released_positions) if res.discontinuity else \
                [r for r in res.released_positions if r[0] >= target_ms - 6000]
            self.hub.broadcast({"type": "pos_reset"})
            for _, sample, _origin in refill:
                self.hub.broadcast({"type": "pos", **sample})
            self._send_clock(force=True)
            self._dirty = True
            self._tel_dirty = True
        else:
            for _, sample, _origin in res.released_positions:
                self.hub.broadcast({"type": "pos", **sample})
                self._learn(sample)
        self._send_clock()

    def _vod_ingest(self, topic: str, data: Any, event_ms: float, snapshot: bool) -> None:
        self._ingest(topic, data, event_ms, snapshot, "feed", event_ms)

    def _send_clock(self, force: bool = False) -> None:
        """Presentation clock for the map: position t values are on this time base."""
        mode = self.sync.active
        if mode == "LIVE":
            key = ("LIVE",)
            if force or key != self._clock_sent:
                self._clock_sent = key
                self.hub.broadcast({"type": "clock", "mode": "LIVE"})
            return
        rate = self._rate / max(self.source.speed, 1e-6)       # presentation ms per wall ms
        key = (mode, round(rate, 3), int(time.monotonic()))      # refresh once per second
        if force or key != self._clock_sent:
            self._clock_sent = key
            self.hub.broadcast({"type": "clock", "mode": mode, "t": round(self._pres_ms()),
                                "rate": round(rate, 4), "wall": int(time.time() * 1000)})

    async def publish_loop(self) -> None:
        last_tel = last_sync = 0.0
        while True:
            await asyncio.sleep(TICK)
            try:
                self.tick()
            except Exception:  # noqa: BLE001
                self._parse_error("timeline")
            now = time.monotonic()
            self._check_feed_age(now)
            if (self._dirty and now - self._last_full >= STATE_INTERVAL) or now - self._last_full > 5:
                self._dirty = False
                self._last_full = now
                if self.positions.latest:
                    newest = max(v[0] for v in self.positions.latest.values())
                    ref = self._pres_ms() if math.isfinite(self._target_ms) else (
                        self._live_f1_now_ms() if self.source.mode == "live" else time.time() * 1000)
                    self.availability.positions_age_s = round(max(0.0, (ref - newest) / 1000), 1)
                try:
                    # session structure (Q1/Q2/Q3, clock, markers) for the phase label and timeline
                    self.normalizer.timeline = self.sync.timeline()
                    state = self.normalizer.build(self.feed_state, self._target_dt(), self._rate,
                                                  self.availability, rc_until=self._rc_until())
                except Exception:  # noqa: BLE001
                    self._parse_error("normalizer")
                    continue
                self._order = state["order"]
                for num, d in state["drivers"].items():
                    self._in_pit[num] = bool(d["in_pit"])
                    self._laps[num] = d["laps"]
                try:
                    state["map"] = self.map_info()
                except Exception:  # noqa: BLE001 - map extras are optional, the board is not
                    self._parse_error("map")
                self.diag.session_running(bool(state["session"].get("live")), now)
                try:
                    self.hub.publish_state(state)
                except Exception:  # noqa: BLE001 - never let one state end the publishing for good
                    self._parse_error("publish")
            if self._tel_dirty and now - last_tel >= TEL_INTERVAL:
                self._tel_dirty = False
                last_tel = now
                year = None
                path = (self.feed_state.get("SessionInfo") or {}).get("Path") or ""
                if path[:4].isdigit():
                    year = int(path[:4])
                # one object per car: only what CarData.z carries, with its age (old data is
                # flagged, then hidden - never shown as current); refreshed at least every second
                try:
                    self.hub.broadcast({"type": "tel", "cars": self.cardata.objects(year, self._now_pres_ms())})
                except Exception:  # noqa: BLE001
                    self._parse_error("telemetry")
            elif self.cardata.latest and now - last_tel >= 1.0:
                self._tel_dirty = True
            if self._track_id is not None and now - self._map_report_last >= MAP_REPORT_S and self.positions.latest:
                self._map_report_last = now
                self._log_map_report()
            if self.source.mode == "live" and self.diag.due(now):
                self.diag.mark_reported(now)
                try:
                    for line in format_report(self.diagnostics()).splitlines():
                        log.info("%s", line)
                except Exception:  # noqa: BLE001
                    self._parse_error("diagnostics")
            try:
                self._ref_poll(now)
            except Exception:  # noqa: BLE001
                self._parse_error("track-reference")
            if self.pit_collector is not None and now - self._pit_last_poll >= 2.0:
                self._pit_last_poll = now
                try:
                    self._pit_poll()
                except Exception:  # noqa: BLE001
                    self._parse_error("pitlane")
            if now - last_sync >= SYNC_INTERVAL:
                last_sync = now
                try:
                    self.hub.set_sync(self.sync_status())
                except Exception:  # noqa: BLE001
                    self._parse_error("sync-status")

    def _rc_until(self) -> Optional[datetime]:
        """Race control is cut at the shown moment whenever that is a real point in time
        (recording, delay, rewound live video) - at the live edge everything received is shown."""
        return self._target_dt() if math.isfinite(self._target_ms) else None

    def snapshot(self) -> dict:
        st = self.normalizer.build(self.feed_state, self._target_dt(), self._rate, self.availability,
                                   rc_until=self._rc_until())
        try:
            st["map"] = self.map_info()
        except Exception:  # noqa: BLE001
            self._parse_error("map")
        return st

    # ------------------------------------------------------------------ positions / diagnostics
    def _now_pres_ms(self) -> float:
        """The shown F1 moment on the position / telemetry time base."""
        if math.isfinite(self._target_ms):
            return self._pres_ms()
        return self._live_f1_now_ms() if self.source.mode == "live" else self.source.now().timestamp() * 1000

    def _driver_numbers(self) -> set[str]:
        dl = self.feed_state.get("DriverList") or {}
        lines = (self.feed_state.get("TimingData") or {}).get("Lines") or {}
        return {str(k) for k in list(dl) + list(lines) if str(k).isdigit()}

    def map_info(self) -> dict:
        """Map extras: objects in Position.z that are not cars, and the safety car position -
        only when a key configured in [f1_tv] safety_car_position_keys is in the feed (never
        estimated, never a normal driver)."""
        drivers = self._driver_numbers()
        latest = self.positions.latest
        others = sorted(k for k in latest if k not in drivers) if drivers else []
        now = self._now_pres_ms()
        sc = {"available": False, "source": None}
        key = next((k for k in self.sc_keys if k in latest), None)
        if key is not None:
            v = latest[key]
            age = max(0, int(now - v[0]))
            sc = {"available": True, "source": "Position.z", "key": key, "x": v[1], "y": v[2],
                  "z": v[4] if len(v) > 4 else None, "status": v[3], "age_ms": age, "fresh": age <= POS_FRESH_MS}
        elif not latest:
            sc["reason"] = "no Position.z data received"
        elif others:
            sc["reason"] = (f"Position.z has {len(others)} non-driver object(s) ({', '.join(others)}) - none is "
                            "configured as the safety car ([f1_tv] safety_car_position_keys)")
        else:
            sc["reason"] = "not provided by the feed (Position.z carries cars only)"
        return {"safety_car": sc, "non_driver_objects": [k for k in others if k != key]}

    def diagnostics(self) -> dict:
        """What the feed delivers now - safe for logs / HTTP (no secrets)."""
        mono = time.monotonic()
        fs = self.feed_state
        si = fs.get("SessionInfo") or {}
        conn = {"mode": self.source.mode, "state": self._status.get("state"),
                "transport": self._status.get("transport"), "auth_mode": self._status.get("auth", "ANONYMOUS")
                if self.source.mode == "live" else None}
        info = getattr(self.source, "connection_info", None)
        extra = info() if callable(info) else {}
        subscribed = extra.get("topics_subscribed") or []
        drivers = self._driver_numbers()
        now = self._now_pres_ms()
        pos = self.positions.freshness(now)
        car = self.cardata.freshness(now)
        chans = sorted({k for c in self.cardata.latest.values() for k in c if k != "_t"}, key=lambda x: (not x.isdigit(), int(x) if x.isdigit() else 0, x))
        app = (fs.get("TimingAppData") or {}).get("Lines") or {}
        stints = sum(1 for v in app.values() if isinstance(v, dict) and v.get("Stints"))
        tss = fs.get("TyreStintSeries") or {}
        rc = fs.get("RaceControlMessages") or {}
        msgs = rc.get("Messages") if isinstance(rc, dict) else None
        n_rc = len(msgs) if isinstance(msgs, (list, dict)) else 0
        w = fs.get("WeatherData")
        ts = fs.get("TrackStatus") or {}
        geo = self.geometry
        rep = {
            "auth": extra.get("f1_tv") or {"subscription": False, "state": "DISABLED"},
            "connection": conn,
            "session": {"meeting": (si.get("Meeting") or {}).get("Name"), "name": si.get("Name"),
                        "type": si.get("Type"), "key": si.get("Key"),
                        "status": (fs.get("SessionStatus") or {}).get("Status") or si.get("SessionStatus")},
            "topics": self.diag.topics_table(subscribed, mono),
            "positions": {"drivers": len(drivers), "with_data": sum(1 for k in pos if k in drivers),
                          "fresh": sum(1 for k, v in pos.items() if k in drivers and v["fresh"]),
                          "source": self.availability.positions_source,
                          "non_driver_objects": self.map_info()["non_driver_objects"]},
            "car_data": {"drivers": len(drivers), "with_data": sum(1 for k in car if k in drivers),
                         "fresh": sum(1 for k, v in car.items() if k in drivers and v["fresh"]),
                         "channels": chans, "source": self.availability.car_data_source},
            "safety_car_position": self.map_info()["safety_car"],
            "map_validation": self.map_report(),
            "track_geometry": {"available": geo is not None,
                               "detail": f"{geo.name} ({geo.source})" if geo is not None else "not loaded"},
            "tyres": {"available": bool(stints or tss or fs.get("CurrentTyres")),
                      "detail": f"TimingAppData stints for {stints} cars" + (", TyreStintSeries" if tss else "")
                      + (", CurrentTyres" if fs.get("CurrentTyres") else "")},
            "race_control": {"available": "RaceControlMessages" in fs.topics, "detail": f"{n_rc} messages"},
            "weather": {"available": bool(w), "detail": f"air {w.get('AirTemp')} °C, track {w.get('TrackTemp')} °C"
                        if isinstance(w, dict) and w else ""},
            "track_status": {"available": bool(ts), "detail": f"code {ts.get('Status')} {ts.get('Message') or ''}".strip()
                             if ts else "no TrackStatus (race control is the fallback)"},
        }
        return rep
