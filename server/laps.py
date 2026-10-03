"""Per-car lap / sector progress on F1's clock (derived topic ``_Laps``).

Built only from live (non-snapshot) timing updates in time order, stored as a FeedState
topic - so checkpoints and seeks restore it with everything else and any replay moment is
reconstructed exactly (the same events give the same state; nothing from the future).

Signals (all official timing; the feed - and recordings of it - lose single messages, so
every fact has more than one signal):

* timing line crossed (a lap is complete, the next one starts): ``NumberOfLaps`` increases,
  a new ``LastLapTime`` value, a sector-3 ``Value``, or a new ``LapSeries`` lap entry;
* sector k completed: a non-empty ``Sectors[k].Value`` (``PreviousValue`` repeats it a moment
  later - used when the Value itself was lost, but then the sector start time is unknown);
* position inside the lap: the mini-sector ``Segments`` the car has passed (a segment of
  sector j with a non-zero status = the car is at least in sector j); all segments set back
  to 0 = a new lap has begun (arrives a few seconds after the line - a lost crossing message
  is still noticed, with an unknown start time);
* pit: ``InPit`` true (in the pit lane, no lap is being driven), ``PitOut`` true / ``InPit``
  false = the out lap starts at the pit exit.

Per car: ``n`` completed laps, ``start`` (F1 ms the current lap started, None = unknown),
``how`` (line | pit | reset), ``sec`` (0..2 = the sector being driven now, None = unknown / in
the pit), ``sec_start``, ``last_sec`` [k, value, ms], ``hist`` [[part, lap, time, ms, how the lap started, part it started
in, went through the pit], ...] (every completed lap time; only a lap that began at the timing
line in the same part and did not pass the pit lane can be a timed lap).
"""
from __future__ import annotations

from typing import Any, Optional

LAPS = "_Laps"
DEDUPE_MS = 8_000          # the twin topics (TimingData / TimingDataF1) repeat each update
RESET_AFTER_MS = 20_000    # a segment reset this long after the last crossing = a lost crossing
HIST_MAX = 80
PIT_ACTIVITY_MS = 45_000   # same rule as feedstate: timing activity this long after InPit = racing
IDLE_MS = 120_000          # no timing activity this long = the car stood (garage); same as the normalizer


def _int(v: Any) -> Optional[int]:
    try:
        return int(str(v).strip())
    except (TypeError, ValueError):
        return None


def _items(v: Any) -> list[tuple[int, Any]]:
    if isinstance(v, list):
        return list(enumerate(v))
    if isinstance(v, dict):
        out = []
        for k, x in v.items():
            i = _int(k)
            if i is not None:
                out.append((i, x))
        return sorted(out, key=lambda p: p[0])
    return []


def activity(upd: Any) -> bool:
    """A timing update that shows the car driving: a passed mini-sector, a sector / lap time, a
    lap count, a speed trap value. Updates that only clear values (every car gets them when a
    new qualifying part begins, also cars in the garage) are no activity."""
    if not isinstance(upd, dict):
        return False
    if upd.get("NumberOfLaps") is not None:
        return True
    ll = upd.get("LastLapTime")
    if isinstance(ll, dict) and str(ll.get("Value") or "").strip():
        return True
    for _, s in _items(upd.get("Sectors")):
        if not isinstance(s, dict):
            continue
        if str(s.get("Value") or "").strip() or str(s.get("PreviousValue") or "").strip():
            return True
        for _, seg in _items(s.get("Segments")):
            if isinstance(seg, dict) and _int(seg.get("Status")) not in (None, 0):
                return True
    sp = upd.get("Speeds")
    if isinstance(sp, dict) and any(isinstance(v, dict) and str(v.get("Value") or "").strip() for v in sp.values()):
        return True
    return False


