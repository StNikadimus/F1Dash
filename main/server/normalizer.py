"""Raw feed state -> normalized dashboard model.

This is the only module that knows the shape of the raw F1 topics. The output
consists solely of the dataclasses from ``models.py`` (serialised to dicts).
"""
from __future__ import annotations

import json
import logging
from dataclasses import replace
from datetime import datetime, timezone
from typing import Any, Optional

from . import chase as _chase
from .chase import CHASE
from .feedstate import IN_PIT_SINCE, STOPPED_SINCE, TIMES, FeedState
from .lap_state import classify as lap_state_classify, driver_ref, pace_refs
from .laps import LAPS
from . import team_radio
from .models import (Availability, ClockState, DriverState, RaceControlFlags, SessionState,
                     StintState, TimeValue, TrackStatusState, WeatherState, to_dict)
from .race_control import RaceControlResult, process_messages, visible_messages
from .session_phases import SessionTimeline, phase_label, phase_prefix
from .telemetry import parse_utc

log = logging.getLogger("normalizer")

# longer than a pit stop / drive-through (~20-35 s, a long repair ~60 s): the car is in the garage
GARAGE_SECONDS = 75.0
# a car that reports Stopped and sets no sector / lap data for this long is out (crash / failure):
# the Retired flag does not always arrive (real 2026 data)
DNF_STOPPED_SECONDS = 60.0

TRACK_STATUS = {
    "1": "GREEN", "2": "YELLOW", "3": "YELLOW", "4": "SC", "5": "RED", "6": "VSC", "7": "VSC_ENDING",
}


def _s(v: Any) -> Optional[str]:
    if v is None:
        return None
    s = str(v).strip()
    return s or None


def _i(v: Any) -> Optional[int]:
    try:
        return int(str(v).strip())
    except (TypeError, ValueError):
        return None


def _f(v: Any) -> Optional[float]:
    try:
        return float(str(v).strip())
    except (TypeError, ValueError):
        return None


def _b(v: Any) -> Optional[bool]:
    if isinstance(v, bool):
        return v
    if v is None:
        return None
    s = str(v).strip().lower()
    if s in ("true", "1"):
        return True
    if s in ("false", "0"):
        return False
    return None


def _items(v: Any) -> list:
    """F1 lists sometimes arrive as dicts keyed "0", "1", ... - return an ordered list."""
    if isinstance(v, list):
        return v
    if isinstance(v, dict):
        pairs = []
        for k, x in v.items():
            idx = _i(k)
            if idx is not None:
                pairs.append((idx, x))
        return [x for _, x in sorted(pairs)]
    return []


def _value(v: Any) -> Optional[str]:
    if isinstance(v, dict):
        return _s(v.get("Value"))
    return _s(v)


def _hms_to_ms(s: Any) -> Optional[int]:
    if not isinstance(s, str) or not s:
        return None
    try:
        parts = [float(p) for p in s.split(":")]
    except ValueError:
        return None
    while len(parts) < 3:
        parts.insert(0, 0.0)
    h, m, sec = parts[-3:]
    return int((h * 3600 + m * 60 + sec) * 1000)


def _session_kind(type_: Optional[str], name: Optional[str]) -> str:
    from .session_phases import session_kind
    return session_kind(type_, name)


def lap_ms(v: Any) -> Optional[int]:
    """'1:31.537' / '31.537' -> ms (None if not a time)."""
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


def fmt_gap(ms: int) -> str:
    return f"+{ms / 1000:.3f}"


PHASE_STATE = {"Started": "RUNNING", "Aborted": "SUSPENDED", "Finished": "ENDED", "Finalised": "ENDED",
               "Ends": "ENDED", "Inactive": "NOT STARTED"}
RUNNING_MAX_MS = 10 * 60_000     # a lap / sector "running" longer than this is not shown as a running time
from .laps import IDLE_MS       # noqa: E402  (no timing activity for this long: not driving a lap now)
IN_LAP_MS = 90_000      # pit lane entered from a lap on track this recently: IN LAP (then IN PIT)


