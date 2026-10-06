"""Race control / FIA message interpretation.

Only information explicitly present in the official race-control messages is
used. Nothing is inferred beyond the literal message content:

* sector yellow flags come from "YELLOW IN TRACK SECTOR n" style messages
  (structured fields Category=Flag, Scope=Sector, Sector=n)
* investigations / penalties / disqualifications are parsed from the steward
  message text and attached to the car numbers named in the message
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Optional

from .models import RaceControlFlags, RaceControlMessage
from .telemetry import parse_utc

# "81 (PIA)" - but not the clock time in "... LAP 11 7:55:04 (PIT)" (real 2024 message)
CAR_RE = re.compile(r"(?<!:)\b(\d{1,2}) \(([A-Z]{3})\)")
CAR_ONLY_RE = re.compile(r"\bCARS? (\d{1,2})\b")
TIME_PEN_RE = re.compile(r"(\d+) SECOND TIME PENALTY")
SG_PEN_RE = re.compile(r"(\d+) SECOND STOP[ /-]*(?:AND[ -])?GO PENALTY")
GRID_PEN_RE = re.compile(r"(\d+) PLACE GRID PENALTY")
LAP_TIME_RE = re.compile(r"\bTIME (\d{1,2}:\d{2}\.\d{3})\b")      # "CAR 41 (LIN) TIME 1:31.537 DELETED ..."
LAP_NO_RE = re.compile(r"\bLAP (\d{1,3})\b")                         # "... LAP DELETED ... LAP 18" / "LAP 7 DELETED"

_INV_KEYWORDS = [
    ("REVIEWED NO FURTHER INVESTIGATION", "CLOSED"),
    ("NO FURTHER INVESTIGATION", "CLOSED"),
    ("NO FURTHER ACTION", "CLOSED"),
    ("NO INVESTIGATION NECESSARY", "CLOSED"),
    ("WILL BE INVESTIGATED AFTER THE RACE", "AFTER RACE"),
    ("WILL BE INVESTIGATED AFTER THE SESSION", "AFTER RACE"),
    ("UNDER INVESTIGATION", "UNDER INVESTIGATION"),
    ("NOTED", "NOTED"),
]
_INV_RANK = {"UNDER INVESTIGATION": 3, "AFTER RACE": 2, "NOTED": 1}


@dataclass
class _Incident:
    cars: set[str]
    status: str
    reason: Optional[str]


@dataclass
class RaceControlResult:
    messages: list[RaceControlMessage] = field(default_factory=list)       # oldest first
    sector_flags: dict[str, str] = field(default_factory=dict)
    driver_flags: dict[str, RaceControlFlags] = field(default_factory=dict)
    sc_phase: Optional[str] = None
    overtake: Optional[str] = None
    drs: Optional[str] = None
    chequered: bool = False
    red_flag_msg: bool = False
    # session flow from the messages alone (fallback when TrackStatus / SessionStatus are missing)
    track_flag: Optional[str] = None     # GREEN / YELLOW / RED / CHEQUERED (last track-wide flag)
    safety_car: Optional[str] = None     # SC / SC_ENDING / VSC / VSC_ENDING while deployed
    session_rc: Optional[str] = None     # Started / Aborted / Finished (category SessionStatus)
    pit_exit: Optional[str] = None       # OPEN / CLOSED (literal "PIT EXIT OPEN / CLOSED" messages)
    pit_entry: Optional[str] = None      # OPEN / CLOSED
    red_flag_seen: bool = False          # a red flag earlier in this session
    track_flag_utc: Optional[str] = None  # Utc of the message that set track_flag / safety_car
    driver_messages: dict[str, list] = field(default_factory=dict)   # num -> latest messages naming it


def _ordered_messages(raw: Any) -> list[dict]:
    if isinstance(raw, dict):
        raw = raw.get("Messages", raw)
    if isinstance(raw, list):
        return [m for m in raw if isinstance(m, dict)]
    if isinstance(raw, dict):
        items = []
        for k, v in raw.items():
            try:
                items.append((int(k), v))
            except ValueError:
                continue
        return [v for _, v in sorted(items) if isinstance(v, dict)]
    return []


def _cars_in(text: str, racing_number: Any) -> list[str]:
    cars = [m.group(1) for m in CAR_RE.finditer(text)]
    if not cars:
        cars = [m.group(1) for m in CAR_ONLY_RE.finditer(text)]
    if racing_number not in (None, ""):
        rn = str(racing_number)
        if rn not in cars:
            cars.insert(0, rn)
    return list(dict.fromkeys(cars))


def _penalty_label(text: str) -> Optional[str]:
    m = TIME_PEN_RE.search(text)
    if m:
        return f"+{m.group(1)}s"
    m = SG_PEN_RE.search(text)
    if m:
        return f"SG{m.group(1)}"
    if "STOP AND GO" in text or "STOP/GO" in text or "STOP-GO" in text:
        return "SG"
    if "DRIVE THROUGH" in text:
        return "DT"
    m = GRID_PEN_RE.search(text)
    if m:
        return f"GRID{m.group(1)}"
    if "PIT LANE START" in text and "PENALTY" in text:
        return "PLS"
    return None


def _reason(text: str, keyword_end: int) -> Optional[str]:
    rest = text[keyword_end:]
    if " - " in rest:
        return rest.split(" - ", 1)[1].strip() or None
    return None


def _severity(m: dict, text: str) -> str:
    cat = (m.get("Category") or "").upper()
    flag = (m.get("Flag") or "").upper()
    if cat == "FLAG":
        if flag in ("YELLOW", "DOUBLE YELLOW"):
            return "yellow"
        if flag == "RED":
            return "red"
        if flag in ("GREEN", "CLEAR"):
            return "green"
        if flag == "BLUE":
            return "blue"
        if flag == "CHEQUERED":
            return "chequered"
        if flag in ("BLACK AND WHITE", "BLACK AND ORANGE"):
            return "investigation"
        if flag == "BLACK":
            return "penalty"
    if cat == "SAFETYCAR":
        return "sc"
    if "DISQUALIFIED" in text or ("PENALTY" in text and "NO PENALTY" not in text
                                  and "INVESTIGATION" not in text):
        return "penalty"
    if "INVESTIGATION" in text or "NOTED" in text or "INVESTIGATED" in text:
        return "investigation"
    if "DELETED" in text:
        return "deleted"
    return "info"


_TAGS = [
    ("UNDER INVESTIGATION", "investigation"), ("INVESTIGATED", "investigation"), ("NOTED", "investigation"),
    ("NO FURTHER INVESTIGATION", "investigation"), ("NO FURTHER ACTION", "investigation"),
    ("PENALTY", "penalty"), ("DISQUALIFIED", "penalty"), ("REPRIMAND", "penalty"),
    ("DELETED", "deleted_lap"), ("REINSTATED", "deleted_lap"),
    ("TRACK LIMITS", "track_limits"), ("UNSAFE RELEASE", "unsafe_release"),
    ("PIT EXIT", "pit_lane"), ("PIT ENTRY", "pit_lane"), ("PIT LANE", "pit_lane"),
    ("BLUE FLAG", "blue_flag"),
]
_HIGH = {"red_flag", "safety_car", "penalty", "session_status", "chequered"}
_MEDIUM = {"investigation", "deleted_lap", "yellow_flag", "unsafe_release", "pit_lane"}


def _tags(m: dict, up: str) -> list[str]:
    """What a message literally is about - from F1's own fields and the words in it."""
    cat = (m.get("Category") or "").upper()
    flag = (m.get("Flag") or "").upper()
    tags: list[str] = []
    if cat == "FLAG":
        tags.append({"RED": "red_flag", "YELLOW": "yellow_flag", "DOUBLE YELLOW": "yellow_flag",
                     "CHEQUERED": "chequered", "BLUE": "blue_flag"}.get(flag, "flag"))
    elif cat == "SAFETYCAR":
        tags.append("safety_car")
    elif cat == "SESSIONSTATUS":
        tags.append("session_status")
    elif cat == "DRS":
        tags.append("drs")
    for word, tag in _TAGS:
        if word in up and tag not in tags and not (tag == "penalty" and "NO PENALTY" in up):
            tags.append(tag)
    return tags