def _new() -> dict:
    return {"n": None, "start": None, "how": None, "sec": None, "sec_start": None,
            "last_sec": None, "hist": [], "xing": None, "pit": False, "pit_t": None,
            "act": None, "seg": None, "lp": None, "lpit": False, "done": None,
            # "pin": the car went into the pit lane from a lap on track (an in lap), not out of
            # the garage / standing in the pit lane when the data began
            "pin": False,
            # the lap being driven: sector times so far, speed at the line when it began;
            # the two laps before it (for OUT / PREP / HOT / COOLDOWN, see normalizer.lap_state)
            "cs": [None, None, None], "lfl": None, "pl": None, "pl2": None}


def _archive(st: dict, t: float) -> None:
    """The lap being driven is complete (or interrupted): keep what is known about it."""
    st["pl2"] = st["pl"]
    st["pl"] = {"how": st["how"], "cs": list(st["cs"]), "lfl": st["lfl"], "pit": bool(st["lpit"]),
                "time": None, "t": t}
    st["cs"], st["lfl"] = [None, None, None], None


def _start_out_lap(st: dict, t: Optional[float], part: Optional[int], from_pit: bool = False) -> None:
    """An out lap begins: at the pit exit (t known) or found later (t None, how = "garage").
    ``opit``: the car is known to have come out of the pit lane (PitOut / InPit, also when the
    exit time is lost) - not only "moving again after a silence" (e.g. off the grid)."""
    st["opit"] = t is not None or from_pit
    st["start"], st["how"] = t, ("pit" if t is not None else "garage")
    st["sec"], st["sec_start"] = (0, t) if t is not None else (None, None)
    st["lp"], st["lpit"] = part, True             # an out lap is never a timed lap
    st["cs"], st["lfl"] = [None, None, None], None


def _crossing(st: dict, t: float, n: Optional[int] = None, part: Optional[int] = None) -> bool:
    """Timing line crossed at t. Returns False when it is the echo of the same crossing.
    What is known about the lap that ends here is kept in ``done`` - the lap time may arrive in
    another message (topic order is not guaranteed) and must be filed with the lap it belongs to."""
    if st["xing"] is not None and 0 <= t - st["xing"] < DEDUPE_MS and (n is None or n <= (st["n"] or 0)):
        return False
    st["done"] = {"how": st["how"], "part": st["lp"], "pit": st["lpit"], "t": t}
    _archive(st, t)
    st["xing"] = t
    st["n"] = max(n, st["n"] or 0) if n is not None else (st["n"] + 1 if st["n"] is not None else None)
    if st["pit"]:
        st["start"] = st["how"] = st["lp"] = None
        return True                        # crossed inside the pit lane: no lap on track starts here
    st["start"], st["how"], st["lp"], st["lpit"] = t, "line", part, False
    st["sec"], st["sec_start"] = 0, t
    return True


