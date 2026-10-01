"""What a car is doing in qualifying: OUT LAP / PREP / HOT LAP / COOLDOWN / PIT / UNKNOWN.

The timing feed has no field for it (checked on the real Suzuka 2026 qualifying data), so this is
a deterministic, per-driver classification from official timing only - never a single global
speed or time threshold. Every value is compared with that driver's OWN pace (his personal best
sectors / lap / finish-line speed so far), and with the session's best only when the driver has
not set a representative time yet.

Evidence, strongest first:

* in the pit lane / garage                                  -> PIT          (HIGH)
* the lap began at the pit exit                            -> OUT LAP      (HIGH)
* sector times of the lap being driven, as a ratio to the reference:
    <= FAST (3 %)  -> HOT LAP;  >= SLOW (10 %) -> PREP, or COOLDOWN when the lap before was a hot
    lap; in between -> UNKNOWN. Two or more sectors -> HIGH, one -> MEDIUM.
* nothing completed yet in this lap:
    - the time spent in the current sector is already far over the reference -> slow (MEDIUM)
    - the finish-line speed with which the lap began, after an out / preparation lap:
      close to the driver's best (>= 97 %) -> HOT LAP (MEDIUM), clearly lower (<= 90 %) -> PREP
* otherwise UNKNOWN (never a guess). A hot lap followed by a slow one is a COOLDOWN; a slow lap
  after an out / slow lap is a PREP - so OUT -> PREP -> PREP -> HOT and HOT -> COOLDOWN -> HOT are
  told apart from the laps' own times.

Real Suzuka 2026 qualifying (LEC): hot S1 31.8-32.4 s vs 39-45 s on preparation / cooldown laps,
lap 1:29-1:30 vs 1:43-1:58 - the ratios above sit far from both.
"""
from __future__ import annotations

from typing import Any, Optional

FAST = 1.03            # sector / lap ratio to the driver's reference: a push
SLOW = 1.10            # sector ratio: clearly not a push
LAP_SLOW = 1.08        # lap ratio: clearly not a push
FL_FAST = 0.97         # finish-line speed ratio (to the driver's best) when a flying lap begins
FL_SLOW = 0.90
ELAPSED_SLOW = 1.20    # time in the current sector already this far over its reference
OWN_WITHIN = 1.04      # the driver's own best counts as his pace when within 4 % of the session best
ESTABLISHED_N = 3      # cars with a representative lap before the session pace is used at all

STATES = ("OUT LAP", "PREP", "HOT LAP", "COOLDOWN", "PIT", "UNKNOWN")


def t_ms(v: Any) -> Optional[int]:
    if not isinstance(v, str) or not v.strip():
        return None
    try:
        parts = [float(p) for p in v.strip().split(":")]
    except ValueError:
        return None
    t = 0.0
    for p in parts:
        t = t * 60 + p
    return int(round(t * 1000)) if t > 0 else None


def _items(v: Any) -> list:
    if isinstance(v, list):
        return v
    if isinstance(v, dict):
        out = []
        for k, x in v.items():
            try:
                out.append((int(k), x))
            except (TypeError, ValueError):
                continue
        return [x for _, x in sorted(out, key=lambda p: p[0])]
    return []


def _int(v: Any) -> Optional[int]:
    try:
        return int(str(v).strip())
    except (TypeError, ValueError):
        return None


def pace_refs(lines: dict, stats: dict) -> dict:
    """Reference pace at the shown moment (from the merged timing state = known up to now)."""
    sec = {}           # num -> [ms|None]*3  personal best sectors
    lap = {}           # num -> ms            personal best lap (any part)
    fl = {}            # num -> km/h          best finish-line speed
    for num, st in (stats or {}).items():
        if not isinstance(st, dict):
            continue
        sec[str(num)] = [t_ms(x.get("Value")) if isinstance(x, dict) else None
                         for x in (_items(st.get("BestSectors")) + [None] * 3)[:3]]
        bsp = st.get("BestSpeeds") or {}
        f = bsp.get("FL") if isinstance(bsp, dict) else None
        v = _int(f.get("Value")) if isinstance(f, dict) else None
        if v:
            fl[str(num)] = v
    for num, ln in (lines or {}).items():
        if not isinstance(ln, dict):
            continue
        vals = [t_ms(x.get("Value")) for x in _items(ln.get("BestLapTimes")) if isinstance(x, dict)]
        bl = ln.get("BestLapTime")
        vals.append(t_ms(bl.get("Value")) if isinstance(bl, dict) else None)
        vals = [v for v in vals if v]
        if vals:
            lap[str(num)] = min(vals)
    s_best = [min([v[k] for v in sec.values() if v[k]], default=None) for k in range(3)]
    l_best = min(lap.values(), default=None)
    established = l_best is not None and \
        sum(1 for v in lap.values() if v <= l_best * FAST) >= ESTABLISHED_N and all(s_best)
    return {"sec": sec, "lap": lap, "fl": fl, "s_best": s_best, "l_best": l_best,
            "fl_best": max(fl.values(), default=None), "established": established}


