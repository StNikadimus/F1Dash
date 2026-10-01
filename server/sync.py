"""VOYO video <-> F1 timeline synchronisation (the sync manager).

Clocks (never mixed):

* **A - F1 event time**: canonical timeline, epoch ms (see server/timeline.py).
* **B - F1 receive time**: when the server got a message; diagnostics only.
* **C - VOYO playback time**: ``HTMLVideoElement.currentTime`` of the official
  VOYO player, read by tools/voyo_clock.py through the VOYO window's local
  DevTools port (read-only; no stream access, nothing recorded, DRM untouched).

    absoluteF1Time = anchorF1Time + (currentTime - anchorVideoTime) = currentTime + offset

Nothing on the VOYO page gives the offset: the picture cannot be read (DRM),
the VOD playlist has no PROGRAM-DATE-TIME / DATERANGE, and mediaId / title /
length / startAt / GraphQL ``info`` carry no UTC. So every offset comes from
an *anchor* (a moment of the video whose F1 time is known):

  VOYO Countdown     you enter the countdown VOYO shows to the session start;
                     F1 time = OpenF1 sessions.date_start - countdown   (±2 s)
  Manual Exact Time  you enter the time the video shows                  (MANUAL)
  L / S keys         event anchors: lights out / session start, or the selected
                     car crossing the line; matched automatically to OpenF1's
                     millisecond timestamps (laps.date_start, SESSION STARTED)
  pin (K)            "the time shown now is right"                       (MANUAL)
  Estimate           OpenF1 date_start + a VOYO lead time YOU set        (LOW)
  Session clock      qualifying / practice: the time remaining / elapsed the TV clock shows
                     in Q1/Q2/Q3 / FP; F1 time from the official timing clock   (±0.5–1 s)
  Phase marker       SYNC HERE on a moment known to the millisecond: the session clock
                     reaching 0:00 (Q2 END / SESSION END) or starting (Q2 START)  (event anchor)
  Automatic          only a sync saved for the same video + session; otherwise
                     it says why it cannot sync and changes nothing.

Confidence (descriptive, no invented score):
  HIGH     >= 2 independent anchors agree within their errors
  MEDIUM   one good anchor that cannot be verified independently
  LOW      estimate from the session start, anchors that disagree, or a lap
           crossing whose lap is not confirmed
  MANUAL   time entered / pinned / adjusted by you
  UNSYNCED nothing reliable -> no timestamp is produced
"""
from __future__ import annotations

import json
import logging
import math
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from statistics import median
from typing import Any, Optional

from .openf1 import RefEvents
from .session_phases import SessionTimeline, build_timeline, fmt_clock, session_kind
from .telemetry import parse_utc

log = logging.getLogger("sync")

INF = math.inf
MODES = ("AUTO", "VOYO", "DELAY", "LIVE")
VIDEO_EVENTS = {"play", "pause", "seeking", "seeked", "ratechange", "waiting", "playing",
                "stalled", "emptied", "loadstart", "ended", "timeupdate", "durationchange"}
SEEK_EVENTS = {"seeking", "seeked", "emptied", "loadstart"}


def _num(v: Any, lo: float, hi: float) -> Optional[float]:
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    f = float(v)
    if not math.isfinite(f) or f < lo or f > hi:
        return None
    return f


def utc_str(ms: Optional[float]) -> Optional[str]:
    if ms is None or not math.isfinite(ms):
        return None
    return datetime.fromtimestamp(ms / 1000, timezone.utc).strftime("%H:%M:%S.%f")[:-3]


# ---------------------------------------------------------------------------
# C: the VOYO playback clock
# ---------------------------------------------------------------------------
@dataclass
class VoyoSample:
    mono: float                    # server monotonic time the value was valid at
    pb: float                      # currentTime (s, VOYO asset timeline)
    paused: bool
    rate: float
    ready: int
    seeking: bool
    ended: bool
    duration: Optional[float]
    seekable_start: Optional[float]
    seekable_end: Optional[float]
    buffered_end: Optional[float]
    asset: str
    meta: dict
    events: list
    page: dict = field(default_factory=dict)      # title / og:title / media title / media id (display + detection)


def parse_voyo_sample(body: Any, now_wall: float, now_mono: float) -> VoyoSample:
    """Validate a sample posted by the launcher bridge. Raises ValueError."""
    if not isinstance(body, dict):
        raise ValueError("object expected")
    pb = _num(body.get("playback_time"), 0, 1e7)
    if pb is None:
        raise ValueError("playback_time missing/invalid")
    rate = _num(body.get("playback_rate", 1.0), 0, 16)
    ready = _num(body.get("ready_state", 4), 0, 4)
    ts_local = _num(body.get("timestamp_local"), 1e9, 1e10)       # epoch seconds (same PC)
    age = 0.0
    if ts_local is not None:
        a = now_wall - ts_local
        if 0 <= a <= 1.5:                  # same machine clock: compensate transport time
            age = a
    events = []
    for ev in (body.get("events") or [])[:50] if isinstance(body.get("events"), list) else []:
        if isinstance(ev, dict) and ev.get("type") in VIDEO_EVENTS:
            events.append({"type": ev["type"], "pb": _num(ev.get("pb"), 0, 1e7)})
    meta = {}
    raw_meta = body.get("meta") if isinstance(body.get("meta"), dict) else {}
    for k in ("length", "startAt"):
        v = _num(raw_meta.get(k), -1e7, 1e7)
        if v is not None:
            meta[k] = v
    if isinstance(raw_meta.get("drmProtected"), bool):
        meta["drmProtected"] = raw_meta["drmProtected"]
    asset = str(body.get("asset") or "")[:200]
    asset = "".join(ch for ch in asset if 32 <= ord(ch) < 127)
    page = {}
    raw_page = body.get("page") if isinstance(body.get("page"), dict) else {}
    for k in ("title", "og_title", "media_title", "media_id", "url_path", "published"):
        v = raw_page.get(k)
        if isinstance(v, (str, int)) and not isinstance(v, bool):
            txt = "".join(ch for ch in str(v)[:200] if ch.isprintable())
            if txt:
                page[k] = txt
    if page.get("media_id"):
        # a VOYO recording is identified by its media id (stable across reloads)
        asset = "voyo-media:" + "".join(ch for ch in page["media_id"] if ch.isalnum() or ch in "-_")[:64]
    return VoyoSample(
        mono=now_mono - age, pb=pb, paused=bool(body.get("paused")),
        rate=1.0 if rate is None else rate, ready=int(ready if ready is not None else 4),
        seeking=bool(body.get("seeking")), ended=bool(body.get("ended")),
        duration=_num(body.get("duration"), 0, 1e8),
        seekable_start=_num(body.get("seekable_start"), 0, 1e8),
        seekable_end=_num(body.get("seekable_end"), 0, 1e8),
        buffered_end=_num(body.get("buffered_end"), 0, 1e8),
        asset=asset, meta=meta, events=events, page=page)


class VoyoClock:
    STALE_S = 3.0          # no sample for this long -> clock lost
    EXTRAP_MAX_S = 2.0     # never extrapolate the video further than this

    def __init__(self) -> None:
        self.last: Optional[VoyoSample] = None
        self.prev: Optional[VoyoSample] = None
        self.stalled = False
        self.samples = 0
        self.recent_events: deque = deque(maxlen=12)

    def update(self, s: VoyoSample) -> dict:
        info = {"seek": False, "asset_changed": False}
        if self.last is not None:
            predicted = self.pb_at(s.mono)
            if any(e["type"] in SEEK_EVENTS for e in s.events) or \
                    (predicted is not None and abs(s.pb - predicted) > 1.0):
                info["seek"] = True
            if s.asset != self.last.asset:
                info["asset_changed"] = True
            # progress check: "playing" but the position does not move -> buffering
            dt = s.mono - self.last.mono
            if not s.paused and not s.seeking and dt >= 0.35:
                self.stalled = (s.pb - self.last.pb) < 0.05 * max(s.rate, 0.1) * dt
            elif s.paused or s.seeking:
                self.stalled = False
        for e in s.events:
            if e["type"] != "timeupdate":
                self.recent_events.append((round(time.time(), 1), e["type"]))
        self.prev, self.last = self.last, s
        self.samples += 1
        return info

    def rate_now(self) -> float:
        s = self.last
        if s is None or s.paused or s.seeking or s.ended or s.ready < 3 or self.stalled:
            return 0.0
        return s.rate

    def pb_at(self, mono: float) -> Optional[float]:
        s = self.last
        if s is None:
            return None
        dt = max(0.0, min(mono - s.mono, self.EXTRAP_MAX_S))
        return s.pb + dt * self.rate_now()

    def fresh(self, mono: float) -> bool:
        return self.last is not None and mono - self.last.mono < self.STALE_S

    def state(self, mono: float) -> str:
        s = self.last
        if s is None:
            return "NONE"
        if not self.fresh(mono):
            return "STALE"
        if s.ended:
            return "ENDED"
        if s.seeking:
            return "SEEKING"
        if s.paused:
            return "PAUSED"
        if s.ready < 3 or self.stalled:
            return "BUFFERING"
        return "PLAYING"