def track_laps(topics: dict, topic: str, data: Any, t_ms: float, before_all: dict) -> None:
    """``before_all``: per car what the merged state showed before this update (feedstate._line_summary)."""
    store = topics.setdefault(LAPS, {})
    if topic == "LapSeries":
        if not isinstance(data, dict):
            return
        for num, ent in data.items():
            if not isinstance(ent, dict) or _int(num) is None:
                continue
            laps = [i for i, _ in _items(ent.get("LapPosition"))]
            if not laps or isinstance(ent.get("LapPosition"), list):
                continue                    # a full list = snapshot content, not a new lap
            st = store.setdefault(str(num), _new())
            k = max(laps)
            if st["n"] is not None and k > st["n"]:
                _crossing(st, t_ms, k, _int((topics.get("TimingData") or {}).get("SessionPart")))
            elif st["n"] is None:
                st["n"] = k
        return

    incoming = data.get("Lines") if isinstance(data, dict) else None
    if not isinstance(incoming, dict):
        return
    td = topics.get("TimingData") or {}
    part = _int(td.get("SessionPart"))
    for num, upd in incoming.items():
        if not isinstance(upd, dict) or _int(num) is None:
            continue
        st = store.setdefault(str(num), _new())
        before = before_all.get(str(num)) or {}
        if st["n"] is None and before.get("n") is not None:
            st["n"] = before["n"]

        if upd.get("KnockedOut") is True and st.get("ko") is None and part:
            # knocked out when this part began = eliminated in the part before
            st["ko"] = max(1, part - 1)
        moving = activity(upd)
        if moving and not st["pit"] and (st["act"] is None or t_ms - st["act"] > IDLE_MS) and \
                upd.get("PitOut") is not True and _int(upd.get("NumberOfLaps")) is None:
            # timing activity again after a long silence: the car left the garage although the
            # pit-exit message is missing - an out lap whose start time is not known. It counts as
            # coming out of the pit only when F1's line still says InPit (stuck flag, real 2026
            # data) - a car moving off the grid after a long stand is not on an out lap
            line = ((td.get("Lines") or {}).get(str(num)) or {}) if isinstance(td, dict) else {}
            _start_out_lap(st, None, part, from_pit=bool(line.get("InPit")) or bool(upd.get("InPit")))
        prev_act = st["act"]
        if moving:
            st["act"] = t_ms                   # last timing activity (a car in the garage has none)
        # ---- pit lane
        if upd.get("InPit") is True and not st["pit"]:
            # a lap on track was being driven just now: pit entry (not a parked car re-flagged)
            # (activity before this message: a full line re-sent after a reconnect - also its echo on
            # the other timing topic at the same instant - is no lap)
            st["pin"] = st["how"] is not None and prev_act is not None and 0 < t_ms - prev_act <= IDLE_MS
            st["pit"], st["pit_t"], st["lpit"] = True, t_ms, True     # this lap went through the pit
            st["start"] = st["how"] = st["sec"] = st["sec_start"] = None
        elif st["pit"] and st["pit_t"] is not None and t_ms - st["pit_t"] > PIT_ACTIVITY_MS and moving:
            # InPit stuck although the car keeps setting times (the "left the pit" message was
            # lost - real 2026 data): it is on track - an out lap of unknown start
            st["pit"] = st["pin"] = False
            _start_out_lap(st, None, part, from_pit=True)
        pit_exit = upd.get("PitOut") is True or (upd.get("InPit") is False and st["pit"])
        # ---- the timing line
        crossed = False
        n = _int(upd.get("NumberOfLaps"))
        if n is not None:
            if st["n"] is not None and n > st["n"]:
                crossed = _crossing(st, t_ms, n, part) or crossed
            else:
                st["n"] = max(n, st["n"] or 0)
        ll = upd.get("LastLapTime")
        if isinstance(ll, dict) and ll.get("Value") and ll.get("Value") != before.get("ll"):
            v = str(ll["Value"])
            h = st["hist"]
            echo = h and h[-1][2] == v and 0 <= t_ms - h[-1][3] < DEDUPE_MS
            if not echo:
                if not crossed:
                    crossed = _crossing(st, t_ms, n, part)
                d = st["done"] if st["done"] and 0 <= t_ms - st["done"]["t"] < DEDUPE_MS else \
                    {"how": None, "part": None, "pit": True}
                # [part at the end, lap, time, F1 ms, how it started, part at the start, via the pit]
                h.append([part, st["n"], v, t_ms, d["how"], d["part"], bool(d["pit"])])
                del h[:-HIST_MAX]
                if st["pl"] and 0 <= t_ms - st["pl"]["t"] < DEDUPE_MS:
                    st["pl"]["time"] = v
        if pit_exit:
            st["pit"] = st["pin"] = False
            if not (st["how"] == "pit" and st["start"] is not None and 0 <= t_ms - st["start"] < DEDUPE_MS):
                _start_out_lap(st, t_ms, part)

        # ---- sectors and mini-sectors
        reset = False
        passed: list[int] = []
        for k, s in _items(upd.get("Sectors")):
            if not isinstance(s, dict) or k > 2:
                continue
            val = s.get("Value")
            sv = before.get("sv") or []
            if isinstance(val, str) and val.strip() and not (k < len(sv) and sv[k] == val):
                ls = st["last_sec"]
                echo = ls and ls[0] == k and ls[1] == val and (ls[2] is None or 0 <= t_ms - ls[2] < DEDUPE_MS * 2)
                if not echo:
                    st["last_sec"] = [k, val, t_ms]
                    if k == 2:
                        recent = crossed or (st["xing"] is not None and 0 <= t_ms - st["xing"] < DEDUPE_MS)
                        if recent and st["pl"] is not None:
                            st["pl"]["cs"][2] = val            # S3 of the lap that just ended
                        else:
                            st["cs"][2] = val
                            if not crossed:
                                crossed = _crossing(st, t_ms, n, part)
                    else:
                        if not st["pit"]:
                            st["cs"][k] = val
                        if not st["pit"] and (st["sec"] is None or st["sec"] <= k):
                            st["sec"], st["sec_start"] = k + 1, t_ms
            prev = s.get("PreviousValue")
            if isinstance(prev, str) and prev.strip() and k < 2 and not st["pit"] and st["sec"] == k:
                # the sector's Value message was lost: it is complete, its end time is not known
                st["sec"], st["sec_start"] = k + 1, None
                if st["cs"][k] is None:
                    st["cs"][k] = prev
                if not (st["last_sec"] and st["last_sec"][0] == k and st["last_sec"][1] == prev):
                    st["last_sec"] = [k, prev, None]
            elif isinstance(prev, str) and prev.strip() and k == 2 and st["pl"] is not None and \
                    st["pl"]["cs"][2] is None and 0 <= t_ms - st["pl"]["t"] < 10_000:
                st["pl"]["cs"][2] = prev                     # S3 of the lap that just ended
            for _, seg in _items(s.get("Segments")):
                if not isinstance(seg, dict) or "Status" not in seg:
                    continue
                if _int(seg.get("Status")) == 0:
                    reset = True
                else:
                    passed.append(k)
        near_line = st["seg"] is not None and st["seg"][0] == 2 and 0 <= t_ms - st["seg"][1] < 60_000
        out_lap = st["how"] == "pit" and st["start"] is not None and 0 <= t_ms - st["start"] < 90_000
        if reset and not st["pit"] and not crossed and not out_lap and near_line:
            # (after a pit exit the reset belongs to the out lap that just began; a reset of a car
            # that was not in the last sector - e.g. every car when the next Q part begins - is
            # no new lap)
            if st["xing"] is None or t_ms - st["xing"] > RESET_AFTER_MS:
                # a new lap started, but its line-crossing message is missing
                st["done"] = {"how": st["how"], "part": st["lp"], "pit": st["lpit"], "t": t_ms}
                _archive(st, t_ms)
                st["start"], st["how"], st["sec"], st["sec_start"] = None, "reset", 0, None
                st["lp"], st["lpit"] = part, False
                st["xing"] = t_ms
                if st["n"] is not None and n is None:
                    st["n"] += 1
        elif passed and not st["pit"] and not reset:
            j = max(passed)
            just_crossed = st["xing"] is not None and 0 <= t_ms - st["xing"] < DEDUPE_MS
            # (the last mini-sectors of the lap that just ended may arrive after the crossing)
            if not (just_crossed and j == 2) and (st["sec"] is None or j == st["sec"] + 1):
                st["sec"], st["sec_start"] = j, None
        if passed and not reset:
            st["seg"] = [max(passed), t_ms]
        elif reset:
            st["seg"] = [min(passed), t_ms] if passed else None
        # ---- speed at the finish line = how fast the car crossed into the lap it drives now
        sp = upd.get("Speeds")
        fl = sp.get("FL") if isinstance(sp, dict) else None
        v = _int(fl.get("Value")) if isinstance(fl, dict) else None
        if v and st["xing"] is not None and 0 <= t_ms - st["xing"] < DEDUPE_MS and not st["pit"]:
            st["lfl"] = v