class Normalizer:
    def __init__(self, race_control_max: int = 60) -> None:
        self.race_control_max = race_control_max
        self._now_ms = 0.0
        self._rc_cache_key: Any = None
        self._rc: RaceControlResult = RaceControlResult()
        # session structure on F1's clock (phases, clock, markers) - set by the engine when known
        self.timeline: Optional[SessionTimeline] = None

    def race_control(self, feed: FeedState, until_ms: Optional[float] = None) -> RaceControlResult:
        raw = feed.get("RaceControlMessages")
        # only messages issued up to the shown moment (after a rewind nothing "from the future" stays)
        n_visible = len(visible_messages(raw or {}, until_ms))
        key = (id(raw), json.dumps(raw, sort_keys=True, default=str)[-200:] if raw else "", n_visible)
        if key != self._rc_cache_key:
            try:
                self._rc = process_messages(raw or {}, until_ms)
            except Exception:  # noqa: BLE001
                log.exception("Race control parse error")
                self._rc = RaceControlResult()
            self._rc_cache_key = key
        return self._rc

    # ------------------------------------------------------------------
    def session(self, feed: FeedState, now: datetime, speed: float) -> SessionState:
        si = feed.get("SessionInfo") or {}
        meeting = si.get("Meeting") or {}
        circuit = meeting.get("Circuit") or {}
        status = _s((feed.get("SessionStatus") or {}).get("Status")) or self._series_status(feed) or \
            _s(si.get("SessionStatus"))
        name = _s(si.get("Name"))
        type_ = _s(si.get("Type"))
        path = _s(si.get("Path"))
        start = _s(si.get("StartDate"))
        year = _i(path[:4]) if path else None
        if year is None and start:
            year = _i(start[:4])

        lc = feed.get("LapCount") or {}
        td = self._timing(feed)
        clock = ClockState(speed=speed)
        ec = feed.get("ExtrapolatedClock") or {}
        rem = _hms_to_ms(ec.get("Remaining"))
        if rem is not None:
            running = bool(ec.get("Extrapolating"))
            if running:
                utc = parse_utc(ec.get("Utc"))
                # the official session end freezes the clock where it was then (a race clock is
                # not stopped by the feed at the finish - real 2026 Japanese GP data)
                ended = self._ended_at(feed, status)
                ref = min(now, ended) if ended is not None else now
                if utc is not None:
                    rem -= int((ref - utc).total_seconds() * 1000)
                if ended is not None and ended <= now:
                    running = False
            clock = ClockState(remaining_ms=max(0, rem), running=running and rem > 0, speed=speed)

        part = _i(td.get("SessionPart")) if isinstance(td, dict) else None
        cutoff = None
        entries = _items(td.get("NoEntries")) if isinstance(td, dict) else []
        if part is not None and 0 <= part < len(entries):
            cutoff = _i(entries[part])          # cars that advance to the next part
        kind = _session_kind(type_, name)
        phase = phase_label(kind, name, part)
        pstate = PHASE_STATE.get(status or "")
        duration = None
        tl = self.timeline
        if phase and tl is not None and tl.phase(phase) is not None:
            duration = tl.phase(phase).duration
        if kind == "qualifying":
            title = ("SPRINT QUALIFYING" if phase_prefix(kind, name) == "SQ" else "QUALIFYING") + \
                (f" — {phase}" if phase else "")
        elif kind == "practice":
            title = f"{phase} — BEST LAP"
        else:
            title = (name or "").upper() or None
        return SessionState(
            phase=phase, phase_state=pstate, phase_duration_ms=duration, title=title,
            now_ms=round(now.timestamp() * 1000),
            quali_cutoff=cutoff,
            meeting_name=_s(meeting.get("Name")),
            official_name=_s(meeting.get("OfficialName")),
            location=_s(meeting.get("Location")),
            country=_s((meeting.get("Country") or {}).get("Name")),
            circuit_key=_i(circuit.get("Key")),
            circuit_name=_s(circuit.get("ShortName")),
            session_key=_i(si.get("Key")),
            session_type=type_,
            session_name=name,
            session_kind=kind,
            session_part=part,
            status=status,
            start_utc=start,
            gmt_offset=_s(si.get("GmtOffset")),
            year=year,
            path=path,
            lap=_i(lc.get("CurrentLap")),
            total_laps=_i(lc.get("TotalLaps")),
            clock=clock,
            live=status in ("Started", "Aborted"),
        )

    @staticmethod
    def _ended_at(feed: FeedState, status: Optional[str]) -> Optional[datetime]:
        """F1 time the session (part) ended, when it has: the first Finished / Finalised / Ends
        after the last Started in SessionData.StatusSeries."""
        if status not in ("Finished", "Finalised", "Ends"):
            return None
        items = [e for e in _items((feed.get("SessionData") or {}).get("StatusSeries"))
                 if isinstance(e, dict) and e.get("SessionStatus")]
        last_start = max((i for i, e in enumerate(items) if e.get("SessionStatus") == "Started"), default=-1)
        for e in items[last_start + 1:]:
            if e.get("SessionStatus") in ("Finished", "Finalised", "Ends"):
                return parse_utc(e.get("Utc"))
        return None

    @staticmethod
    def _series_status(feed: FeedState) -> Optional[str]:
        """Latest SessionStatus of SessionData.StatusSeries (when the SessionStatus topic is missing)."""
        ss = (feed.get("SessionData") or {}).get("StatusSeries")
        items = _items(ss)
        for e in reversed(items):
            if isinstance(e, dict) and e.get("SessionStatus"):
                return _s(e.get("SessionStatus"))
        return None

    def track_status(self, feed: FeedState, rc: RaceControlResult, session: SessionState) -> TrackStatusState:
        ts = feed.get("TrackStatus") or {}
        code = _s(ts.get("Status"))
        status = TRACK_STATUS.get(code or "", "UNKNOWN")
        # (a code this version does not know is still F1's official status: passed on, not replaced)
        source = "TrackStatus" if code else None
        if source is None:
            # no official track status: what the race control messages up to now say - and still
            # UNKNOWN when they say nothing (never an invented GREEN)
            derived = None
            if rc.track_flag == "RED" or rc.session_rc == "Aborted":
                derived = "RED"                  # (TRACK CLEAR during a stoppage is not the restart)
            elif rc.safety_car:
                derived = {"SC": "SC", "SC_ENDING": "SC", "VSC": "VSC", "VSC_ENDING": "VSC_ENDING"}[rc.safety_car]
            elif rc.track_flag == "CHEQUERED":
                derived = "CHEQUERED"
            elif rc.track_flag in ("GREEN", "YELLOW"):
                derived = "YELLOW" if rc.sector_flags or rc.track_flag == "YELLOW" else "GREEN"
            if derived:
                status, source = derived, "RaceControl"
        if rc.chequered and session.status in ("Finished", "Finalised", "Ends"):
            status = "CHEQUERED"
        sc_phase = rc.sc_phase if status in ("SC", "VSC", "VSC_ENDING") else None
        sector_flags = dict(rc.sector_flags) if status not in ("RED",) else {}
        state = {"GREEN": "GREEN", "YELLOW": "YELLOW", "SC": "SAFETY_CAR", "VSC": "VSC", "VSC_ENDING": "VSC_ENDING",
                 "RED": "RED_FLAG", "CHEQUERED": "CHEQUERED"}.get(status, "UNKNOWN")
        if state == "YELLOW" and "DOUBLE YELLOW" in sector_flags.values():
            state = "DOUBLE_YELLOW"
        if state == "UNKNOWN" and code:
            state = f"TRACK_STATUS_{code}"      # an official code this version does not know: passed on
        ts_ms = None
        if source == "TrackStatus":
            ts_ms = (feed.get(TIMES) or {}).get("TrackStatus")
        elif source == "RaceControl" and rc.track_flag_utc:
            u = parse_utc(rc.track_flag_utc)
            ts_ms = u.timestamp() * 1000 if u else None
        running = (session.status or rc.session_rc) == "Started"
        return TrackStatusState(code=code, status=status, message=_s(ts.get("Message")), sc_phase=sc_phase,
                                sector_flags=sector_flags,
                                overtake=rc.overtake, drs=rc.drs, chequered=rc.chequered, source=source,
                                state=state, timestamp=round(ts_ms) if ts_ms is not None else None,
                                pit_exit=rc.pit_exit, pit_entry=rc.pit_entry,
                                red_flag_restart=bool(rc.red_flag_seen and running and status != "RED"))

    def session_flow(self, feed: FeedState, rc: RaceControlResult, session: SessionState,
                     track: TrackStatusState) -> None:
        """RUNNING / SUSPENDED (red flag) / FINISHED / NOT STARTED at the shown moment, from the
        official session status, else the race control messages; UNKNOWN when neither says it."""
        status = session.status or rc.session_rc
        state = PHASE_STATE.get(status or "", "UNKNOWN")
        red = status == "Aborted" or track.code == "5" or (track.status == "RED" and state != "ENDED")
        if red and state in ("RUNNING", "UNKNOWN"):
            state = "SUSPENDED"
        session.state = {"ENDED": "FINISHED"}.get(state, state)
        session.red_flag = red
        session.phase_state = state if session.phase else session.phase_state
        have = [feed.get("RaceControlMessages") is not None, feed.get("TrackStatus") is not None,
                feed.get("SessionStatus") is not None or bool(self._series_status(feed))]
        session.rc_coverage = "COMPLETE" if all(have) else "PARTIAL" if any(have) else "NONE"

    def weather(self, feed: FeedState) -> WeatherState:
        w = feed.get("WeatherData") or {}
        rain = _i(w.get("Rainfall"))
        return WeatherState(
            air_temp=_f(w.get("AirTemp")), track_temp=_f(w.get("TrackTemp")),
            humidity=_f(w.get("Humidity")), pressure=_f(w.get("Pressure")),
            wind_speed=_f(w.get("WindSpeed")), wind_direction=_i(w.get("WindDirection")),
            rainfall=None if rain is None else rain > 0,
        )

    @staticmethod
    def _timing(feed: FeedState) -> dict:
        td = feed.get("TimingData")
        if isinstance(td, dict) and td.get("Lines"):
            return td
        td1 = feed.get("TimingDataF1")
        if isinstance(td1, dict) and td1.get("Lines"):
            return td1
        return td if isinstance(td, dict) else {}

    def drivers(self, feed: FeedState, rc: RaceControlResult, session: SessionState) -> tuple[dict[str, DriverState], list[str]]:
        dl = feed.get("DriverList") or {}
        td = self._timing(feed)
        lines = td.get("Lines") or {}
        app = (feed.get("TimingAppData") or {}).get("Lines") or {}
        stats = (feed.get("TimingStats") or {}).get("Lines") or {}
        cur_tyres = (feed.get("CurrentTyres") or {}).get("Tyres") or {}
        tss = (feed.get("TyreStintSeries") or {}).get("Stints") or {}
        part = session.session_part
        kind = session.session_kind

        nums = [n for n in dl.keys() if isinstance(dl.get(n), dict) and _i(n) is not None]
        for n in lines.keys():
            if n not in nums and _i(n) is not None:
                nums.append(n)

        out: dict[str, DriverState] = {}
        for num in nums:
            info = dl.get(num) if isinstance(dl.get(num), dict) else {}
            line = lines.get(num) if isinstance(lines.get(num), dict) else {}
            d = DriverState(number=str(num))
            d.tla = _s(info.get("Tla"))
            d.full_name = _s(info.get("FullName")) or _s(info.get("BroadcastName"))
            d.first_name = _s(info.get("FirstName"))
            d.last_name = _s(info.get("LastName"))
            d.team = _s(info.get("TeamName"))
            colour = _s(info.get("TeamColour"))
            d.team_color = f"#{colour}" if colour and not colour.startswith("#") else colour

            d.position = _i(line.get("Position"))
            sp = line.get("ShowPosition")
            d.show_position = True if sp is None else bool(sp)

            # ---- gaps ---------------------------------------------------
            if kind == "qualifying":
                st = _items(line.get("Stats"))
                idx = (part or 1) - 1
                if 0 <= idx < len(st) and isinstance(st[idx], dict):
                    d.gap = _s(st[idx].get("TimeDiffToFastest"))
                    d.interval = _s(st[idx].get("TimeDifftoPositionAhead") or st[idx].get("TimeDiffToPositionAhead"))
                blt = _items(line.get("BestLapTimes"))
                best_part = None
                if 0 <= idx < len(blt) and isinstance(blt[idx], dict):
                    best_part = _s(blt[idx].get("Value"))
                d.best_lap = TimeValue(value=best_part or _value(line.get("BestLapTime")))
            else:
                gap = line.get("GapToLeader")
                if gap is None:
                    gap = line.get("TimeDiffToFastest")
                d.gap = _value(gap)
                itv = line.get("IntervalToPositionAhead")
                if itv is None:
                    itv = line.get("TimeDiffToPositionAhead")
                d.interval = _value(itv)
                if isinstance(itv, dict):
                    d.catching = _b(itv.get("Catching"))
                d.best_lap = TimeValue(value=_value(line.get("BestLapTime")))

            pb = (stats.get(num) or {}).get("PersonalBestLapTime") if isinstance(stats.get(num), dict) else None
            if isinstance(pb, dict) and _i(pb.get("Position")) == 1 and d.best_lap.value:
                d.best_lap.overall_best = True

            ll = line.get("LastLapTime") or {}
            if isinstance(ll, dict):
                d.last_lap = TimeValue(value=_s(ll.get("Value")), personal_best=bool(ll.get("PersonalFastest")),
                                       overall_best=bool(ll.get("OverallFastest")))
            secs = []
            for s in _items(line.get("Sectors"))[:3]:
                if not isinstance(s, dict):
                    secs.append(TimeValue())
                    continue
                secs.append(TimeValue(value=_s(s.get("Value")), personal_best=bool(s.get("PersonalFastest")),
                                      overall_best=bool(s.get("OverallFastest"))))
            d.sectors = secs
            speeds = line.get("Speeds") or {}
            if isinstance(speeds, dict):
                d.speed_trap = _value(speeds.get("ST"))
            d.laps = _i(line.get("NumberOfLaps"))
            d.pit_stops = _i(line.get("NumberOfPitStops"))
            d.in_pit = bool(line.get("InPit"))
            d.pit_out = bool(line.get("PitOut"))
            d.retired = bool(line.get("Retired"))
            d.stopped = bool(line.get("Stopped"))
            d.knocked_out = bool(line.get("KnockedOut"))
            st_since = (feed.get(STOPPED_SINCE) or {}).get(str(num))
            d.dnf = d.retired or (d.stopped and st_since is not None
                                  and self._now_ms - st_since >= DNF_STOPPED_SECONDS * 1000)
            if d.in_pit:
                ent = (feed.get(IN_PIT_SINCE) or {}).get(str(num))
                since, stale = (ent if isinstance(ent, list) and len(ent) == 2 else (None, False))
                if stale:
                    # InPit still true but the car keeps setting sectors: the "left the pit"
                    # message was lost (seen in real 2026 data) - it is racing
                    d.in_pit = False
                else:
                    long_stay = since is not None and self._now_ms - since >= GARAGE_SECONDS * 1000
                    d.in_garage = d.dnf or long_stay
            d.cutoff = bool(line.get("Cutoff"))

            # ---- tyres ---------------------------------------------------
            a = app.get(num) if isinstance(app.get(num), dict) else {}
            d.grid_position = _i(a.get("GridPos"))
            raw_stints, src = _items(a.get("Stints")), "TimingAppData"
            if not raw_stints:
                # the same stint records in their own topic (when TimingAppData has none for the car)
                raw_stints, src = _items(tss.get(num) if isinstance(tss, dict) else None), "TyreStintSeries"
            stints = []
            for k, st in enumerate(raw_stints):
                if not isinstance(st, dict):
                    continue
                comp = _s(st.get("Compound"))
                if comp in ("UNKNOWN", "TEST_UNKNOWN"):
                    comp = None
                total, start = _i(st.get("TotalLaps")), _i(st.get("StartLaps"))
                stints.append(StintState(compound=comp, new=_b(st.get("New")), tyre_age=total,
                                         laps=(total - start) if total is not None and start is not None else None,
                                         stint=k + 1, source=src))
            d.stints = stints
            if stints:
                d.tyre = stints[-1]
                if d.pit_stops is None and session.session_kind == "race":
                    d.pit_stops = max(0, len(stints) - 1)
            ct = cur_tyres.get(num) if isinstance(cur_tyres, dict) else None
            if isinstance(ct, dict) and d.tyre.compound is None:
                comp = _s(ct.get("Compound"))
                if comp and comp != "UNKNOWN":
                    d.tyre = StintState(compound=comp, new=_b(ct.get("New")), tyre_age=d.tyre.tyre_age,
                                        laps=d.tyre.laps, stint=d.tyre.stint, source="CurrentTyres")

            d.rc = replace(rc.driver_flags.get(str(num), RaceControlFlags()),
                           messages=list(rc.driver_messages.get(str(num), [])))
            self._progress(d, line, (feed.get(LAPS) or {}).get(str(num)),
                           (stats.get(num) or {}) if isinstance(stats.get(num), dict) else {}, kind, part)
            out[str(num)] = d

        def line_of(n: str) -> Optional[int]:
            return _i((dl.get(n) or {}).get("Line")) if isinstance(dl.get(n), dict) else None

        if kind in ("qualifying", "practice"):
            # what each car is doing now: out lap / preparation / hot lap / cooldown / pit
            refs = pace_refs(lines, stats)
            laps_store = feed.get(LAPS) or {}
            for num, d in out.items():
                if d.dnf or d.retired or d.knocked_out:
                    continue
                st = laps_store.get(num)
                state, conf, why = lap_state_classify(st, driver_ref(refs, num), self._now_ms,
                                                      in_pit=d.in_pit or d.in_garage, on_lap=d.lap_now is not None)
                if conf == "LOW":
                    state = "UNKNOWN"
                d.lap_state, d.lap_state_conf, d.lap_state_why = state, conf, why
        if kind in ("qualifying", "practice"):
            return out, self._rank_timed(out, lines, feed, kind, part, _items(td.get("NoEntries")), session)

        # Timing positions normally are a clean 1..N sequence. If they are not
        # (e.g. a lossy recording), fall back to the classification line that
        # the feed also publishes in DriverList.
        positions = [d.position for d in out.values() if d.position is not None]
        lines_ = [line_of(n) for n in out]
        use_line = (len(positions) != len(set(positions)) or not positions) and \
            all(v is not None for v in lines_) and len(set(lines_)) == len(lines_)
        if use_line:
            for n, d in out.items():
                d.position = line_of(n)

        if kind == "race":
            chase = feed.get(CHASE) or {}
            by_pos = {d.position: n for n, d in out.items() if d.position is not None}
            for num, d in out.items():
                c = chase.get(num)
                if isinstance(c, dict):
                    ah = out.get(str(c.get("ahead"))) if c.get("ahead") is not None else None
                    laps, why = int(c.get("laps") or 0), c.get("why")
                    now_ahead = by_pos.get(d.position - 1) if d.position and d.position > 1 else None
                    if laps and now_ahead is not None and str(c.get("ahead")) != now_ahead:
                        laps, why = 0, "ahead"      # passed / was passed since that lap: chase over
                    d.chase = {"laps": laps, "ahead": c.get("ahead"),
                               "ahead_tla": ah.tla if ah else None, "gap": c.get("gap"), "why": why,
                               "threshold": _chase.CHASE_GAP_S}
        def sort_key(n: str):
            d = out[n]
            return (d.position is None, d.position or 99, line_of(n) or 99, _i(n) or 0)

        order = sorted(out.keys(), key=sort_key)
        return out, order

    # ------------------------------------------------------------------ lap / sector progress
    def _progress(self, d: DriverState, line: dict, st: Optional[dict], stats: dict, kind: str,
                  part: Optional[int]) -> None:
        """Current lap / sector from the derived lap tracker (server/laps.py) - a lap being driven
        is never shown as the last completed one; unknown stays None."""
        bs = []
        for s in _items(stats.get("BestSectors"))[:3]:
            bs.append(TimeValue(value=_s(s.get("Value")) if isinstance(s, dict) else None,
                                overall_best=isinstance(s, dict) and _i(s.get("Position")) == 1))
        d.best_sectors = bs
        d.lap_phase = self._lap_phase(d, st if isinstance(st, dict) else {})
        if not isinstance(st, dict):
            return
        hist = st.get("hist") or []
        marks = [[h[3], h[1]] for h in hist if h[3] is not None and (kind != "qualifying" or h[0] == part)]
        d.lap_marks = marks[-40:]
        ls = st.get("last_sec")
        if isinstance(ls, list) and len(ls) >= 2:
            d.last_sector = {"n": int(ls[0]) + 1, "value": ls[1]}
        if d.dnf or d.retired or d.stopped or d.in_pit or d.in_garage or st.get("pit") or \
                (kind == "qualifying" and d.knocked_out):
            return                                   # not driving a lap (pit lane / garage / out)
        act = st.get("act")
        if act is None or self._now_ms - act > IDLE_MS:
            return                                   # no timing activity: not on a lap (garage, lost InPit)
        n = max([x for x in (d.laps, st.get("n")) if x is not None], default=None)
        if n is None:
            return
        d.lap_now = n + 1
        d.lap_how = st.get("how")
        now = self._now_ms
        start = st.get("start")
        if start is not None and 0 <= now - start <= RUNNING_MAX_MS:
            d.lap_start_ms = start
        sec = st.get("sec")
        if sec is not None:
            d.sector_now = int(sec) + 1
            ss = st.get("sec_start")
            if ss is not None and 0 <= now - ss <= RUNNING_MAX_MS:
                d.sector_start_ms = ss

    def _lap_phase(self, d: DriverState, st: dict) -> Optional[str]:
        """OUT LAP / IN LAP / IN PIT / RETIRED / STOPPED from the official pit and lap data only
        (InPit, PitOut, Retired, Stopped and the lap tracker) - never from slow times or missing
        sectors. None: driving a normal lap (or nothing known)."""
        if d.retired or d.dnf:
            return "RETIRED"
        if d.stopped:
            return "STOPPED"
        if d.in_pit or d.in_garage or st.get("pit"):
            # an in lap only when the car came in from a lap on track and has not been there long
            pit_t = st.get("pit_t")
            recent = pit_t is not None and 0 <= self._now_ms - pit_t <= IN_LAP_MS
            return "IN LAP" if st.get("pin") and recent and not d.in_garage else "IN PIT"
        act = st.get("act")
        if act is None or self._now_ms - act > IDLE_MS:
            return None                              # no timing activity: unknown, not guessed
        if st.get("how") in ("pit", "garage") and st.get("opit", st.get("how") == "pit"):
            return "OUT LAP"                         # the lap began at the pit exit
        return None

    @staticmethod
    def _timed_lap(h: list, keep) -> bool:
        """A lap of the tracked history that can be a timed lap: it began at the timing line in the
        same part it ended in and did not go through the pit lane (never an out / in lap, never a
        lap that ran from Q1 into Q2)."""
        return (keep(h) and len(h) > 6 and h[4] == "line" and h[5] == h[0] and not h[6]
                and lap_ms(h[2]) is not None)

    def _best_valid(self, raw: Optional[str], deleted: list[str], hist: list, keep) -> tuple[Optional[str], bool]:
        """Best valid lap of the phase known at the shown moment. F1's own best-lap field decides;
        only when race control deleted that time (the feed does not correct the field - real
        Suzuka 2026 data) the best other timed lap of the same phase is used, or None."""
        if not raw or raw not in deleted:
            return raw, False
        cands = [h[2] for h in hist if self._timed_lap(h, keep) and h[2] not in deleted]
        return (min(cands, key=lap_ms) if cands else None), True

    def _rank_timed(self, out: dict[str, DriverState], lines: dict, feed: FeedState, kind: str,
                    part: Optional[int], entries: list, session: SessionState) -> list[str]:
        """Qualifying / practice: ranking by the best VALID lap known at the shown moment (the
        current Q part only), gaps to P1 - not the race order and not the final result."""
        laps = feed.get(LAPS) or {}
        prefix = phase_prefix(kind, session.session_name)
        idx = (part or 1) - 1
        entries_i = [_i(x) for x in entries]
        timed, untimed, ko = [], [], []
        for num, d in out.items():
            line = lines.get(num) if isinstance(lines.get(num), dict) else {}
            hist = (laps.get(num) or {}).get("hist") or []
            # deleted by race control: the times it names, and the times of the laps it names
            nums_del = set(d.rc.deleted_lap_numbers or [])
            deleted = list(d.rc.deleted_times or []) + [h[2] for h in hist if h[1] in nums_del]
            f1pos = _i(line.get("Position"))
            if kind == "qualifying" and d.knocked_out:
                # the part it was knocked out in: when its KnockedOut flag came (tracked), otherwise
                # from its classification and the number of cars in each part (NoEntries)
                elim = (laps.get(num) or {}).get("ko")
                if elim is None and f1pos is not None:
                    for p in range(len(entries_i), 0, -1):
                        if entries_i[p - 1] is not None and f1pos <= entries_i[p - 1]:
                            elim = p
                            break
                d.out_phase = f"{prefix}{elim}" if elim else "OUT"      # (part unknown: not guessed)
                blt = _items(line.get("BestLapTimes"))
                raw = _s(blt[elim - 1].get("Value")) if elim and 0 <= elim - 1 < len(blt) and \
                    isinstance(blt[elim - 1], dict) else None
                best, d.best_deleted = self._best_valid(raw, deleted, hist, lambda h, e=elim: h[0] == e)
                d.best_lap = TimeValue(value=best)
                d.gap = d.interval = None
                ko.append((-(elim or 0), f1pos or 99, _i(num) or 0, num))
                continue
            if kind == "qualifying":
                blt = _items(line.get("BestLapTimes"))
                raw = _s(blt[idx].get("Value")) if 0 <= idx < len(blt) and isinstance(blt[idx], dict) else None
                keep = (lambda h, p=part: h[0] == p)
            else:
                raw = _value(line.get("BestLapTime"))
                keep = (lambda h: True)
            best, d.best_deleted = self._best_valid(raw, deleted, hist, keep)
            d.best_lap = TimeValue(value=best)
            last = d.last_lap.value
            if last and last in deleted:
                # the last lap was deleted: the last valid lap of this phase (or none)
                d.last_deleted = True
                valid = [h for h in hist if keep(h) and h[2] not in deleted]
                d.last_lap = TimeValue(value=valid[-1][2] if valid else None)
            ms = lap_ms(best)
            if ms is None:
                d.no_time = True
                d.gap = d.interval = None
                untimed.append((f1pos or 99, _i(num) or 0, num))
            else:
                timed.append((ms, f1pos or 99, _i(num) or 0, num))
        timed.sort()
        untimed.sort()
        ko.sort()
        order = [x[3] for x in timed] + [x[2] for x in untimed] + [x[3] for x in ko]
        p1 = timed[0][0] if timed else None
        prev_ms = None
        for i, num in enumerate(order):
            d = out[num]
            d.position = i + 1
            if i < len(timed):
                ms = timed[i][0]
                d.gap = None if i == 0 else fmt_gap(ms - p1)
                d.interval = None if i == 0 else fmt_gap(ms - prev_ms)
                d.best_lap.overall_best = i == 0
                prev_ms = ms
        return order

    def radio(self, feed: FeedState, session: SessionState) -> list[dict]:
        """All team radio clips published so far in this session (server/team_radio.py), newest first.
        In replays / VOD the feed only holds what was published up to the video's time - no spoilers."""
        clips = team_radio.parse_captures(feed.get("TeamRadio") or {}, session.path)
        clips.reverse()
        return clips

    # ------------------------------------------------------------------
    def build(self, feed: FeedState, now: Optional[datetime], speed: float, availability: Availability,
              rc_until: Optional[datetime] = None) -> dict[str, Any]:
        now = now or datetime.now(timezone.utc)
        self._now_ms = now.timestamp() * 1000
        rc = self.race_control(feed, rc_until.timestamp() * 1000 if rc_until else None)
        session = self.session(feed, now, speed)
        track = self.track_status(feed, rc, session)
        self.session_flow(feed, rc, session, track)
        drivers, order = self.drivers(feed, rc, session)
        msgs = [to_dict(m) for m in rc.messages[-self.race_control_max:]]
        msgs.reverse()
        return {
            "session": to_dict(session),
            "track_status": to_dict(track),
            "weather": to_dict(self.weather(feed)),
            "drivers": {n: to_dict(d) for n, d in drivers.items()},
            "order": order,
            "race_control": msgs,
            "radio": self.radio(feed, session),
            "availability": to_dict(availability),
            # phases + markers (structure; incidents only once they are in the past)
            "timeline": self.timeline.to_json(until_ms=self._now_ms, strict=True) if self.timeline is not None
            and not self.timeline.empty() else None,
        }
