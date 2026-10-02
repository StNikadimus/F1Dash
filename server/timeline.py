"""Timestamped F1 timeline: "give me the complete F1 state at time X".

Three clocks are kept strictly apart in this project:

* **A - F1 event time** (``event_ms``): when something happened on F1's clock.
  Sources, most precise first: the ``Timestamp`` of every Position.z sample,
  the ``Utc`` of every CarData.z sample, the timestamp argument of every
  SignalR ``feed`` message (``args[2]``, set by F1's server when the update was
  generated) and, for archive/replay data, the offset in the ``.jsonStream``
  line anchored on Heartbeat ``Utc``. RaceControlMessages also carry their own
  ``Utc`` (1 s resolution) - it is rendered, the message timestamp is used for
  ordering. **This is the canonical timeline.**
* **B - receive time** (``receive_ms``): when this server received the
  message. Only used for diagnostics ("F1 RECEIVE LATENCY"), never for sync.
* **C - VOYO playback time**: position of the video element. Lives only in
  :mod:`server.sync`, which converts it into a target on clock A.

The dashboard shows the state *at a target time* on clock A chosen by the sync
engine (live edge, fixed delay or the VOYO video clock). Moving forward applies
the next events; moving backwards (video seek, calibration change) restores the
nearest checkpoint and replays events up to the target. Every part of the
dashboard (timing, positions, telemetry, tyres, pit, penalties, race control,
flags, weather, session state) comes from this one reconstructed state.
"""
from __future__ import annotations

import bisect
import logging
import math
import pickle
from dataclasses import dataclass, field
from statistics import median
from typing import Any, Callable, Optional

from .feedstate import FeedState
from .telemetry import CarDataStore, PositionStore, decode_z, parse_utc

log = logging.getLogger("timeline")

INF = math.inf


@dataclass(slots=True)
class Event:
    event_ms: float           # A: F1 event time
    seq: int
    kind: str                 # topic | pos | car | reset
    topic: str
    payload: Any
    snapshot: bool
    origin: str
    receive_ms: float         # B: receive time (diagnostics only)

    @property
    def key(self) -> tuple[float, int]:
        return (self.event_ms, self.seq)


@dataclass
class Checkpoint:
    t_ms: float
    last_key: tuple[float, int]
    topics: bytes
    positions: dict
    cardata: bytes
    pos_received: bool
    car_received: bool


@dataclass
class AdvanceResult:
    released_positions: list = field(default_factory=list)   # [(event_ms, sample, origin)]
    state_changed: bool = False
    telemetry_changed: bool = False
    discontinuity: bool = False
    replay_positions: list = field(default_factory=list)     # samples to refill the map after a jump
    clamped: bool = False