class LiveEdge:
    """Live edge from ``video.seekable`` - only trusted when measured to advance in real time."""

    def __init__(self) -> None:
        self.hist: deque = deque()

    def reset(self) -> None:
        self.hist.clear()

    def observe(self, s: VoyoSample) -> None:
        if s.seekable_end is None:
            return
        self.hist.append((s.mono, s.seekable_end))
        while self.hist and s.mono - self.hist[0][0] > 90:
            self.hist.popleft()

    def slope(self) -> Optional[float]:
        if len(self.hist) < 5:
            return None
        (m0, e0), (m1, e1) = self.hist[0], self.hist[-1]
        if m1 - m0 < 15:
            return None
        return (e1 - e0) / (m1 - m0)

    def reliable(self) -> bool:
        sl = self.slope()
        return sl is not None and 0.8 <= sl <= 1.25

    def edge_at(self, mono: float) -> Optional[float]:
        if not self.reliable():
            return None
        m, e = self.hist[-1]
        return e + max(0.0, mono - m)


# ---------------------------------------------------------------------------
# Reference events from the running feed (live / replay; VOD uses OpenF1)
# ---------------------------------------------------------------------------
class FeedRefCollector:
    """Lap-line crossings and session start/finish observed in the feed (clock A)."""

    TIMELINE_TOPICS = ("ExtrapolatedClock", "SessionData", "SessionStatus", "RaceControlMessages", "SessionInfo")

    def __init__(self) -> None:
        self.laps: dict[str, int] = {}
        self.ref = RefEvents(source="feed")
        self._small: list[tuple[float, str, Any]] = []
        self._tl_n = -1
        self._tl_key: Any = None

    def reset(self) -> None:
        self.laps.clear()
        self.ref = RefEvents(source="feed")
        self._small = []
        self._tl_n = -1
        self._tl_key = None

    def timeline(self) -> Optional[SessionTimeline]:
        """Session structure from the clock / status messages received so far (built on demand)."""
        if self._tl_n != len(self._small):
            self._tl_n = len(self._small)
            self.ref.timeline = build_timeline(self._small, source="feed") if self._small else None
        return self.ref.timeline

    def observe(self, topic: str, data: Any, event_ms: Optional[float], snapshot: bool) -> None:
        if event_ms is None or not isinstance(data, dict):
            return
        if topic == "SessionInfo" and data.get("Key") is not None and data.get("Key") != self._tl_key:
            # another session (the server keeps running from FP3 into qualifying): start over
            if self._tl_key is not None:
                self._small, self._tl_n = [], -1
                self.ref.timeline = None
            self._tl_key = data.get("Key")
        if topic in self.TIMELINE_TOPICS and len(self._small) < 100_000:
            self._small.append((float(event_ms), topic, data))
        if topic == "SessionStatus" and not snapshot:
            if data.get("Status") == "Started":
                self.ref.starts.append(event_ms)
            elif data.get("Status") == "Finished":
                self.ref.finishes.append(event_ms)
            return
        if topic not in ("TimingData", "TimingDataF1") or not isinstance(data.get("Lines"), dict):
            return
        for num, d in data["Lines"].items():
            if not isinstance(d, dict) or "NumberOfLaps" not in d:
                continue
            try:
                n = int(d["NumberOfLaps"])
            except (TypeError, ValueError):
                continue
            prev = self.laps.get(str(num))
            self.laps[str(num)] = max(n, prev or 0)
            if snapshot or data.get("_kf") or prev is None or n <= prev or d.get("InPit") or d.get("PitOut"):
                continue            # snapshots / the TimingDataF1 twin / pit lane are no crossings on track
            self.ref.add_crossing(str(num), float(event_ms), n)


# ---------------------------------------------------------------------------
# Anchors and the mapping
# ---------------------------------------------------------------------------
CONFIDENCES = ("HIGH", "MEDIUM", "LOW", "MANUAL", "UNSYNCED")
EVENT_KINDS = ("lap", "start", "finish", "marker")   # matched to OpenF1 / feed / timing clock timestamps (ms)
METHOD_LABEL = {"lap": "Line crossing (event anchor)", "start": "Session start (event anchor)",
                "finish": "Session finish (event anchor)", "countdown": "VOYO Countdown",
                "exact": "Manual Exact Time", "pin": "Manual pin", "estimate": "Session Start Estimate",
                "marker": "Phase marker", "clock": "Session clock"}
CLASS_ORDER = ("event", "clock", "countdown", "exact")      # most precise kind of anchor first


@dataclass
class Anchor:
    kind: str                      # lap | start | finish | countdown | exact | pin
    offset: float                  # VOYO: F1 epoch s - video s;  DELAY: delay s
    f1_ms: float                   # clock A the anchor says the video showed
    video_time: Optional[float]    # clock C at that moment (None in DELAY mode)
    wall: float
    driver: Optional[str] = None
    lap: Optional[int] = None
    grounded: bool = True          # which lap / which start is certain
    ambiguous: bool = False
    outlier: bool = False
    ref_source: str = ""
    restored: bool = False
    error: Optional[float] = None  # nominal error of this anchor (s)
    detail: str = ""               # e.g. "23:47 before session start"
    paused: bool = False           # video was paused on the event (no reaction time)
    status: str = ""               # history only: replaced | rejected
    label: str = ""                # method as the user reads it: "Q2 Time Remaining", "Q2 END Marker"

    def to_json(self) -> dict:
        return {k: getattr(self, k) for k in ("kind", "offset", "f1_ms", "video_time", "wall", "driver", "lap",
                                                "grounded", "ambiguous", "ref_source", "error", "detail", "paused",
                                                "status", "label")}

    def group(self) -> tuple:
        """Anchors from the same user action / the same source are not independent:
        pressing S twice for one crossing is one event; all countdown readings share
        the countdown graphic's bias; all typed times share your reading."""
        if self.kind in EVENT_KINDS:
            return ("event", round(self.f1_ms / 10))
        return (self.kind,)

    def nominal(self) -> Optional[tuple[float, float]]:
        """Stated error range of ONE anchor of this kind (not a measurement)."""
        if self.kind in EVENT_KINDS:
            return (0.1, 0.3) if self.paused else (0.2, 0.5)       # press reaction; ms timestamps
        if self.kind == "countdown":
            return (1.0, 2.0)                                       # whole seconds + graphic lag
        if self.kind == "clock":
            return (0.5, 1.0)                                       # the TV clock shows whole seconds
        if self.kind == "exact":
            return (0.5, 1.0)
        return None


@dataclass
class Mapping:
    offset: Optional[float]        # VOYO: K ;  DELAY: delay
    error: Optional[float]         # seconds; None = not meaningful
    confidence: str
    source: str
    reason: str
    grounded: bool = False
    n: int = 0
    spread: Optional[float] = None
    method: str = ""               # what the user sees as "Method"
    anchor: str = ""               # what the user sees as "Anchor"
    # SYNC HEALTH = how good the time actually is (separate from how it was obtained)
    health: str = "UNSYNCED"       # HIGH (<= 0.5 s) | MEDIUM (<= 2 s) | LOW (> 2 s / unknown) | MANUAL | UNSYNCED
    health_error: Optional[str] = None   # "±0.25 s" (measured) or "±1–2 s" (estimated); None = not computable
    error_measured: bool = False
    n_valid: int = 0
    n_outlier: int = 0
    independent: int = 0
    deviation: Optional[float] = None    # largest deviation between the used anchors (s)


@dataclass
class Target:
    ms: Optional[float]   # clock A target (INF = everything received, None = unsynced)
    rate: float           # clock A ms per wall ms (0 = frozen)
    mode: str             # VOYO | DELAY | LIVE | HOLD
    jump: bool = False


class CalibrationStore:
    def __init__(self, path: Optional[Path]) -> None:
        self.path = path
        self.data: dict = {}
        if path and path.exists():
            try:
                self.data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                log.warning("Could not read %s - starting without saved calibration", path)
        for k in ("assets", "leads", "lead_user"):
            self.data.setdefault(k, {})

    def save(self) -> None:
        if not self.path:
            return
        try:
            now = time.time()
            self.data["assets"] = {k: v for k, v in self.data["assets"].items()
                                   if now - float(v.get("saved", 0)) < 30 * 86400}
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.data, indent=1), encoding="utf-8")
            tmp.replace(self.path)
        except OSError:
            log.exception("Could not save sync calibration")


def _robust(values: list[float]) -> tuple[float, Optional[float]]:
    med = median(values)
    if len(values) >= 3:
        return med, max(1.4826 * median(abs(v - med) for v in values), 0.02)
    if len(values) == 2:
        return med, abs(values[0] - values[1]) / 2
    return med, None


def fmt_hms(seconds: float) -> str:
    s = int(round(abs(seconds)))
    h, m, sec = s // 3600, (s % 3600) // 60, s % 60
    return (f"{h}:{m:02d}:{sec:02d}" if h else f"{m}:{sec:02d}")