def driver_ref(refs: dict, num: str) -> Optional[dict]:
    """The driver's own pace where it is representative, else the session's."""
    if not refs.get("established"):
        return None
    own = refs["sec"].get(num) or [None] * 3
    sec = [o if (o and sb and o <= sb * OWN_WITHIN) else sb for o, sb in zip(own, refs["s_best"])]
    ol = refs["lap"].get(num)
    lap = ol if (ol and ol <= refs["l_best"] * OWN_WITHIN) else refs["l_best"]
    return {"sec": sec, "lap": lap, "fl": refs["fl"].get(num) or refs.get("fl_best")}


def _sector_ratios(cs: list, ref: dict) -> list[float]:
    return [t_ms(v) / r for v, r in zip(cs or [], ref["sec"]) if t_ms(v) and r]


def _sector_ratio(cs: list, ref: dict) -> tuple[Optional[float], int]:
    """Consistent sectors -> their overall ratio; a lap with pushed AND clearly slow sectors (a
    push that was given up) -> (None, n): it is neither a hot nor a preparation lap."""
    rs = _sector_ratios(cs, ref)
    if not rs:
        return None, 0
    if min(rs) <= FAST and max(rs) >= SLOW:
        return None, len(rs)
    done = [(t_ms(v), r) for v, r in zip(cs or [], ref["sec"]) if t_ms(v) and r]
    return sum(a for a, _ in done) / sum(b for _, b in done), len(done)


def lap_kind(lap: Optional[dict], ref: dict) -> Optional[str]:
    """A completed lap: OUT, IN, HOT, SLOW or None (not known)."""
    if not lap:
        return None
    if lap.get("how") in ("pit", "garage"):
        return "OUT"
    if lap.get("pit"):
        return "IN"
    t = t_ms(lap.get("time"))
    if t and ref.get("lap"):
        r = t / ref["lap"]
        return "HOT" if r <= FAST else "SLOW" if r >= LAP_SLOW else None
    rs = _sector_ratios(lap.get("cs"), ref)
    if rs and max(rs) >= SLOW:
        return "SLOW"          # one clearly slow sector: whatever it was, it was no hot lap
    if len(rs) >= 2 and max(rs) <= FAST:
        return "HOT"           # a hot lap needs two pushed sectors
    return None


def classify(st: Optional[dict], ref: Optional[dict], now_ms: float, *, in_pit: bool, on_lap: bool) -> tuple[str, str, str]:
    """(state, confidence HIGH|MEDIUM|LOW, reason) for the lap being driven now."""
    if in_pit:
        return "PIT", "HIGH", "in the pit lane / garage"
    if not isinstance(st, dict) or not on_lap:
        return "UNKNOWN", "LOW", "no lap being driven is known"
    if st.get("how") == "pit":
        return "OUT LAP", "HIGH", "lap began at the pit exit"
    if st.get("how") == "garage":
        return "OUT LAP", "MEDIUM", "first lap after standing in the pit (pit-exit message missing)"
    if ref is None:
        return "UNKNOWN", "LOW", "session pace not established yet"
    prev = lap_kind(st.get("pl"), ref)

    def slow(conf: str, why: str) -> tuple[str, str, str]:
        if prev == "HOT":
            return "COOLDOWN", conf, why + " after a hot lap"
        if prev in ("OUT", "SLOW", "IN"):
            return "PREP", conf, why + f" after {'an out' if prev == 'OUT' else 'a slow'} lap"
        return "UNKNOWN", "LOW", why + ", the lap before is not known"

    r, n = _sector_ratio(st.get("cs"), ref)
    if r is None and n:
        return "UNKNOWN", "LOW", "pushed and slow sectors in this lap (a push given up?)"
    if r is not None:
        conf = "HIGH" if n >= 2 else "MEDIUM"
        if r <= FAST:
            return "HOT LAP", conf, f"{n} sector(s) at {r:.3f}x own pace"
        if r >= SLOW:
            return slow(conf, f"{n} sector(s) at {r:.3f}x own pace")
        return "UNKNOWN", "LOW", f"sectors at {r:.3f}x - between push and preparation"
    sec, ss = st.get("sec"), st.get("sec_start")
    if sec is not None and ss is not None and ref["sec"][sec]:
        if now_ms - ss > ref["sec"][sec] * ELAPSED_SLOW:
            return slow("MEDIUM", f"S{sec + 1} already {(now_ms - ss) / ref['sec'][sec]:.2f}x its reference")
    lfl = st.get("lfl")
    if lfl and ref.get("fl") and prev in ("OUT", "SLOW"):
        q = lfl / ref["fl"]
        if q >= FL_FAST:
            return "HOT LAP", "MEDIUM", f"crossed the line at {lfl} km/h ({q:.2f}x best) after a {prev.lower()} lap"
        if q <= FL_SLOW:
            return "PREP", "MEDIUM", f"crossed the line at {lfl} km/h ({q:.2f}x best)"
    return "UNKNOWN", "LOW", "no evidence yet in this lap"