class Timeline:
    """Event buffer + reconstructable state. Not thread safe (asyncio only)."""

    def __init__(self, buffer_seconds: float = 120.0,
                 on_topic: Optional[Callable[[str], None]] = None,
                 map_time: Callable = lambda dt: int(dt.timestamp() * 1000),
                 hold_max_seconds: float = 1800.0) -> None:
        self.buffer_ms = max(10.0, float(buffer_seconds)) * 1000
        self.hold_max_ms = max(self.buffer_ms, float(hold_max_seconds) * 1000)
        self.ckpt_interval_ms = max(2000.0, min(5000.0, self.buffer_ms / 12))
        self.on_topic = on_topic
        self.map_time = map_time
        self.track_latency = True            # receive latency only means something for live data
        self.on_position_entry: Optional[Callable[[dict], None]] = None   # every new Position entry (pit lane learning)
        self.feed = FeedState()
        self.positions = PositionStore()
        self.cardata = CarDataStore()
        self._init_buffers()

    def _init_buffers(self) -> None:
        self.events: list[Event] = []
        self._keys: list[tuple[float, int]] = []
        self.idx = 0                         # next event to apply
        self.applied_ms = -INF               # state represents F1 time <= applied_ms
        self.checkpoints: list[Checkpoint] = []
        self._seq = 0
        self._last_key: tuple[float, int] = (-INF, -1)
        self._latencies: list[float] = []
        self.latest_event_ms = -INF
        self.buffer_exceeded = False
        self._late_emit: list = []
        self._late_state = False
        self._late_tel = False
        self.replaying = False               # True while rebuilding (callbacks can skip side effects)
        self._emitted_key: tuple[float, int] = (-INF, -1)   # position samples streamed up to here

    # ------------------------------------------------------------------ ingest
    def reset(self) -> None:
        """Forget everything (new replay loop, time jumped backwards on the source)."""
        self.feed.reset()
        self.positions.reset()
        self.cardata.reset()
        self._init_buffers()

    def clear_state(self, keep: tuple[str, ...] = ()) -> None:
        """Clear the *current* state (session change) - called while applying events."""
        self.feed_reset_keep(keep)
        self.positions.reset()
        self.cardata.reset()

    def feed_reset_keep(self, keep: tuple[str, ...]) -> None:
        """Clear the feed topics except ``keep`` (positions / telemetry stay)."""
        kept = {k: self.feed.get(k) for k in keep if self.feed.get(k) is not None}
        self.feed.reset()
        for k, v in kept.items():
            self.feed.apply(k, v, snapshot=True)

    def mark_reset(self, event_ms: float, receive_ms: float) -> None:
        """Insert a 'state was reset here' marker (new subscription snapshot)."""
        self._add(Event(event_ms, 0, "reset", "__reset__", None, True, "feed", receive_ms))

    def ingest(self, topic: str, data: Any, event_ms: Optional[float], receive_ms: float,
               snapshot: bool = False, origin: str = "feed") -> None:
        """Split one feed message into timeline events (clock A), keeping receive time (B)."""
        if topic in ("Position.z", "Position"):
            obj = decode_z(data) if topic.endswith(".z") else data
            for entry in (obj or {}).get("Position") or []:
                if self.on_position_entry is not None:
                    try:
                        self.on_position_entry(entry)
                    except Exception:  # noqa: BLE001 - learning must never break the timeline
                        log.exception("pit lane learning failed on a position entry")
                ts = parse_utc((entry or {}).get("Timestamp"))
                t = ts.timestamp() * 1000 if ts else (event_ms if event_ms is not None else receive_ms)
                self._add(Event(t, 0, "pos", "Position", entry, snapshot, origin, receive_ms))
        elif topic in ("CarData.z", "CarData"):
            obj = decode_z(data) if topic.endswith(".z") else data
            for entry in (obj or {}).get("Entries") or []:
                ts = parse_utc((entry or {}).get("Utc"))
                t = ts.timestamp() * 1000 if ts else (event_ms if event_ms is not None else receive_ms)
                self._add(Event(t, 0, "car", "CarData", entry, snapshot, origin, receive_ms))
        else:
            t = event_ms if event_ms is not None else receive_ms
            self._add(Event(t, 0, "topic", topic, data, snapshot, origin, receive_ms))
            if event_ms is not None and not snapshot and origin == "feed" and self.track_latency:
                self._latencies.append(receive_ms - event_ms)
                if len(self._latencies) > 300:
                    del self._latencies[:100]

    def _add(self, ev: Event) -> None:
        self._seq += 1
        ev.seq = self._seq
        self.latest_event_ms = max(self.latest_event_ms, ev.event_ms)
        key = ev.key
        pos = bisect.bisect_right(self._keys, key)
        self._keys.insert(pos, key)
        self.events.insert(pos, ev)
        if pos < self.idx:
            # Truly late: older than events already applied. Apply now so nothing
            # is lost (positions still reach the map), keep the list sorted.
            self.idx += 1
            if ev.kind in ("topic", "reset"):
                # checkpoints after this point no longer contain it
                self.checkpoints = [c for c in self.checkpoints if c.last_key < key]
                if self._reorder():
                    self._late_state = True
                    return
            emit: list = []
            sc, tc = self._apply(ev, emit, force_emit=True)
            self._late_emit.extend(emit)
            self._late_state |= sc
            self._late_tel |= tc
        # pos >= idx: applied by the next advance() once the target reaches it

    def _reorder(self) -> bool:
        """A feed message older than messages already applied (live: delivered late - real feeds
        do that by up to ~2 s). Its effect must not depend on when it arrived: an older clock /
        track status post applied on top would restart a stopped clock or end a red flag. The
        feed topics are rebuilt from the last checkpoint before it, applying the messages in F1
        time order (as a recording would). Positions / telemetry are timestamped samples and stay.
        False: no checkpoint before it, or a session change in between - applied on top instead."""
        if not self.checkpoints:
            return False
        ck = self.checkpoints[-1]                    # (those after the late message were dropped)
        j = bisect.bisect_right(self._keys, ck.last_key)
        span = [e for e in self.events[j:self.idx] if e.kind in ("topic", "reset")]
        if any(e.topic == "SessionInfo" for e in span):
            return False
        self.feed.topics = pickle.loads(ck.topics)
        self.replaying = True
        try:
            for e in span:
                self._apply(e, None)
        finally:
            self.replaying = False
        return True

    # ------------------------------------------------------------------ apply
    def _apply(self, ev: Event, emit: Optional[list], force_emit: bool = False) -> tuple[bool, bool]:
        """Apply one event to the state. Returns (state_changed, telemetry_changed)."""
        try:
            if ev.kind == "pos":
                samples = self.positions.ingest({"Position": [ev.payload]}, self.map_time)
                if emit is not None and (force_emit or ev.key > self._emitted_key):
                    emit.extend((ev.event_ms, s, ev.origin) for s in samples)
                    if ev.key > self._emitted_key:
                        self._emitted_key = ev.key
                return False, False
            if ev.kind == "car":
                self.cardata.ingest({"Entries": [ev.payload]})
                return False, True
            if ev.kind == "reset":
                # live reconnect: the subscribe snapshot replaces every feed topic, but it has
                # nothing of what is derived from the update history ("_" topics: lap history,
                # since when in the pit / stopped, knocked-out part) - that history is still true
                # for this session (a new session clears it in Engine._check_session). Car
                # positions / telemetry are timestamped samples: kept, so the map does not blank.
                derived = tuple(k for k in self.feed.topics if k.startswith("_"))
                self.feed_reset_keep(derived)
                return True, True
            self.feed.apply(ev.topic, ev.payload, ev.snapshot, ev.event_ms)
            if self.on_topic:
                self.on_topic(ev.topic)
            return True, False
        except Exception:  # noqa: BLE001 - one bad packet must never stop the timeline
            log.exception("Could not apply %s event", ev.topic)
            return False, False
        finally:
            if ev.key > self._last_key:
                self._last_key = ev.key

    # ------------------------------------------------------------------ checkpoints
    def _checkpoint(self) -> None:
        self.checkpoints.append(Checkpoint(
            t_ms=self.applied_ms, last_key=self._last_key,
            topics=pickle.dumps(self.feed.topics, pickle.HIGHEST_PROTOCOL),
            positions=dict(self.positions.latest),
            cardata=pickle.dumps(self.cardata.latest, pickle.HIGHEST_PROTOCOL),
            pos_received=self.positions.received, car_received=self.cardata.received))

    def _restore(self, ck: Checkpoint) -> None:
        self.feed.topics = pickle.loads(ck.topics)
        self.positions.latest = dict(ck.positions)
        self.positions.received = ck.pos_received
        self.cardata.latest = pickle.loads(ck.cardata)
        self.cardata.received = ck.car_received
        self.applied_ms = ck.t_ms
        self._last_key = ck.last_key
        self.idx = bisect.bisect_right(self._keys, ck.last_key)

    # ------------------------------------------------------------------ advance
    def advance(self, target_ms: float, pos_lookahead_ms: float = 0.0) -> AdvanceResult:
        """Bring the state to F1 event time ``target_ms`` (INF = everything received).

        ``pos_lookahead_ms``: position samples up to this much *after* the target
        are streamed early (not applied) so the browser can interpolate the car
        markers exactly at the video's time instead of lagging behind it.
        """
        res = AdvanceResult()
        clamped = False
        # a paused video may hold the state for a long time - but not forever
        if self.latest_event_ms > -INF and target_ms < self.latest_event_ms - self.hold_max_ms:
            target_ms = self.latest_event_ms - self.hold_max_ms
            clamped = True

        if target_ms < self.applied_ms - 50:
            if self.checkpoints:
                c, target_ms = self._rebuild(target_ms, res)
                clamped |= c
            else:
                clamped = True                 # nothing to go back to: hold current state
        emit: list = res.released_positions
        if self._late_emit:
            emit.extend(self._late_emit)
            self._late_emit = []
        res.state_changed |= self._late_state
        res.telemetry_changed |= self._late_tel
        self._late_state = self._late_tel = False
        while self.idx < len(self.events) and self.events[self.idx].event_ms <= target_ms:
            sc, tc = self._apply(self.events[self.idx], emit)
            res.state_changed |= sc
            res.telemetry_changed |= tc
            self.idx += 1
        if target_ms > self.applied_ms:
            self.applied_ms = target_ms if target_ms < INF else max(self.applied_ms, self.latest_event_ms)
        if pos_lookahead_ms > 0 and target_ms < INF:
            self._peek_positions(target_ms + pos_lookahead_ms, emit)
        if self.events and self.applied_ms > -INF and (
                not self.checkpoints or self.applied_ms - self.checkpoints[-1].t_ms >= self.ckpt_interval_ms):
            self._checkpoint()
            self._trim()
        self.buffer_exceeded = clamped
        res.clamped = clamped
        return res

    def _peek_positions(self, until_ms: float, emit: list) -> None:
        j = bisect.bisect_right(self._keys, self._emitted_key)
        j = max(j, self.idx)
        scratch = PositionStore()
        while j < len(self.events) and self.events[j].event_ms <= until_ms:
            ev = self.events[j]
            if ev.kind == "pos" and ev.key > self._emitted_key:
                for smp in scratch.ingest({"Position": [ev.payload]}, self.map_time):
                    emit.append((ev.event_ms, smp, ev.origin))
                self._emitted_key = ev.key
            j += 1

    def _rebuild(self, target_ms: float, res: AdvanceResult) -> tuple[bool, float]:
        cands = [c for c in self.checkpoints if c.t_ms <= target_ms]
        clamped = not cands
        ck = cands[-1] if cands else self.checkpoints[0]
        target_ms = max(target_ms, ck.t_ms)
        self._restore(ck)
        self._emitted_key = ck.last_key
        # newer checkpoints are recreated on the way forward
        self.checkpoints = [c for c in self.checkpoints if c.t_ms <= ck.t_ms]
        # the car positions at the checkpoint itself, so the map has a sample before the target
        replay: list = []
        by_t: dict = {}
        for num, v in self.positions.latest.items():
            t, x, y, status = v[0], v[1], v[2], v[3]
            by_t.setdefault(t, []).append([num, x, y, 1 if status == "OnTrack" else 0])
        for t in sorted(by_t):
            replay.append((ck.t_ms, {"t": t, "cars": by_t[t]}, "checkpoint"))
        self.replaying = True
        try:
            while self.idx < len(self.events) and self.events[self.idx].event_ms <= target_ms:
                self._apply(self.events[self.idx], replay)
                self.idx += 1
        finally:
            self.replaying = False
        self.applied_ms = target_ms
        self._late_emit = []
        res.discontinuity = True
        res.state_changed = True
        res.telemetry_changed = True
        res.replay_positions = [r for r in replay if r[0] >= target_ms - 6000]
        return clamped, target_ms

    def _trim(self) -> None:
        """Drop events older than ``buffer`` behind the shown state; never drop unapplied events."""
        if self.applied_ms == -INF:
            return
        cutoff = min(self.applied_ms, self.latest_event_ms) - self.buffer_ms
        base = None
        for c in self.checkpoints:
            if c.t_ms <= cutoff:
                base = c
        if base is None:
            return
        self.checkpoints = [c for c in self.checkpoints if c.t_ms >= base.t_ms]
        n = min(bisect.bisect_right(self._keys, base.last_key), self.idx)
        if n > 0:
            del self.events[:n]
            del self._keys[:n]
            self.idx -= n

    # ------------------------------------------------------------------ info
    def oldest_ms(self) -> float:
        """Oldest F1 time the state can be rebuilt for."""
        if self.checkpoints:
            return self.checkpoints[0].t_ms
        return self.applied_ms

    def buffered_seconds(self) -> Optional[float]:
        """How far back from the shown state a seek can go."""
        if self.applied_ms == -INF or not self.checkpoints:
            return None
        return round(max(0.0, (min(self.applied_ms, self.latest_event_ms) - self.oldest_ms()) / 1000), 1)

    def ahead_seconds(self) -> Optional[float]:
        """Received data that is not shown yet (the hold-back of the delay/video)."""
        if self.applied_ms == -INF or self.latest_event_ms == -INF:
            return None
        return round(max(0.0, (self.latest_event_ms - self.applied_ms) / 1000), 2)

    def receive_latency_s(self) -> Optional[float]:
        if len(self._latencies) < 5:
            return None
        return round(median(self._latencies[-150:]) / 1000, 2)