def parse_countdown(text: str) -> Optional[float]:
    """'23:47', '00:23:47', '1:02:03', '23m 47s', '47s', '23m', '1h 2m' -> seconds (0 .. 6 h)."""
    import re
    t = (text or "").strip().lower().replace(",", ".")
    if not t:
        return None
    m = re.fullmatch(r"(?:(\d{1,2}):)?(\d{1,3}):(\d{1,2}(?:\.\d+)?)", t)
    if m:
        h, mi, se = int(m.group(1) or 0), int(m.group(2)), float(m.group(3))
        if se >= 60 or (m.group(1) and mi >= 60):
            return None
        v = h * 3600 + mi * 60 + se
    else:
        m = re.fullmatch(r"(?:(\d{1,2})\s*h)?\s*(?:(\d{1,3})\s*(?:m|min))?\s*(?:(\d{1,2}(?:\.\d+)?)\s*s)?", t)
        if not m or not any(m.groups()):
            return None
        v = int(m.group(1) or 0) * 3600 + int(m.group(2) or 0) * 60 + float(m.group(3) or 0)
    return v if 0 <= v <= 6 * 3600 else None


class SyncManager:
    """Maps VOYO playback time (C) onto F1 time (A):  A = C + offset.

    Interface:  initialize(session, ref) · update(sample) · get_state()
                add_countdown_anchor(s) · add_manual_anchor(f1_ms) · add_event_anchor(kind)
                auto_sync() · set_estimate(lead) · clear_anchor()
    """

    def __init__(self, cfg: dict, voyo_enabled: bool, legacy_delay: float, source_speed: float,
                 store_path: Optional[Path] = None, vod: bool = False) -> None:
        s = cfg or {}
        self.enabled = bool(s.get("enabled", True))
        mode = str(s.get("mode", "AUTO")).upper()
        self.mode_cfg = mode if mode in MODES else "AUTO"
        self.use_voyo = bool(s.get("voyo_playback_clock", True)) or vod
        self.vod = vod
        self.voyo_enabled = voyo_enabled
        self.step = max(0.01, float(s.get("adjustment_step", 0.25)))
        self.auto_drift = bool(s.get("auto_drift_correction", True))
        self.slew = max(0.005, float(s.get("drift_slew_seconds_per_second", 0.05)))
        self.reaction = max(0.0, float(s.get("mark_reaction_seconds", 0.2)))
        self.window_s = max(5.0, float(s.get("mark_window_seconds", 40)))
        self.marks_used = max(1, int(s.get("marks_used", 5)))
        self.anchor_error = max(0.05, float(s.get("anchor_error_seconds", 0.25)))
        self.countdown_error = max(0.5, float(s.get("countdown_error_seconds", 2.0)))
        self.exact_error = max(0.5, float(s.get("exact_time_error_seconds", 1.0)))
        self.agree_s = float(s.get("anchors_agree_seconds", 0.5))          # consistent / health HIGH
        self.drift_warn_s = float(s.get("drift_warning_seconds", 2.0))     # beyond: ask before using
        self.outlier_s = float(s.get("outlier_seconds", 1.0))              # with a majority: outlier
        self.disagree_s = self.drift_warn_s
        lead = s.get("broadcast_lead_seconds", "")
        self.lead_cfg: Optional[float] = None if lead in ("", None) else float(lead)
        self.lead_uncertainty = max(10.0, float(s.get("lead_uncertainty_seconds", 1200)))
        self.speed = source_speed
        self.default_delay = max(0.0, float(s.get("broadcast_delay_seconds", 5.0)))
        if not self.enabled:
            self.delay = max(0.0, legacy_delay)
        elif self.mode_cfg == "LIVE":
            self.delay = 0.0
        elif self.mode_cfg == "DELAY" or voyo_enabled:
            self.delay = self.default_delay
        else:
            self.delay = max(0.0, legacy_delay)
        self.clock = VoyoClock()
        self.edge = LiveEdge()
        self.feed_ref = FeedRefCollector()
        self.store = CalibrationStore(store_path)
        self.session: Optional[dict] = None
        self.vod_ref: Optional[RefEvents] = None
        self.detect_reason: Optional[str] = None
        self.anchors: list[Anchor] = []
        self.trim = 0.0
        self.est_K: Optional[float] = None          # live-stream estimate (live edge + delay)
        self.est_source = "none"
        self.K: Optional[float] = None               # offset applied now (slews towards the solution)
        self.mapping = Mapping(None, None, "UNSYNCED", "none", "no VOYO clock yet")
        self.asset: Optional[str] = None
        self.page: dict = {}
        self.active = "LIVE"
        self.note: Optional[str] = None
        self.last_result: Optional[str] = None
        self.captured: Optional[tuple] = None        # (video time or None, clock-B ms, mono) for typed input
        self.pending: Optional[dict] = None          # new anchor(s) that differ > drift_warning_seconds
        self.history: list[Anchor] = []              # replaced / rejected anchors (diagnostics)
        self.deviation_note: Optional[str] = None    # minor deviation warning
        self.replaced_note: Optional[str] = None
        self._last_target: Optional[float] = None
        self._last_src_now = 0.0
        self._last_mono: Optional[float] = None
        self._pending_jump = False
        self._clock_lost_logged = False
        self._solve()

    # ------------------------------------------------------------------ context
    @property
    def ref(self) -> Optional[RefEvents]:
        return self.vod_ref if self.vod else self.feed_ref.ref

    def _session_start_ms(self) -> Optional[float]:
        d = parse_utc((self.session or {}).get("date_start"))
        return d.timestamp() * 1000 if d else None

    def initialize(self, session: Optional[dict], ref: Optional[RefEvents] = None) -> None:
        """New session context. A sync of another video / GP / session is never reused."""
        old = (self.session or {}).get("session_key")
        self.session = session
        if self.vod:
            self.vod_ref = ref
        if session is None or session.get("session_key") != old:
            self.anchors = []
            self.history = []
            self.pending = None
            self.deviation_note = None
            self.trim = 0.0
            self._restore_asset()
        self._solve()
        self._pending_jump = True

    def observe_feed(self, topic: str, data: Any, event_ms: Optional[float], snapshot: bool) -> None:
        if self.vod:
            return
        self.feed_ref.observe(topic, data, event_ms, snapshot)
        if topic == "SessionInfo" and isinstance(data, dict) and data.get("Key"):
            if (self.session or {}).get("session_key") != data.get("Key"):
                from .sources.f1_live import _local_to_utc
                start = _local_to_utc(data.get("StartDate"), data.get("GmtOffset"))
                self.session = {"session_key": data.get("Key"), "session_name": data.get("Name"),
                                "meeting_name": (data.get("Meeting") or {}).get("Name"),
                                "country_code": ((data.get("Meeting") or {}).get("Country") or {}).get("Code"),
                                "date_start": start.isoformat() if start else None,
                                "gmt_offset": data.get("GmtOffset")}

    def on_source_reset(self) -> None:
        self.feed_ref.reset()
        self._last_target = None

    # ------------------------------------------------------------------ C: video clock
    def update(self, s: VoyoSample, src_now_ms: float) -> None:
        info = self.clock.update(s)
        self.edge.observe(s)
        if s.page:
            self.page = s.page
        if self.asset is None or info["asset_changed"]:
            self._new_asset(s, src_now_ms, changed=info["asset_changed"])
        elif info["seek"]:
            self._pending_jump = True
        if not self.vod and self.est_source == "broadcast-delay-estimate" and self.edge.reliable():
            k = src_now_ms / 1000 - self.default_delay - self.edge.edge_at(s.mono)
            if self.est_K is not None and abs(k - self.est_K) > 2.0:
                log.info("Live estimate re-anchored to the measured live edge (%.1f s change)", k - self.est_K)
                self.est_K, self.est_source = k, "live-edge-estimate"
                self._solve()
                self._pending_jump = True

    voyo_sample = update          # old name

    def _new_asset(self, s: VoyoSample, src_now_ms: float, changed: bool) -> None:
        prev_target = self._last_target
        self.asset = s.asset
        self.edge.reset()
        self.edge.observe(s)
        self.anchors = []
        self.history = []
        self.pending = None
        self.deviation_note = None
        self.trim = 0.0
        if not self.vod:
            if changed and prev_target is not None and math.isfinite(prev_target):
                self.est_K = prev_target / 1000 - s.pb       # keep the picture continuous
            else:
                learned = self.store.data.get("edge_delay_s")
                edge = self.edge.edge_at(s.mono)
                base = edge if edge is not None and learned is not None else s.pb
                self.est_K = src_now_ms / 1000 - (float(learned) if edge is not None and learned is not None
                                                  else self.default_delay) - base
            self.est_source = "broadcast-delay-estimate"
        self._restore_asset()
        if changed:
            self.note = "VOYO video changed - the previous sync is not used"
        log.info("VOYO playback clock: %s video %r", "changed" if changed else "new", s.asset[:60])
        self._solve()
        self._pending_jump = True

    def _restore_asset(self) -> bool:
        saved = self.store.data["assets"].get(self.asset or "")
        if not saved:
            return False
        age = time.time() - float(saved.get("saved", 0))
        if not self.vod and age > 12 * 3600:
            return False                        # a live channel page is a different broadcast tomorrow
        if saved.get("session_key") != (self.session or {}).get("session_key"):
            return False                        # other GP / session: never reuse
        restored = []
        for a in saved.get("anchors") or []:
            try:
                restored.append(Anchor(restored=True, **a))
            except TypeError:
                continue
        self.history = []
        for a in saved.get("history") or []:
            try:
                self.history.append(Anchor(restored=True, **a))
            except TypeError:
                continue
        if restored:
            self.anchors = restored
            self.trim = float(saved.get("trim") or 0.0)
            self.note = f"sync restored for this video and session ({len(restored)} anchor(s))"
            log.info("Sync: %s", self.note)
        return bool(restored)

    def _save_asset(self) -> None:
        if not self.asset:
            return
        self.store.data["assets"][self.asset] = {
            "session_key": (self.session or {}).get("session_key"), "saved": time.time(),
            "anchors": [a.to_json() for a in self.anchors if a.video_time is not None], "trim": self.trim,
            "history": [a.to_json() for a in self.history if a.video_time is not None][-30:]}
        m = self.mapping
        start = self._session_start_ms()
        if self.vod and start is not None and m.offset is not None and m.confidence in ("HIGH", "MEDIUM"):
            # video before the scheduled start - offered (never applied silently) as the next estimate
            leads = self.store.data["leads"].setdefault(str((self.session or {}).get("session_name") or "?"), [])
            leads.append(round(start / 1000 - m.offset, 1))
            del leads[:-10]
        edge = self.edge.edge_at(time.monotonic())
        if not self.vod and edge is not None and m.offset is not None and m.confidence in ("HIGH", "MEDIUM"):
            self.store.data["edge_delay_s"] = round(time.time() - (edge + m.offset), 3)
        self.store.save()

    # ------------------------------------------------------------------ the mapping
    def _video_mode(self, mono: Optional[float] = None) -> bool:
        return self.vod or (self.enabled and self.use_voyo and self.mode_cfg in ("AUTO", "VOYO")
                            and mono is not None and self.clock.fresh(mono))

    def voyo_active(self, mono: float) -> bool:
        return self._video_mode(mono) and self.clock.fresh(mono) and self.K is not None

    def learned_lead(self) -> tuple[Optional[float], int]:
        leads = self.store.data["leads"].get(str((self.session or {}).get("session_name") or "?")) or []
        return (median(leads), len(leads)) if leads else (None, 0)

    def user_lead(self) -> Optional[float]:
        v = self.store.data["lead_user"].get(str((self.session or {}).get("session_name") or "?"))
        return float(v) if v is not None else self.lead_cfg

    def _solve(self, video: Optional[bool] = None) -> Mapping:
        """Best mapping from what is known. Never invents an offset."""
        if video is None:
            video = self.vod or self.active == "VOYO" or (self.clock.last is not None and self.use_voyo
                                                          and self.mode_cfg in ("AUTO", "VOYO"))
        typed = [a for a in self.anchors if (a.video_time is not None) == video]
        pins = [a for a in typed if a.kind == "pin"]
        real = [a for a in typed if a.kind != "pin"][-12:]
        m: Optional[Mapping] = None
        if pins and (not real or pins[-1].wall > max(a.wall for a in real)):
            a = pins[-1]
            m = Mapping(a.offset, None, "MANUAL", "manual-pin", "You pinned the shown time as correct; "
                        "its error cannot be measured.", True, 1, None, METHOD_LABEL["pin"], "shown time pinned",
                        health="MANUAL", n_valid=1, independent=1)
        elif real:
            m = self._solve_anchors(real, video)
        elif video and self.vod:
            start = self._session_start_ms()
            lead = self.user_lead()
            if start is None:
                m = Mapping(None, None, "UNSYNCED", "none", self.detect_reason or "Session not identified.")
            elif lead is None:
                m = Mapping(None, None, "UNSYNCED", "none",
                            "No sync yet. Use VOYO Countdown or Manual Exact Time (or set a Session Start Estimate).")
            else:
                m = Mapping(start / 1000 - lead, self.lead_uncertainty, "LOW", "session-start-estimate",
                            "Timestamp is estimated from the scheduled session start and the VOYO offset you set - "
                            "real starts differ from the schedule by minutes.",
                            True, 0, None, METHOD_LABEL["estimate"], f"video starts {fmt_hms(lead)} before session start",
                            health="LOW")
        elif video:
            if self.est_K is None:
                m = Mapping(None, None, "UNSYNCED", "none", "No VOYO clock yet.")
            else:
                m = Mapping(self.est_K, None, "LOW", self.est_source,
                            "Estimated from broadcast_delay_seconds" + (" at the measured live edge."
                                                                       if self.est_source == "live-edge-estimate" else "."),
                            True, 0, None, "Broadcast delay estimate", f"{self.default_delay:.1f} s behind live",
                            health="LOW")
        else:
            if self.delay <= 0:
                m = Mapping(0.0, None, "LIVE", "live", "No delay.", method="Live", health="LIVE")
            else:
                m = Mapping(self.delay, None, "LOW", "configured-delay", "Fixed delay from the configuration.",
                            True, 0, None, "Fixed delay", f"{self.delay:.2f} s", health="LOW")
        if self.trim and m.offset is not None and m.confidence != "LIVE":
            m = Mapping(m.offset + (-self.trim if video else self.trim), None, "MANUAL", m.source + "+manual-trim",
                        f"Adjusted by you ({self.trim:+.2f} s) on top of: {m.method or m.source}; the error of the "
                        "adjusted time cannot be measured.",
                        m.grounded, m.n, m.spread, m.method + " + manual adjust", m.anchor, health="MANUAL",
                        n_valid=m.n_valid, n_outlier=m.n_outlier, independent=m.independent, deviation=m.deviation)
        self.mapping = m
        return m

    def _health_level(self, err: float) -> str:
        return "HIGH" if err <= self.agree_s else "MEDIUM" if err <= self.drift_warn_s else "LOW"

    def _solve_anchors(self, real: list[Anchor], video: bool) -> Mapping:
        """Robust offset from the anchors: median per independent group, median over the
        most precise groups, outliers rejected; the other groups only verify."""
        grounded = [a for a in real if a.grounded]
        use = grounded or real
        for a in use:
            a.outlier = False
        groups: dict[tuple, list[Anchor]] = {}
        for a in use:
            groups.setdefault(a.group(), []).append(a)
        rep = {k: median(x.offset for x in g) for k, g in groups.items()}
        for cls in CLASS_ORDER:
            pkeys = [k for k in groups if k[0] == cls]
            if pkeys:
                break
        pkeys = pkeys[-self.marks_used:]
        med, _ = _robust([rep[k] for k in pkeys])
        if len(pkeys) >= 3:                                  # a majority exists: reject outliers
            sigma = 1.4826 * median(abs(rep[k] - med) for k in pkeys)
            bad = [k for k in pkeys if abs(rep[k] - med) > max(3 * sigma, self.outlier_s)]
            if len(pkeys) - len(bad) >= 2:
                for k in bad:
                    for x in groups[k]:
                        x.outlier = True
                pkeys = [k for k in pkeys if k not in bad]
                med, _ = _robust([rep[k] for k in pkeys])
        spread = max(abs(rep[k] - med) for k in pkeys) if len(pkeys) >= 2 else None
        # verification by the less precise groups (countdown / typed time): within their stated range?
        verified, contra = [], []
        for k, g in groups.items():
            if k in pkeys or any(x.outlier for x in g):
                continue
            hi = (g[0].nominal() or (0, self.drift_warn_s))[1]
            if abs(rep[k] - med) <= max(hi, self.agree_s):
                verified.append(k)
            elif len(pkeys) >= 2:
                for x in g:
                    x.outlier = True                         # two precise anchors outvote it
            else:
                contra.append(k)
        valid = [x for x in use if not x.outlier]
        agree_keys = pkeys + verified
        independent = len(agree_keys)
        kinds = {x.kind for x in valid}
        methods = sorted({x.label or METHOD_LABEL.get(x.kind, x.kind) for x in valid})
        method = " + ".join(methods)
        anchor_txt = "; ".join(self._anchor_text(x) for x in valid[-3:])
        src = "+".join(sorted(kinds)) + ("+restored" if any(x.restored for x in valid) else "")
        n_out = sum(1 for x in use if x.outlier)
        precise_kind = pkeys[0][0] if pkeys else ""
        # ---- health: measured if >= 2 independent precise anchors, otherwise the stated range
        if precise_kind == "event" and spread is not None:
            # measured spread between independent event anchors; never below 0.1 s - the
            # resolution of a key press / of the feed's message timestamps
            err, measured = max(spread, 0.1), True
            health_err = f"±{err:.2f} s"
        else:
            rng = groups[pkeys[-1]][0].nominal() if pkeys else None
            if precise_kind == "event" and any(x.paused for x in groups[pkeys[-1]]):
                rng = (0.1, 0.3)
            err, measured = (rng[1] if rng else None), False
            health_err = f"±{rng[0]:g}–{rng[1]:g} s" if rng else None
        common = dict(grounded=True, n=len(valid), spread=spread, method=method, anchor=anchor_txt,
                      n_valid=len(valid), n_outlier=n_out, independent=independent, deviation=spread,
                      error_measured=measured)
        if not use[0].grounded:
            return Mapping(med, self.lead_uncertainty, "LOW", src,
                           "Which lap / start the anchor belongs to is not confirmed - confirm it (the dashboard "
                           "lap matches the TV) or add a countdown / exact time / L.",
                           **{**common, "grounded": False, "health": "LOW", "health_error": None,
                              "error_measured": False})
        worst = max([abs(rep[k] - med) for k in contra] + [spread or 0.0])
        if contra or (spread is not None and spread > self.drift_warn_s):
            return Mapping(med, worst, "LOW", src,
                           f"Anchors disagree by {worst:.2f} s and there is no majority - check them and remove "
                           "the wrong one (Clear) or add another anchor.",
                           **{**common, "health": "LOW", "health_error": f"±{worst:.1f} s (anchors disagree)",
                              "error_measured": True, "deviation": worst})
        if kinds <= {"exact"}:
            return Mapping(med, None, "MANUAL", src, "The time was entered by you (Manual Exact Time); "
                           "its error cannot be measured.", **{**common, "health": "MANUAL", "health_error": None})
        health = self._health_level(err) if err is not None else "LOW"
        if spread is not None and spread > self.agree_s:
            reason = (f"{independent} independent anchors, but they differ by up to {spread:.2f} s "
                      f"(minor deviation) - add another anchor to see which one is right.")
            return Mapping(med, err, "MEDIUM", src, reason, **{**common, "health": health, "health_error": health_err})
        if independent >= 2:
            detail = f"within ±{max(spread, 0.1):.2f} s" if spread is not None else "within their stated errors"
            conf = "HIGH" if video else "MEDIUM"
            reason = f"{independent} independent anchors agree {detail} ({method})."
            if n_out:
                reason += f" {n_out} outlier(s) ignored."
            if not video:
                reason += " A fixed delay cannot follow pause/seek."
            return Mapping(med, err, conf, src, reason, **{**common, "health": health, "health_error": health_err})
        a = groups[pkeys[-1]][0]
        if a.kind == "countdown":
            reason = ("The VOYO countdown was provided by you and matched to the official OpenF1 session start; "
                      "not yet verified independently (add L at the start or S at a line crossing).")
        elif a.kind == "clock":
            reason = ("The session clock you read was matched to the official F1 timing clock (whole seconds); "
                      "not yet verified independently (add a phase END / START marker or S at a line crossing).")
        elif a.kind == "marker":
            reason = (f"Phase marker matched to the F1 timing clock ({a.label or 'marker'}); the error is where you "
                      f"paused / pressed ({'video paused on the moment' if a.paused else 'reaction time'}). "
                      "Not yet verified independently.")
        elif a.kind in EVENT_KINDS:
            reason = (f"One event anchor matched to the {a.ref_source or 'reference'} millisecond timestamp; "
                      f"the error is your key press ({'video paused on the event' if a.paused else 'reaction time'}). "
                      "Not yet verified independently.")
        else:
            reason = "One anchor."
        return Mapping(med, err, "MEDIUM", src, reason, **{**common, "health": health, "health_error": health_err})

    def _anchor_text(self, a: Anchor) -> str:
        if a.detail:
            return a.detail
        if a.kind == "lap":
            return f"#{a.driver} line crossing, lap {a.lap} at {utc_str(a.f1_ms)} UTC"
        if a.kind in ("start", "finish"):
            return f"session {a.kind} at {utc_str(a.f1_ms)} UTC"
        return utc_str(a.f1_ms) or ""

    # ------------------------------------------------------------------ qualifying / practice
    def timeline(self) -> Optional[SessionTimeline]:
        if self.vod:
            return getattr(self.vod_ref, "timeline", None) if self.vod_ref is not None else None
        return self.feed_ref.timeline()

    def session_kind(self) -> str:
        sess = self.session or {}
        k = session_kind(sess.get("session_type"), sess.get("session_name"))
        if k == "unknown":
            tl = self.timeline()
            k = tl.kind if tl is not None else k
        return k

    def add_clock_anchor(self, phase: str, mode: str, seconds: float, mono: float, src_now_ms: float,
                         text: str = "") -> str:
        """You read the session clock of ``phase`` (time remaining or elapsed) at the captured /
        current moment -> F1 time from the official timing clock of that phase."""
        err = self._need_clock(mono)
        if err:
            return err
        tl = self.timeline()
        if tl is None or tl.empty():
            return "SYNC: the session clock is not known yet (session timing not loaded) - use Manual Exact Time"
        mode = "elapsed" if str(mode).lower().startswith("el") else "remaining"
        conv = tl.elapsed_to_f1 if mode == "elapsed" else tl.remaining_to_f1
        f1, why = conv(phase, seconds * 1000)
        p = tl.phase(phase)
        if f1 is None:
            return f"SYNC: {why}"
        video, pb, now_b, _ = self._typed_point(mono, src_now_ms)
        self.captured = None
        offset = f1 / 1000 - pb if video else (now_b - f1) / 1000
        shown = text or fmt_clock(seconds * 1000)
        what = "Time Remaining" if mode == "remaining" else "Time Elapsed"
        a = Anchor("clock", offset, f1, pb, time.time(), grounded=True, ref_source="f1-timing-clock",
                   error=1.0, label=f"{p.label} {what}",
                   detail=f"{p.label} {'remaining' if mode == 'remaining' else 'elapsed'} {shown} "
                          f"(clock → {utc_str(f1)} UTC)")
        m = self._add(a, explicit=True)
        return self._anchor_result(m, a, f"{p.label} {what.upper()} {shown}")

    def add_marker_anchor(self, marker_id: str, mono: float, src_now_ms: float) -> str:
        """SYNC HERE: the video shows the marked moment now (e.g. the Q2 clock reaching 0:00)."""
        err = self._need_clock(mono)
        if err:
            return err
        tl = self.timeline()
        mk = tl.marker(marker_id) if tl is not None else None
        if mk is None:
            return f"SYNC: marker {marker_id} is not in this session's timing data"
        if not mk.sync:
            return f"SYNC: {mk.label} is not known precisely enough for SYNC HERE"
        video, pb, press_b = self._now_point(mono, src_now_ms, reaction=True)
        offset = mk.ms / 1000 - pb if video else (press_b - mk.ms) / 1000
        a = Anchor("marker", offset, mk.ms, pb, time.time(), grounded=True, ref_source="f1-timing-clock",
                   error=self.anchor_error, paused=bool(video and self.clock.last and self.clock.last.paused),
                   label=f"{mk.label} Marker", detail=f"{mk.label} at {utc_str(mk.ms)} UTC ({mk.how})")
        m = self._add(a, explicit=True)
        return self._anchor_result(m, a, f"{mk.label} MARKER")

    def _timeline_state(self) -> Optional[dict]:
        tl = self.timeline()
        if tl is None or tl.empty():
            return None
        tgt = self._last_target
        synced = self.mapping.confidence in ("HIGH", "MEDIUM", "MANUAL") and tgt is not None and math.isfinite(tgt)
        js = tl.to_json(until_ms=tgt if synced else None)
        clk = tl.clock_at(tgt) if synced else None
        return {**js, "current": clk}

    def _apply_mapping(self, jump_threshold: float = 2.0) -> None:
        m = self.mapping
        if m.offset is None:
            self.K = None
            return
        if self.K is None or abs(m.offset - self.K) > jump_threshold or not self.auto_drift \
                or m.confidence in ("LOW", "MANUAL"):
            if self.K is None or abs(m.offset - self.K) > 1e-6:
                self._pending_jump = True
            self.K = m.offset

    # ------------------------------------------------------------------ target
    def target(self, mono: float, src_now_ms: float) -> Target:
        video = self._video_mode(mono)
        if video and self.clock.fresh(mono):
            if self.active != "VOYO":
                self._clock_lost_logged = False
                log.info("Sync: following the VOYO playback clock")
                self.active = "VOYO"
                self._solve(video=True)
            self._apply_mapping()
            self._do_slew(mono)
            jump = self._pending_jump
            self._pending_jump = False
            if self.K is None:
                tgt = Target(None, 0.0, "VOYO", jump)
            else:
                t = (self.clock.pb_at(mono) + self.K) * 1000
                if not jump and self._last_target is not None and t < self._last_target and \
                        self._last_target - t < 300:
                    t = self._last_target          # sample jitter: hold instead of stepping back
                tgt = Target(t, self.clock.rate_now(), "VOYO", jump)
        elif self.vod:
            if self.clock.last is not None and not self._clock_lost_logged:
                log.warning("VOYO playback clock lost - holding the shown time")
                self._clock_lost_logged = True
            self.active = "HOLD"
            jump = self._pending_jump
            self._pending_jump = False
            tgt = Target(self._last_target, 0.0, "HOLD", jump)
        else:
            if self.active == "VOYO" and self._last_target is not None and math.isfinite(self._last_target):
                self.delay = max(0.0, (self._last_src_now - self._last_target) / 1000)
                if not self._clock_lost_logged:
                    log.warning("VOYO playback clock lost - holding a fixed delay of %.2f s", self.delay)
                    self._clock_lost_logged = True
                self.active = "DELAY"
            jump = self._pending_jump
            self._pending_jump = False
            m = self._solve(video=False)
            delay = m.offset if m.offset is not None else self.delay
            if delay <= 0 or (self.enabled and self.mode_cfg == "LIVE"):
                self.active = "LIVE"
                tgt = Target(INF, self.speed, "LIVE", jump)
            else:
                self.active = "DELAY"
                tgt = Target(src_now_ms - delay * 1000, self.speed, "DELAY", jump)
        if tgt.ms is not None and math.isfinite(tgt.ms):
            self._last_target = tgt.ms
        elif tgt.mode != "HOLD":
            self._last_target = None
        self._last_src_now = src_now_ms
        self._last_mono = mono
        return tgt

    def _do_slew(self, mono: float) -> None:
        goal = self.mapping.offset
        if self.K is None or goal is None:
            return
        diff = goal - self.K
        if abs(diff) < 1e-4:
            self.K = goal
            return
        dt = 0.1 if self._last_mono is None else max(0.0, min(1.0, mono - self._last_mono))
        stepv = self.slew * dt
        self.K += max(-stepv, min(stepv, diff))

    # ------------------------------------------------------------------ anchors
    def _now_point(self, mono: float, src_now_ms: float, reaction: bool) -> tuple[bool, Optional[float], float]:
        """(video mode, video time at the moment, clock-B time of the moment)."""
        video = self._video_mode(mono) and self.clock.fresh(mono)
        if video:
            paused = self.clock.last.paused
            pb = self.clock.pb_at(mono)
            if reaction and not paused:
                pb -= self.reaction * max(self.clock.rate_now(), 0.0)
            return True, pb, src_now_ms
        return False, None, src_now_ms - (self.reaction * 1000 * self.speed if reaction else 0)

    def _shift(self, video: bool, new: float, old: float) -> float:
        """How far the shown F1 time would move (s, + = later) when the offset changes old -> new."""
        return (new - old) if video else (old - new)

    def _add(self, a: Anchor, remove_pins: bool = True, force: bool = False, explicit: bool = False) -> Mapping:
        """Add an anchor. With an existing sync it is first compared (drift detection):
        <= anchors_agree_seconds  consistent -> used at once
        <= drift_warning_seconds  minor deviation -> used, the display moves slowly, warning
        >  drift_warning_seconds  NOT used until you choose Keep old / Use new."""
        video = a.video_time is not None
        cur = self._solve(video=video)
        cur_off = self.K if (video and self.K is not None) else cur.offset
        has_sync = (cur.offset is not None and cur.n > 0 and cur.grounded and a.kind != "pin"
                    and cur.confidence != "UNSYNCED")
        shift = self._shift(video, a.offset, cur_off) if has_sync and cur_off is not None else None
        self.replaced_note = None
        if explicit and has_sync and shift is not None and abs(shift) > self.drift_warn_s:
            # a time you typed in the menu (exact time / countdown) is an explicit new sync, not a
            # silent change: the previous anchors go to the history and the menu says so
            for x in self.anchors:
                if (x.video_time is not None) == video:
                    x.status = "replaced"
                    self.history.append(x)
            del self.history[:-30]
            self.anchors = [x for x in self.anchors if (x.video_time is not None) != video]
            self.pending = None
            self.replaced_note = f"previous sync replaced (it differed by {shift:+.2f} s)"
            log.info("Sync: %s", self.replaced_note)
            has_sync = False
        if has_sync and not force and shift is not None and abs(shift) > self.drift_warn_s:
            prev = self.pending
            anchors = [a]
            agree_prev = False
            if prev and abs(self._shift(video, a.offset, prev["anchors"][-1].offset)) <= self.agree_s:
                anchors = prev["anchors"] + [a]
                agree_prev = True
            self.pending = {"anchors": anchors, "old": cur_off, "new": median(x.offset for x in anchors),
                            "shift": self._shift(video, median(x.offset for x in anchors), cur_off),
                            "video": video, "agree": len(anchors) if agree_prev else 1, "wall": time.time()}
            log.warning("Sync: possible drift - new %s anchor would move the shown time by %+.2f s "
                        "(not applied; waiting for Keep old / Use new)", a.kind, shift)
            return cur
        if remove_pins:
            self.anchors = [x for x in self.anchors if x.kind != "pin"]
        self.anchors.append(a)
        del self.anchors[:-20]
        self.trim = 0.0
        if a.grounded and a.kind != "pin":
            self._rematch_ungrounded(a.offset, video)
        m = self._solve(video=video)
        move = None if (self.K is None or m.offset is None or not video) else self._shift(video, m.offset, self.K)
        if not has_sync or move is None or abs(move) <= self.agree_s or force:
            self.K = m.offset                    # consistent (or the first anchor): applied at once
            self._pending_jump = True
            self.deviation_note = None
        else:
            # minor deviation: no sudden jump - the display moves slowly (drift_slew_seconds_per_second)
            self.deviation_note = (f"The new anchor differs by {shift:+.2f} s from the current sync; the anchors do "
                                   f"not match perfectly. The display moves by {move:+.2f} s gradually.")
            log.info("Sync: minor deviation %+.2f s - moving gradually", shift)
        self._save_asset()
        return m

    def keep_old(self) -> str:
        p = self.pending
        if not p:
            return "SYNC: no drift warning to answer"
        for x in p["anchors"]:
            x.status = "rejected"
            self.history.append(x)
        del self.history[:-30]
        self.pending = None
        self._save_asset()
        return self._result(self.mapping, "KEPT THE CURRENT SYNC")

    def use_new(self) -> str:
        p = self.pending
        if not p:
            return "SYNC: no drift warning to answer"
        video = p["video"]
        for x in self.anchors:
            if (x.video_time is not None) == video:
                x.status = "replaced"
                self.history.append(x)
        del self.history[:-30]
        self.anchors = [x for x in self.anchors if (x.video_time is not None) != video] + p["anchors"]
        self.pending = None
        self.trim = 0.0
        self.deviation_note = None
        m = self._solve(video=video)
        self.K = m.offset                        # your choice: applied at once
        self._pending_jump = True
        self._save_asset()
        return self._result(m, "USING THE NEW SYNC")

    def _result(self, m: Mapping, head: str) -> str:
        txt = f"{head} → {m.confidence}"
        if m.health_error and m.confidence in ("HIGH", "MEDIUM", "MANUAL"):
            txt += f" · health {m.health} {m.health_error}"
        self.last_result = txt
        log.info("Sync: %s (%s; %s)", txt, m.method, m.reason)
        return txt

    def _need_clock(self, mono: float) -> Optional[str]:
        if (self.vod or self.use_voyo) and self.clock.last is not None and not self.clock.fresh(mono):
            return "SYNC: the VOYO playback clock is not available (VOYO window closed or no video)"
        if self.vod and self.clock.last is None:
            return "SYNC: no VOYO playback clock yet - open the recording in the VOYO window"
        return None

    def capture(self, mono: float, src_now_ms: float) -> dict:
        """Freeze 'this moment of the video' while you read the countdown / time and type it."""
        video, pb, now_b = self._now_point(mono, src_now_ms, reaction=False)
        self.captured = (pb if video else None, now_b, mono)
        return {"video_time": pb, "paused": bool(self.clock.last and self.clock.last.paused)}

    def _typed_point(self, mono: float, src_now_ms: float) -> tuple[bool, Optional[float], float, str]:
        """Moment a typed value refers to: the capture (if recent) or now."""
        video, pb, now_b = self._now_point(mono, src_now_ms, reaction=False)
        c = self.captured
        if c is not None and mono - c[2] < 300 and (c[0] is not None) == video:
            return video, c[0], c[1], "captured"
        return video, pb, now_b, "now"

    def add_countdown_anchor(self, countdown_s: float, mono: float, src_now_ms: float, text: str = "") -> str:
        """VOYO shows 'countdown_s' until the session start now -> F1 time now = start - countdown."""
        err = self._need_clock(mono)
        if err:
            return err
        start = self._session_start_ms()
        if start is None:
            return "SYNC: the session start is not known yet (session not identified)"
        video, pb, now_b, _ = self._typed_point(mono, src_now_ms)
        self.captured = None
        f1 = start - countdown_s * 1000
        offset = f1 / 1000 - pb if video else (now_b - f1) / 1000
        detail = f"{text or fmt_hms(countdown_s)} before session start ({utc_str(start)[:8]} UTC)"
        a = Anchor("countdown", offset, f1, pb, time.time(), grounded=True, ref_source="openf1-schedule",
                   error=self.countdown_error, detail=detail)
        m = self._add(a, explicit=True)
        return self._anchor_result(m, a, f"COUNTDOWN {text or fmt_hms(countdown_s)}")

    def add_manual_anchor(self, f1_ms: Optional[float], mono: float, src_now_ms: float, why: str = "",
                          text: str = "") -> str:
        """Manual exact time ('the video shows f1_ms now'); None = pin the time shown now."""
        err = self._need_clock(mono)
        if err:
            return err
        video, pb, now_b = self._now_point(mono, src_now_ms, reaction=False)
        if f1_ms is not None:
            video, pb, now_b, _ = self._typed_point(mono, src_now_ms)
            self.captured = None
        if f1_ms is None:
            f1_ms = self._last_target
            if f1_ms is None or not math.isfinite(f1_ms):
                return "SYNC: nothing to pin - the dashboard has no synchronised time yet" + (f" ({why})" if why else "")
            offset = f1_ms / 1000 - pb if video else (now_b - f1_ms) / 1000
            m = self._add(Anchor("pin", offset, f1_ms, pb, time.time(), detail="shown time pinned"), remove_pins=False)
            return self._result(m, "PINNED" + (f" ({why})" if why else ""))
        bad = self._outside_session(f1_ms)
        if bad:
            return bad
        offset = f1_ms / 1000 - pb if video else (now_b - f1_ms) / 1000
        a = Anchor("exact", offset, f1_ms, pb, time.time(), grounded=True, error=self.exact_error,
                   detail=f"video showed {text or utc_str(f1_ms)[:8] + ' UTC'}")
        m = self._add(a, explicit=True)
        return self._anchor_result(m, a, f"EXACT TIME {text or utc_str(f1_ms)[:8]}")

    def _outside_session(self, f1_ms: float) -> Optional[str]:
        """A typed time far away from the session (wrong time zone / 12-hour clock / wrong day)
        would put the dashboard where no data exists - refuse it with the reason."""
        sess = self.session or {}
        start = parse_utc(sess.get("date_start"))
        end = parse_utc(sess.get("date_end"))
        if start is None:
            return None
        lo = start.timestamp() * 1000 - 3 * 3600_000
        hi = (end.timestamp() * 1000 if end else start.timestamp() * 1000 + 3 * 3600_000) + 3 * 3600_000
        if lo <= f1_ms <= hi:
            return None
        away = (f1_ms - start.timestamp() * 1000) / 3600_000
        return (f"SYNC: {utc_str(f1_ms)[:8]} UTC is {away:+.1f} h from the session start "
                f"({utc_str(start.timestamp() * 1000)[:5]} UTC) - there is no data there. Check the time zone "
                "(Slovenia / track) and 24-hour / 12-hour (AM/PM).")

    def add_event_anchor(self, kind: str, mono: float, src_now_ms: float, driver: Optional[str] = None,
                         label: str = "") -> str:
        """You saw an event on the video now; match it to its timestamp on F1's clock (OpenF1 / feed)."""
        err = self._need_clock(mono)
        if err:
            return err
        ref = self.ref
        if ref is None or ref.empty():
            return "SYNC: no reference events (OpenF1 / archive / feed) - use Countdown or Manual Exact Time"
        if kind == "lap":
            if not driver:
                return "SYNC: no driver selected and no leader known"
            cands = [(t, lap) for t, lap in ref.crossings.get(str(driver), [])]
            what = f"{label or driver} line crossing"
        elif kind == "start":
            cands = [(t, None) for t in ref.starts]
            what = "session start"
        else:
            cands = [(t, None) for t in ref.finishes]
            what = "session finish"
        if not cands:
            return f"SYNC: no {what} in the reference data ({ref.source})"
        video, pb, press_b = self._now_point(mono, src_now_ms, reaction=True)
        prior = self._solve(video=video)
        if prior.offset is None or prior.confidence == "LIVE" and video:
            est = None
        else:
            est = (pb + (self.K if video and self.K is not None else prior.offset)) * 1000 if video \
                else press_b - prior.offset * 1000
        if est is None and kind == "lap":
            return "SYNC: not synchronised yet - first use Countdown / Exact Time or L at the session start"
        if est is None and len(cands) == 1:
            est = cands[0][0]
        if est is None and len(cands) > 1:
            sched = self._session_start_ms()
            after = [c for c in cands if sched is None or c[0] >= sched - 600_000]
            est = (after or cands)[0][0]
        if prior.offset is None or (kind != "lap" and len(cands) == 1):
            window = INF                                   # a unique event needs no prior
        elif prior.error is None:
            window = self.window_s * 1000
        else:
            window = max(3 * prior.error + 2, 5.0) * 1000
        in_win = [c for c in cands if abs(c[0] - est) <= window]
        if not in_win:
            return f"SYNC: no {what} within ±{window / 1000:.0f} s of the current time - check the session / sync"
        best = min(in_win, key=lambda c: abs(c[0] - est))
        ambiguous = len(in_win) > 1
        unique_global = len(cands) == 1
        grounded = (not ambiguous) and (unique_global or (prior.offset is not None and prior.grounded))
        ev_ms, lap = best
        offset = ev_ms / 1000 - pb if video else (press_b - ev_ms) / 1000
        a = Anchor(kind, offset, ev_ms, pb, time.time(), driver if kind == "lap" else None, lap, grounded,
                   ambiguous, False, ref.source, error=self.anchor_error,
                   paused=bool(video and self.clock.last and self.clock.last.paused))
        m = self._add(a)
        head = f"{kind.upper()} {label}" + (f" lap {lap}" if lap is not None else "")
        return self._anchor_result(m, a, head.strip() + ("" if grounded else " (lap not confirmed)"))

    def _anchor_result(self, m: Mapping, a: Anchor, head: str) -> str:
        p = self.pending
        if p and a in p["anchors"]:
            txt = (f"POSSIBLE SYNC DRIFT: {head} differs by {p['shift']:+.2f} s - not applied "
                   f"(Keep old / Use new)")
            self.last_result = txt
            return txt
        txt = self._result(m, head)
        if self.replaced_note and a in self.anchors:
            txt += " · " + self.replaced_note
        if self.deviation_note and a in self.anchors:
            txt += " · minor deviation"
        return txt

    def _rematch_ungrounded(self, offset: float, video: bool) -> None:
        ref = self.ref
        for x in self.anchors:
            if x.grounded or x.kind != "lap" or x.video_time is None or not video or ref is None:
                continue
            est = (x.video_time + offset) * 1000
            near = [(t, lap) for t, lap in ref.crossings.get(str(x.driver), []) if abs(t - est) <= 5000]
            if len(near) == 1:
                x.f1_ms, x.lap = near[0]
                x.offset = x.f1_ms / 1000 - x.video_time
                x.grounded, x.ambiguous = True, False
            else:
                x.outlier = True
        self.anchors = [x for x in self.anchors if x.grounded or x.kind != "lap" or not x.outlier]

    def confirm(self, mono: float) -> str:
        """You checked the TV (lap counter / session clock): the unconfirmed anchors are right."""
        n = 0
        for a in self.anchors:
            if not a.grounded:
                a.grounded, a.ambiguous = True, False
                n += 1
        if not n:
            return "SYNC: nothing to confirm"
        m = self._solve()
        self.K = m.offset
        self._pending_jump = True
        self._save_asset()
        return self._result(m, f"CONFIRMED {n}")

    def clear_anchor(self) -> str:
        for x in self.anchors:
            x.status = "replaced"
        self.history = (self.history + self.anchors)[-30:]
        self.anchors = []
        self.pending = None
        self.deviation_note = None
        self.trim = 0.0
        m = self._solve()
        self._apply_mapping()
        self._pending_jump = True
        self._save_asset()
        return self._result(m, "SYNC CLEARED")

    def set_estimate(self, lead_s: Optional[float]) -> str:
        """Session Start Estimate: the video starts lead_s before the scheduled start (None = remove)."""
        name = str((self.session or {}).get("session_name") or "?")
        if lead_s is None:
            self.store.data["lead_user"].pop(name, None)
        else:
            self.store.data["lead_user"][name] = float(lead_s)
        self.store.save()
        m = self._solve()
        self._apply_mapping()
        self._pending_jump = True
        if lead_s is None:
            return self._result(m, "ESTIMATE REMOVED")
        if self.anchors:
            return self._result(m, f"ESTIMATE SAVED ({fmt_hms(lead_s)}) - anchors still take precedence")
        return self._result(m, f"ESTIMATE {fmt_hms(lead_s)} before start")

    def auto_sync(self, mono: float) -> dict:
        """Automatic: only what is reliable without your input. Never applied silently."""
        reasons = []
        if self.clock.last is None or not self.clock.fresh(mono):
            reasons.append("no VOYO playback clock")
        if self.session is None:
            reasons.append(self.detect_reason or "session not identified")
        if self.anchors:
            m = self._solve()
            self._apply_mapping()
            restored = any(a.restored for a in self.anchors)
            return {"applied": True, "confidence": m.confidence,
                    "message": ("Using the sync saved for this video and session. " if restored else
                                "Existing anchors re-checked. ") + m.reason}
        if self._restore_asset():
            m = self._solve()
            self._apply_mapping()
            self._pending_jump = True
            return {"applied": True, "confidence": m.confidence,
                    "message": "Restored the sync saved for this video and session. " + m.reason}
        reasons += ["the VOYO picture cannot be read (DRM)",
                    "the HLS playlist has no PROGRAM-DATE-TIME / DATERANGE",
                    "the page metadata (mediaId, title, length, startAt) carries no UTC time"]
        lead, n = self.learned_lead()
        msg = "Automatic sync is not reliable enough: " + "; ".join(reasons) + \
              ". Use VOYO Countdown or Manual Exact Time."
        if lead is not None:
            msg += f" Suggestion from {n} earlier sync(s): the video starts {fmt_hms(lead)} before the session " \
                   f"start - apply it with Session Start Estimate (it will be marked Estimated)."
        return {"applied": False, "confidence": self.mapping.confidence, "message": msg,
                "suggested_lead": lead}

    def adjust(self, seconds: float, mono: float, src_now_ms: float) -> str:
        """+x = more delay (the dashboard shows older data)."""
        if self.mapping.offset is None:
            return "SYNC: not synchronised - nothing to adjust"
        if self.mapping.confidence == "LIVE":
            if self.mode_cfg == "LIVE":
                self.mode_cfg = "AUTO"
            self.delay = max(0.0, self.delay + seconds)
            self._solve()
            self._pending_jump = True
            return f"SYNC {self._fmt(mono, src_now_ms)}"
        self.trim += seconds
        self._solve()
        self.K = self.mapping.offset
        self._pending_jump = True
        self._save_asset()
        return f"SYNC {self._fmt(mono, src_now_ms)} (MANUAL)"

    def resync(self, mono: float, src_now_ms: float) -> str:
        """Re-sync from the current anchors: drop manual adjustments, recompute (median / outliers)
        and apply at once - an explicit action, so a jump is expected."""
        self.trim = 0.0
        self.deviation_note = None
        m = self._solve()
        if m.offset is None:
            return self._result(m, "RESYNC: nothing to sync from")
        self.K = m.offset
        self._pending_jump = True
        self._save_asset()
        return self._result(m, "RESYNC")

    def _fmt(self, mono: float, src_now_ms: float) -> str:
        m = self.mapping
        if self.vod:
            return f"{self.trim:+.2f}s"
        if self._video_mode(mono) and self.K is not None and self.clock.last is not None:
            return f"{src_now_ms / 1000 - (self.clock.pb_at(mono) + self.K):.2f}s"
        return f"{m.offset:.2f}s" if m.offset else "LIVE"

    # ------------------------------------------------------------------ state
    def get_state(self, mono: float, src_now_ms: float, timeline) -> dict:
        m = self.mapping
        s = self.clock.last
        pb = self.clock.pb_at(mono) if s is not None else None
        edge = self.edge.edge_at(mono)
        tgt = self._last_target
        synced = m.confidence in ("HIGH", "MEDIUM", "MANUAL") and tgt is not None and m.offset is not None \
            and self.active != "HOLD"
        reason = m.reason if self.active != "HOLD" else "VOYO playback clock unavailable - holding the last time."
        flags = []
        if s is not None and not self.clock.fresh(mono) and (self.vod or self.use_voyo):
            flags.append("VOYO_CLOCK_LOST")
        if self.active == "VOYO" and self.clock.rate_now() == 0 and s is not None:
            flags.append(self.clock.state(mono))
        if timeline is not None and timeline.buffer_exceeded:
            flags.append("BUFFER_EXCEEDED")
        if tgt is not None and timeline is not None and timeline.latest_event_ms > -INF and \
                tgt > timeline.latest_event_ms + 2000 and not self.vod:
            flags.append("VIDEO_AHEAD_OF_DATA")
        if self.K is not None and m.offset is not None and abs(m.offset - self.K) > 0.01:
            flags.append("DRIFT_CORRECTING")
        sess = self.session or {}
        start = self._session_start_ms()
        tv_delay = None
        if tgt is not None and not self.vod and m.confidence != "LIVE":
            tv_delay = round((src_now_ms - tgt) / 1000, 3)
        video_mode = m.offset is not None and (self.vod or self.active == "VOYO")
        lead = round(start / 1000 - m.offset, 1) if (video_mode and start is not None) else None
        learned, n_learned = self.learned_lead()
        return {
            "type": "sync",
            # --- the state of the spec -----------------------------------------
            "synced": synced,
            "absoluteTime": utc_iso(tgt) if synced else None,
            "approxTime": utc_minute(tgt) if (m.confidence == "LOW" and tgt is not None) else None,
            "sessionKey": sess.get("session_key"),
            "sessionName": sess.get("session_name"),
            "meetingName": sess.get("meeting_name"),
            "countryCode": sess.get("country_code"),
            "sessionStart": sess.get("date_start"),
            "gmtOffset": sess.get("gmt_offset"),
            # qualifying / practice: phases (Q1/Q2/Q3, FP) with their official clock + SYNC HERE markers
            "sessionKind": self.session_kind(),
            "sessionTimeline": self._timeline_state(),
            "offsetSeconds": round(m.offset, 3) if m.offset is not None else None,
            # only a MEASURED error (spread of independent anchors); stated ranges are in healthError
            "errorSeconds": round(m.error, 2) if m.error is not None and m.error_measured
            and m.confidence not in ("LOW",) else None,
            "confidence": m.confidence,
            "source": m.source,
            "method": m.method,
            "anchor": m.anchor,
            "reason": reason,
            # offset in human terms: which UTC instant video 0:00 is, where the session start is
            "videoZeroUtc": utc_str(m.offset * 1000) if video_mode and m.confidence != "LOW" else None,
            "leadSeconds": lead if m.confidence != "LOW" else (round(lead) if lead is not None else None),
            # --- details ----------------------------------------------------------
            "enabled": self.enabled, "vod": self.vod, "mode_cfg": self.mode_cfg, "mode": self.active,
            "tv_delay": tv_delay,
            "estimateLead": self.user_lead(), "learnedLead": learned, "learnedLeadCount": n_learned,
            "lastResult": self.last_result,
            "anchors": [self._anchor_row(a, m, start) for a in self.anchors[-10:]],
            "history": [self._anchor_row(a, m, start) for a in self.history[-6:]],
            # --- SYNC HEALTH: how good the time is (measured when possible, never invented) ----------
            "health": m.health if synced or m.health in ("LOW", "UNSYNCED", "LIVE") else
            ("UNSYNCED" if m.offset is None else m.health),
            "healthError": m.health_error,
            "errorMeasured": m.error_measured,
            "anchorsValid": m.n_valid, "anchorsOutliers": m.n_outlier, "anchorsIndependent": m.independent,
            "deviationSeconds": None if m.deviation is None else round(m.deviation, 2),
            "offsetDisplay": self._disp(m.offset, start),
            "offsetDisplayLabel": "session start at video" if start is not None else "offset",
            "deviationNote": self.deviation_note,
            "drift": None if not self.pending else {
                "current": self._disp(self.pending["old"], start),
                "new": self._disp(self.pending["new"], start),
                "shift": round(self.pending["shift"], 2),
                "anchors": [self._anchor_row(a, m, start) for a in self.pending["anchors"]],
                "agree": self.pending["agree"]},
            "ref_source": self.ref.source if self.ref else None,
            "ref_events": self.ref.count() if self.ref else 0,
            "trim": round(self.trim, 2),
            "voyo": None if s is None else {
                "state": self.clock.state(mono),
                "playback": None if pb is None else round(pb, 3),
                "rate": s.rate, "paused": s.paused, "ready_state": s.ready,
                "duration": s.duration, "buffered_end": s.buffered_end, "seekable_end": s.seekable_end,
                "behind_live": None if edge is None or pb is None or self.vod else round(edge - pb, 2),
                "live_edge": "measured" if edge is not None and not self.vod else "unknown",
                "meta": s.meta, "page_title": (self.page or {}).get("media_title") or (self.page or {}).get("title"),
                "age_s": round(mono - s.mono, 2),
                "events": [e for _, e in list(self.clock.recent_events)[-5:]],
            },
            "f1_target_utc": utc_str(tgt) if synced else None,
            "f1_latest_utc": utc_str(timeline.latest_event_ms) if timeline is not None
            and timeline.latest_event_ms > -INF and not self.vod else None,
            "receive_latency": timeline.receive_latency_s() if timeline is not None else None,
            "buffer": timeline.buffered_seconds() if timeline is not None else None,
            "holdback": timeline.ahead_seconds() if timeline is not None and not self.vod else None,
            "flags": flags,
            "note": self.note,
            "detection": self.detect_reason,
            "step": self.step,
        }

    status = get_state

    def _disp(self, offset: Optional[float], start: Optional[float]) -> Optional[float]:
        """Offset as the user reads it: video position (s) of the scheduled session start
        (video mode, session known); + means the start is later in the video."""
        if offset is None:
            return None
        if self.vod or self.active == "VOYO":
            return round(start / 1000 - offset, 2) if start is not None else None
        return round(offset, 2)                       # fixed delay: the delay itself

    def _anchor_row(self, a: Anchor, m: Mapping, start: Optional[float]) -> dict:
        state = a.status or ("outlier" if a.outlier else "unconfirmed" if not a.grounded else "valid")
        shift = None
        if m.offset is not None and a.kind != "pin" and not a.status:
            shift = round(self._shift(a.video_time is not None, a.offset, m.offset), 2)
        label = a.label or {"lap": f"S lap {a.lap}" if a.lap is not None else "S", "start": "L", "finish": "Finish",
                            "countdown": "Countdown", "exact": "Exact time", "pin": "Pin"}.get(a.kind, a.kind)
        return {"kind": a.kind, "label": label, "method": METHOD_LABEL.get(a.kind, a.kind),
                "text": self._anchor_text(a), "driver": a.driver, "lap": a.lap, "state": state,
                "restored": a.restored, "paused": a.paused, "ref": a.ref_source,
                "offset": self._disp(a.offset, start), "residual": shift}


SyncEngine = SyncManager          # old name


def utc_iso(ms: Optional[float]) -> Optional[str]:
    if ms is None or not math.isfinite(ms):
        return None
    return datetime.fromtimestamp(ms / 1000, timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def utc_minute(ms: Optional[float]) -> Optional[str]:
    if ms is None or not math.isfinite(ms):
        return None
    return datetime.fromtimestamp(ms / 1000, timezone.utc).strftime("%H:%M") + " UTC"
