"""Raw feed state: applies F1 live-timing differential updates.

The F1 feed sends a full snapshot of each topic once (the Subscribe result or
a "keyframe") and afterwards only partial updates. Partial updates may address
list items with dict keys ("0", "1", ...) and remove items with "_deleted".
"""
from __future__ import annotations

import copy
import logging
from typing import Any, Optional

from .chase import track_chase
from .laps import activity, track_laps

log = logging.getLogger("feedstate")

_META_KEYS = {"_kf"}
IN_PIT_SINCE = "_InPitSince"      # {num: [F1 time ms the car went InPit, stale]} (derived, not a feed topic)
STOPPED_SINCE = "_StoppedSince"    # {num: F1 time ms the car reported Stopped (and did not move since)}
PIT_ACTIVITY_MS = 45_000           # timing activity later than this after going in = racing, not in the pit
TIMES = "_Times"                   # {topic: F1 time ms of its last message} (derived)


def _index(key: Any) -> int | None:
    try:
        return int(key)
    except (TypeError, ValueError):
        return None


def deep_merge(target: Any, update: Any) -> Any:
    """Merge ``update`` into ``target`` in place and return the result."""
    if not isinstance(update, dict):
        return copy.deepcopy(update)

    if isinstance(target, list):
        deleted = update.get("_deleted")
        for key, value in update.items():
            if key in _META_KEYS or key == "_deleted":
                continue
            idx = _index(key)
            if idx is None or idx < 0 or idx > 10000:
                continue
            while len(target) <= idx:
                target.append(None)
            current = target[idx]
            if isinstance(value, dict) and isinstance(current, (dict, list)):
                deep_merge(current, value)
            else:
                target[idx] = copy.deepcopy(value)
        if deleted:
            for idx in sorted({_index(d) for d in deleted if _index(d) is not None}, reverse=True):
                if 0 <= idx < len(target):
                    target.pop(idx)
        return target

    if not isinstance(target, dict):
        target = {}
    for key, value in update.items():
        if key in _META_KEYS:
            continue
        if key == "_deleted":
            for d in value or []:
                target.pop(str(d), None)
            continue
        current = target.get(key)
        if isinstance(value, dict) and isinstance(current, (dict, list)):
            deep_merge(current, value)
        else:
            target[key] = copy.deepcopy(value)
    return target


def _line_summary(line: dict) -> dict:
    ll = line.get("LastLapTime")
    secs = line.get("Sectors")
    items = secs if isinstance(secs, list) else [secs.get(str(i)) for i in range(3)] if isinstance(secs, dict) else []
    n = line.get("NumberOfLaps")
    try:
        n = int(n) if n is not None else None
    except (TypeError, ValueError):
        n = None
    return {"n": n, "ll": ll.get("Value") if isinstance(ll, dict) else None,
            "sv": [(x.get("Value") if isinstance(x, dict) else None) for x in list(items)[:3]]}


class FeedState:
    """Holds the merged raw state of every non-streaming topic."""

    def __init__(self) -> None:
        self.topics: dict[str, Any] = {}

    def reset(self) -> None:
        self.topics.clear()

    # Topics that carry the same content under a second name. They are merged
    # into one state so that an update missing on one of them is not lost.
    ALIASES = {"TimingDataF1": "TimingData"}

    def apply(self, topic: str, data: Any, snapshot: bool = False, t_ms: Optional[float] = None) -> None:
        # "_kf": true marks a keyframe (complete state of the topic)
        keyframe = isinstance(data, dict) and data.get("_kf") is True
        if keyframe:
            snapshot = True
        # a live update or not is decided before the twin-topic rule below: a snapshot of
        # TimingDataF1 is merged (not replaced), but it is still a snapshot, not activity
        live = not snapshot and t_ms is not None and isinstance(data, dict)
        if topic in self.ALIASES:
            topic = self.ALIASES[topic]
            if topic in self.topics:
                snapshot = False           # never let the twin topic wipe the merged state
        prev: dict = {}
        if live and topic == "TimingData":
            # what each car showed before this update: a crossing = NumberOfLaps going up; a lap /
            # sector time equal to the one already shown is a re-send (reconnect), not a new one
            lines = (self.topics.get("TimingData") or {}).get("Lines") or {}
            for num in (data.get("Lines") or {}) if isinstance(data.get("Lines"), dict) else ():
                ln = lines.get(num)
                if isinstance(ln, dict):
                    prev[str(num)] = _line_summary(ln)
        if snapshot or topic not in self.topics or not isinstance(data, dict):
            self.topics[topic] = deep_merge({}, data) if isinstance(data, dict) else copy.deepcopy(data)
        else:
            self.topics[topic] = deep_merge(self.topics[topic], data)
        if t_ms is not None and not snapshot and not topic.startswith("_"):
            # F1 time of the last update per topic (derived; restored with checkpoints). A snapshot /
            # keyframe / checkpoint restore says nothing about when the content last changed.
            self.topics.setdefault(TIMES, {})[topic] = t_ms
        if topic == "TimingData" and t_ms is not None:
            # a snapshot / keyframe / checkpoint restore carries every key of every car: it says
            # nothing about who is moving now - only live updates count as activity
            self._track_in_pit(data if live else None, t_ms)
        if live and topic in ("TimingData", "LapSeries"):
            # lap / sector progress (derived topic, restored with checkpoints like the pit state)
            track_laps(self.topics, topic, data, t_ms, prev)
        if live and topic == "TimingData":
            # race: for how many laps each car has been close behind the same car (server/chase.py)
            track_chase(self.topics, data, t_ms, prev)

    def _track_in_pit(self, data: Any, t_ms: float) -> None:
        """Since when (F1 time) each car is continuously InPit - a long stay = the garage.
        Entry: [since, stale]. ``stale``: the car still sends sector / speed / lap data more
        than 45 s after going in - it is racing and the "left the pit" message was lost (seen
        in real 2026 data: InPit stuck for an hour). Kept as a topic so checkpoints / seeks
        restore it with the rest of the state."""
        since = self.topics.setdefault(IN_PIT_SINCE, {})
        stopped = self.topics.setdefault(STOPPED_SINCE, {})
        incoming = (data.get("Lines") if isinstance(data, dict) else None) or {}
        for num, line in ((self.topics.get("TimingData") or {}).get("Lines") or {}).items():
            if not isinstance(line, dict):
                continue
            num = str(num)
            upd = incoming.get(num)
            # (values being cleared - e.g. for every car when a qualifying part begins - are no activity)
            moving = activity(upd)
            if line.get("Stopped") and not moving:
                stopped.setdefault(num, t_ms)
            else:
                stopped.pop(num, None)
            if not line.get("InPit"):
                since.pop(num, None)
                continue
            ent = since.setdefault(num, [t_ms, False])
            if moving and t_ms - ent[0] > PIT_ACTIVITY_MS:
                ent[1] = True

    def get(self, topic: str, default: Any = None) -> Any:
        return self.topics.get(topic, default)
