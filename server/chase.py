"""CHASING: for how many laps a car has been close behind the same car ahead (race only).

Derived from the official timing only, at each of the car's own line crossings (a completed lap):

* the car ahead = the car one position in front (TimingData Position / Line) at that moment;
* the gap = ``IntervalToPositionAhead`` at that moment (a lapped "1 L" interval is no chase);
* a lap counts when the gap is <= ``CHASE_GAP_S`` (configurable, [dashboard] chase_gap_seconds)
  behind the SAME car as on the previous counted lap.

The chase ends (counter back to 0) when the gap is larger, the car ahead is another one (an
overtake, the car ahead pitted or was passed), the car itself went through the pit lane on that
lap, or the lap was run under a safety car / VSC / red flag (everybody is close then - no chase).
So a whole race in the same position is never one chase: only consecutive close laps count.

Kept as a derived feed topic (like the lap tracker), so a seek / VOD checkpoint restores it.
"""
from __future__ import annotations

from typing import Any, Optional

CHASE = "_Chase"           # {num: {"ahead": num|None, "laps": int, "since_ms": F1 ms|None, "gap": s|None}}
CHASE_GAP_S = 1.5          # seconds behind the car ahead that count as chasing (set from the config)
NEUTRAL = {"4", "5", "6", "7"}   # TrackStatus: SC, red, VSC, VSC ending


def gap_s(v: Any) -> Optional[float]:
    """'+0.512' / '0.512' -> 0.512; '1 L', 'LAP 3', '' -> None."""
    if isinstance(v, dict):
        v = v.get("Value")
    if not isinstance(v, str):
        return None
    s = v.strip().lstrip("+")
    try:
        f = float(s)
    except ValueError:
        return None
    return f if f >= 0 else None


def _int(v: Any) -> Optional[int]:
    try:
        p = int(v)
    except (TypeError, ValueError):
        return None
    return p if p > 0 else None


def positions(topics: dict) -> dict[str, int]:
    """Running order like the normalizer: TimingData Position, or - when those are not a clean
    sequence (a lossy recording) - the classification line DriverList publishes."""
    lines = (topics.get("TimingData") or {}).get("Lines") or {}
    pos = {str(k): _int(ln.get("Position")) for k, ln in lines.items() if isinstance(ln, dict)}
    vals = [p for p in pos.values() if p is not None]
    if vals and len(vals) == len(set(vals)) and None not in pos.values():
        return pos
    dl = topics.get("DriverList") or {}
    alt = {k: _int((dl.get(k) or {}).get("Line")) if isinstance(dl.get(k), dict) else None for k in pos}
    vals = [p for p in alt.values() if p is not None]
    return alt if vals and len(vals) == len(set(vals)) else pos


def track_chase(topics: dict, data: Any, t_ms: float, prev: dict) -> None:
    """After a live TimingData update was merged: update the chase of every car that crossed the
    line in it (NumberOfLaps went up; ``prev`` = what each car showed before, feedstate)."""
    lines = (topics.get("TimingData") or {}).get("Lines") or {}
    incoming = (data.get("Lines") if isinstance(data, dict) else None) or {}
    if not isinstance(lines, dict) or not isinstance(incoming, dict):
        return
    store = topics.setdefault(CHASE, {})
    status = str(((topics.get("TrackStatus") or {}).get("Status")) or "")
    laps_store = topics.get("_Laps") or {}
    order: Optional[dict] = None
    for num, upd in incoming.items():
        if not isinstance(upd, dict) or upd.get("NumberOfLaps") is None:
            continue
        try:
            n = int(upd["NumberOfLaps"])
        except (TypeError, ValueError):
            continue
        before = (prev.get(str(num)) or {}).get("n")
        if before is None or n <= before:
            continue                                   # not a new completed lap
        line = lines.get(str(num)) or {}
        if order is None:
            order = positions(topics)
        me = order.get(str(num))
        ahead = None
        if me and me > 1:
            ahead = next((k for k, p in order.items() if p == me - 1), None)
        gap = gap_s(line.get("IntervalToPositionAhead"))
        st = laps_store.get(str(num)) or {}
        done = st.get("done") or {}
        via_pit = bool(line.get("InPit") or line.get("PitOut") or done.get("pit"))
        old = store.get(str(num)) or {}
        close = (ahead is not None and gap is not None and gap <= CHASE_GAP_S and not via_pit
                 and status not in NEUTRAL)
        if close and old.get("ahead") == ahead and old.get("laps"):
            store[str(num)] = {"ahead": ahead, "laps": old["laps"] + 1, "since_ms": old.get("since_ms"), "gap": gap}
        elif close:
            store[str(num)] = {"ahead": ahead, "laps": 1, "since_ms": t_ms, "gap": gap}
        else:
            store[str(num)] = {"ahead": ahead, "laps": 0, "since_ms": None, "gap": gap,
                               "why": "leader" if me == 1 else "pit" if via_pit else
                               "neutral" if status in NEUTRAL else "gap" if ahead is not None else None}