def _importance(tags: list[str]) -> str:
    if _HIGH & set(tags):
        return "high"
    if _MEDIUM & set(tags):
        return "medium"
    return "low"


def visible_messages(raw: Any, until_ms: Optional[float]) -> list[dict]:
    """Messages issued up to the shown moment. A message whose own Utc lies after it (e.g. held
    from a later state) is never shown - neither the message nor the penalty / flag it causes."""
    msgs = _ordered_messages(raw)
    if until_ms is None:
        return msgs
    out = []
    for m in msgs:
        t = parse_utc(m.get("Utc")) if m.get("Utc") else None
        if t is None or t.timestamp() * 1000 <= until_ms + 1000:     # Utc has 1 s resolution
            out.append(m)
    return out


def process_messages(raw: Any, until_ms: Optional[float] = None) -> RaceControlResult:
    res = RaceControlResult()
    incidents: dict[str, _Incident] = {}
    msgs = visible_messages(raw, until_ms)

    def dflags(num: str) -> RaceControlFlags:
        if num not in res.driver_flags:
            res.driver_flags[num] = RaceControlFlags()
        return res.driver_flags[num]

    for idx, m in enumerate(msgs):
        text = str(m.get("Message") or "").strip()
        up = text.upper()
        cat = (m.get("Category") or "")
        flag = (m.get("Flag") or "").upper() or None
        scope = m.get("Scope")
        sector = m.get("Sector")
        rn = m.get("RacingNumber")
        try:
            sector_i = int(sector) if sector not in (None, "") else None
        except (TypeError, ValueError):
            sector_i = None
        try:
            lap = int(m.get("Lap")) if m.get("Lap") not in (None, "") else None
        except (TypeError, ValueError):
            lap = None

        tags = _tags(m, up)
        res.messages.append(RaceControlMessage(
            id=str(idx), utc=m.get("Utc"), lap=lap, category=cat or None, flag=flag,
            scope=scope, sector=sector_i, driver=str(rn) if rn not in (None, "") else None,
            text=text, severity=_severity(m, up), tags=tags, importance=_importance(tags),
            status=m.get("Status") or None, mode=m.get("Mode") or None))

        # ---- pit lane (only the literal statements) -----------------------
        for where, attr in (("PIT EXIT", "pit_exit"), ("PIT ENTRY", "pit_entry"), ("PIT LANE", None)):
            for state in ("OPEN", "CLOSED"):
                if f"{where} {state}" in up:
                    if attr:
                        setattr(res, attr, state)
                    else:
                        res.pit_exit = res.pit_entry = state

        # ---- flags --------------------------------------------------------
        if cat.upper() == "FLAG":
            if scope == "Sector" and sector_i is not None:
                if flag in ("YELLOW", "DOUBLE YELLOW"):
                    res.sector_flags[str(sector_i)] = flag
                elif flag in ("CLEAR", "GREEN"):
                    res.sector_flags.pop(str(sector_i), None)
            elif scope == "Track":
                if flag in ("CLEAR", "GREEN"):
                    res.sector_flags.clear()
                    res.red_flag_msg = False
                    res.track_flag, res.safety_car = "GREEN", None
                elif flag == "RED":
                    res.red_flag_msg = True
                    res.red_flag_seen = True
                    res.track_flag, res.safety_car = "RED", None
                elif flag == "CHEQUERED":
                    res.chequered = True
                    res.track_flag = "CHEQUERED"
                elif flag in ("YELLOW", "DOUBLE YELLOW"):
                    res.track_flag = "YELLOW"
                res.track_flag_utc = m.get("Utc")
            elif scope == "Driver" and rn not in (None, ""):
                if flag == "BLACK AND WHITE":
                    dflags(str(rn)).black_white = True
                elif flag == "BLACK":
                    dflags(str(rn)).disqualified = True

        if cat.upper() == "SAFETYCAR":
            mode = (m.get("Mode") or "").upper()
            status = (m.get("Status") or "").upper()
            if not mode:          # (sources without the Mode / Status fields: read the text)
                mode = "VIRTUAL SAFETY CAR" if "VIRTUAL" in up else "SAFETY CAR" if "SAFETY CAR" in up else ""
            if not status:
                status = "IN THIS LAP" if "IN THIS LAP" in up else "ENDING" if "ENDING" in up else \
                    "DEPLOYED" if "DEPLOYED" in up else ""
            res.sc_phase = f"{mode} {status}".strip() or None
            base = "VSC" if mode.startswith("VIRTUAL") else "SC" if mode else None
            if base:
                res.safety_car = base + ("_ENDING" if status in ("ENDING", "IN THIS LAP") else "")
                res.track_flag_utc = m.get("Utc")

        if cat.upper() == "SESSIONSTATUS":
            for key, st in (("STARTED", "Started"), ("RESUMED", "Started"), ("ABORTED", "Aborted"),
                            ("SUSPENDED", "Aborted"), ("FINISHED", "Finished")):
                if key in up:
                    res.session_rc = st
                    break

        if cat.upper() == "DRS" or up.startswith("DRS "):
            if "ENABLED" in up:
                res.drs = "ENABLED"
            elif "DISABLED" in up:
                res.drs = "DISABLED"
        if up.startswith("OVERTAKE ENABLED"):
            res.overtake = "ENABLED"
        elif up.startswith("OVERTAKE DISABLED"):
            res.overtake = "DISABLED"

        # ---- per-driver steward information -------------------------------
        cars = _cars_in(up, rn)
        if not cars:
            continue
        for c in cars:                   # the messages that name this car (and only this car)
            ms = res.driver_messages.setdefault(c, [])
            ms.append({"utc": m.get("Utc"), "text": text})
            del ms[:-5]

        if "DISQUALIFIED" in up:
            for c in cars:
                dflags(c).disqualified = True
            continue

        if "DELETED" in up and ("LAP DELETED" in up or "TIME " in up):
            for c in cars[:1]:
                dflags(c).deleted_laps += 1
                tm = LAP_TIME_RE.search(up)
                if tm and tm.group(1) not in dflags(c).deleted_times:
                    dflags(c).deleted_times.append(tm.group(1))
                ln = LAP_NO_RE.search(up)
                if ln and int(ln.group(1)) not in dflags(c).deleted_lap_numbers:
                    dflags(c).deleted_lap_numbers.append(int(ln.group(1)))
            continue
        if "REINSTATED" in up:
            for c in cars[:1]:
                f = dflags(c)
                f.deleted_laps = max(0, f.deleted_laps - 1)
                tm = LAP_TIME_RE.search(up)
                if tm and tm.group(1) in f.deleted_times:
                    f.deleted_times.remove(tm.group(1))
                ln = LAP_NO_RE.search(up)
                if ln and int(ln.group(1)) in f.deleted_lap_numbers:
                    f.deleted_lap_numbers.remove(int(ln.group(1)))
            continue

        if "PENALTY SERVED" in up:
            label = _penalty_label(up)
            for c in cars:
                for p in dflags(c).penalties:
                    if not p["served"] and (label is None or p["label"] == label):
                        p["served"] = True
                        break
            continue

        if ("WITHDRAWN" in up or "RESCINDED" in up) and "PENALTY" in up:
            label = _penalty_label(up)
            for c in cars:
                pens = dflags(c).penalties
                for p in pens:
                    if label is None or p["label"] == label:
                        pens.remove(p)
                        break
            continue

        inv_status = None
        inv_end = -1
        for kw, st in _INV_KEYWORDS:
            pos = up.find(kw)
            if pos >= 0:
                inv_status, inv_end = st, pos
                break
        label = _penalty_label(up)

        if label and inv_status is None:
            reason = up.split(" - ", 1)[1].strip() if " - " in up else None
            for c in cars:
                dflags(c).penalties.append({"label": label, "served": False, "text": text})
                # A decided penalty closes the matching open investigation(s)
                if reason:
                    for inc in incidents.values():
                        if c in inc.cars and inc.status in _INV_RANK and inc.reason == reason:
                            inc.status = "CLOSED"
            continue

        if inv_status is not None:
            key = up[:inv_end].replace("FIA STEWARDS:", "").strip(" -:")
            reason = _reason(up, inv_end)
            inc = incidents.get(key)
            if inc is None:
                incidents[key] = _Incident(set(cars), inv_status, reason)
            else:
                inc.cars.update(cars)
                inc.status = inv_status
                inc.reason = inc.reason or reason

    for inc in incidents.values():
        if inc.status not in _INV_RANK:
            continue
        for c in inc.cars:
            f = dflags(c)
            if f.investigation is None or _INV_RANK[inc.status] > _INV_RANK.get(f.investigation, 0):
                f.investigation = inc.status
    return res
