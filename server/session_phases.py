"""Session structure on F1's clock: Q1 / Q2 / Q3 (SQ1..3), practice start / end, red flags.

Everything here comes from the official timing topics - never from a fixed schedule:

* ``ExtrapolatedClock`` - the session clock the TV graphic shows. A post while the clock
  runs says "``Remaining`` at ``Utc``"; the clock is posted when it starts (e.g. 14:59
  exactly one second after it started at 15:00) and when it stops (00:00:00 at the exact
  end, or the value it froze at under a red flag). Remaining values are whole seconds.
* ``SessionData`` - ``Series`` (QualifyingPart 1/2/3 with Utc) and ``StatusSeries``
  (SessionStatus Started / Finished / Aborted ... with Utc; the keyframe repeats the history).
* ``RaceControlMessages`` - red / chequered flags (display markers only).
* ``TimingData.SessionPart`` - fallback for the part when ``SessionData`` is missing.

A *run* is one continuous count-down of the clock. From its posts the F1 time of any clock
reading follows exactly (to the 1 s resolution of the clock):

    zero-anchored run (it ended at 00:00:00 at ``end``):   F1 time of reading R = end - R
    start known (``rem0`` at ``start``):                    F1 time of reading R = start + rem0 - R

Phase durations are the clock value when the phase started (Q3 at Suzuka 2026 was 13:00, not
12:00) - never assumed.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Iterable, Optional

from .telemetry import parse_utc

log = logging.getLogger("phases")


def hms_ms(s: Any) -> Optional[int]:
    if not isinstance(s, str) or not s.strip():
        return None
    try:
        parts = [float(p) for p in s.strip().split(":")]
    except ValueError:
        return None
    while len(parts) < 3:
        parts.insert(0, 0.0)
    h, m, sec = parts[-3:]
    return int(round((h * 3600 + m * 60 + sec) * 1000))


def fmt_clock(ms: Optional[float]) -> str:
    if ms is None:
        return "--"
    s = int(round(ms / 1000))
    h, m, x = s // 3600, (s % 3600) // 60, s % 60
    return f"{h}:{m:02d}:{x:02d}" if h else f"{m:02d}:{x:02d}"


def _utc_ms(v: Any) -> Optional[float]:
    dt = parse_utc(v) if v else None
    return dt.timestamp() * 1000 if dt else None


def session_kind(type_: Optional[str], name: Optional[str]) -> str:
    t, n = (type_ or "").lower(), (name or "").lower()
    if "qualifying" in t or "qualifying" in n or "shootout" in n:
        return "qualifying"
    if t == "race" or n in ("race", "sprint"):
        return "race"
    if "practice" in t or "practice" in n:
        return "practice"
    return "unknown"


def phase_prefix(kind: str, name: Optional[str]) -> str:
    n = (name or "").lower()
    if kind == "qualifying":
        return "SQ" if ("sprint" in n or "shootout" in n) else "Q"
    if kind == "practice":
        digits = "".join(ch for ch in n if ch.isdigit())
        return f"FP{digits}" if digits else "FP"
    return "SPRINT" if "sprint" in n else "RACE"


def phase_label(kind: str, name: Optional[str], part: Optional[int]) -> Optional[str]:
    p = phase_prefix(kind, name)
    if kind == "qualifying":
        return f"{p}{part}" if part else None
    if kind == "practice":
        return p
    return None


# ---------------------------------------------------------------------------
@dataclass
class ClockRun:
    """One continuous count-down of the session clock."""
    ref_u: float                          # first post while running: Utc ...
    ref_rem: int                          # ... and the Remaining it showed
    start: Optional[float] = None         # F1 ms the clock started (None = not seen)
    rem0: Optional[int] = None            # the value it started from (= duration of the phase for its first run)
    start_src: str = ""                   # clock (exact post at start+1 s) | status (SessionStatus Started)
    end: Optional[float] = None           # F1 ms it stopped
    rem_end: Optional[int] = None         # the value it stopped at (0 = the phase ended)

    @property
    def zero(self) -> bool:
        return self.end is not None and self.rem_end == 0

    def time_at(self, rem: float) -> float:
        """F1 ms when the clock showed ``rem`` (ms remaining) during this run."""
        if self.zero:
            return self.end - rem
        if self.start is not None and self.rem0 is not None:
            return self.start + (self.rem0 - rem)
        return self.ref_u + (self.ref_rem - rem)

    def rem_at(self, t: float) -> float:
        if self.zero:
            return self.end - t
        if self.start is not None and self.rem0 is not None:
            return self.rem0 - (t - self.start)
        return self.ref_rem - (t - self.ref_u)

    def covers(self, rem: float) -> Optional[bool]:
        """Did the running clock pass through ``rem``? None = cannot tell (start not seen)."""
        lo = self.rem_end if self.end is not None else 0
        hi = self.rem0
        if hi is None:
            if rem <= self.ref_rem and rem > lo:
                return True
            return None
        return lo < rem < hi

    def to_json(self) -> dict:
        return {"start_ms": self.start, "end_ms": self.end, "rem0_ms": self.rem0, "rem_end_ms": self.rem_end}


@dataclass
class Phase:
    id: str                               # Q1 / SQ2 / FP2 / RACE
    label: str
    part: Optional[int] = None
    window: tuple = (float("-inf"), float("inf"))   # F1 ms range the phase is the current one
    runs: list[ClockRun] = field(default_factory=list)
    start: Optional[float] = None
    start_src: str = ""
    end: Optional[float] = None
    end_src: str = ""
    duration: Optional[int] = None        # official length (clock value at the start), ms

    def to_json(self, until_ms: Optional[float] = None, strict: bool = False) -> dict:
        """strict (the dashboard at a replay moment): only what is known at ``until_ms`` - a start /
        end still in the future is not sent (a later end would give away a stoppage)."""
        start, end = self.start, self.end
        if strict:
            if start is not None and (until_ms is None or start > until_ms):
                start = None
            if end is not None and (until_ms is None or end > until_ms):
                end = None
        return {"id": self.id, "label": self.label, "part": self.part, "start_ms": start,
                "end_ms": end, "duration_ms": self.duration, "clock": bool(self.runs)}


@dataclass
class Marker:
    id: str
    label: str
    ms: float
    kind: str                             # start | end | red | resumed | chequered
    phase: Optional[str] = None
    sync: bool = False                    # precise enough for "SYNC HERE"
    how: str = ""                         # what to look for on the video

    def to_json(self) -> dict:
        return {"id": self.id, "label": self.label, "ms": self.ms, "kind": self.kind, "phase": self.phase,
                "sync": self.sync, "how": self.how}


STRUCTURAL = ("start", "end")             # known structure of the session - never a spoiler


@dataclass
class SessionTimeline:
    kind: str = "unknown"
    name: Optional[str] = None
    phases: list[Phase] = field(default_factory=list)
    markers: list[Marker] = field(default_factory=list)
    source: str = ""

    def empty(self) -> bool:
        return not self.phases

    def phase(self, pid: str) -> Optional[Phase]:
        pid = (pid or "").upper()
        for p in self.phases:
            if p.id == pid:
                return p
        return None

    def phase_at(self, t: float) -> Optional[Phase]:
        for p in self.phases:
            if p.window[0] <= t < p.window[1]:
                return p
        return None

    def marker(self, mid: str) -> Optional[Marker]:
        mid = (mid or "").upper()
        for m in self.markers:
            if m.id == mid:
                return m
        return None

    # ---- clock readings -> F1 time -------------------------------------------------------
    def remaining_to_f1(self, pid: str, rem_ms: float) -> tuple[Optional[float], str]:
        """F1 ms at which the session clock of phase ``pid`` showed ``rem_ms`` remaining."""
        p = self.phase(pid)
        if p is None:
            return None, f"no phase {pid} in this session's timing data"
        if not p.runs:
            return None, f"the session clock of {p.label} is not in the timing data"
        if rem_ms <= 0:
            return None, (f"the clock shows 0:00 for a while after {p.label} ends - use the {p.label} END "
                          "marker (pause exactly when it reaches 0:00)")
        hits, unknown = [], False
        for r in p.runs:
            c = r.covers(rem_ms)
            if c:
                hits.append(r.time_at(rem_ms))
            elif c is None:
                unknown = True
        if len(hits) == 1:
            return hits[0], ""
        if len(hits) > 1:
            return None, f"the {p.label} clock showed {fmt_clock(rem_ms)} more than once (it was stopped and restarted)"
        stopped = [r for r in p.runs if rem_ms in (r.rem0, r.rem_end)]
        if stopped:
            return None, (f"the {p.label} clock stood still at {fmt_clock(rem_ms)} (before the start / under a red "
                          "flag) - use a moment while it counts down")
        if unknown:
            return None, f"the start of the {p.label} clock is not in the data - {fmt_clock(rem_ms)} cannot be placed"
        dur = p.duration
        return None, (f"the {p.label} clock never showed {fmt_clock(rem_ms)}" +
                      (f" ({p.label} was {fmt_clock(dur)} long)" if dur else ""))

    def elapsed_to_f1(self, pid: str, el_ms: float) -> tuple[Optional[float], str]:
        p = self.phase(pid)
        if p is None:
            return None, f"no phase {pid} in this session's timing data"
        if p.duration is None:
            return None, f"the length of {p.label} is not known from the timing data - use Time Remaining"
        if el_ms <= 0 or el_ms >= p.duration:
            return None, f"{p.label} is {fmt_clock(p.duration)} long - elapsed must be between 00:01 and that"
        return self.remaining_to_f1(pid, p.duration - el_ms)

    def clock_at(self, t: float) -> Optional[dict]:
        """Phase clock at F1 time t: {phase, remaining_ms, elapsed_ms, running}."""
        p = self.phase_at(t)
        if p is None or not p.runs:
            return None
        rem, running = None, False
        for r in p.runs:
            lo = r.start if r.start is not None else r.ref_u
            if t < lo:
                if rem is None:
                    rem = r.rem0
                break
            if r.end is None or t < r.end:
                rem, running = r.rem_at(t), True
                break
            rem = r.rem_end
        if rem is None:
            return None
        rem = max(0.0, rem)
        return {"phase": p.id, "remaining_ms": int(rem), "running": running,
                "elapsed_ms": int(p.duration - rem) if p.duration is not None else None,
                "duration_ms": p.duration}

    def to_json(self, until_ms: Optional[float] = None, strict: bool = False) -> dict:
        """strict=False (SYNC menu): the phase starts / ends are offered as SYNC HERE points (they
        are needed before any sync); red flags / resumptions / chequered flags only once the synced
        moment has passed them. strict=True (the dashboard at a replay moment): nothing that lies
        after ``until_ms`` - no future marker, start or end. The clock runs are never sent."""
        if strict:
            marks = [m.to_json() for m in self.markers if until_ms is not None and m.ms <= until_ms]
        else:
            marks = [m.to_json() for m in self.markers
                     if m.kind in STRUCTURAL or (until_ms is not None and m.ms <= until_ms)]
        return {"kind": self.kind, "phases": [p.to_json(until_ms, strict) for p in self.phases],
                "markers": marks, "source": self.source}


# ---------------------------------------------------------------------------
def _merge_series(store: dict, v: Any) -> None:
    if isinstance(v, list):
        for i, e in enumerate(v):
            if isinstance(e, dict):
                store[i] = dict(e)
    elif isinstance(v, dict):
        for k, e in v.items():
            try:
                i = int(k)
            except (TypeError, ValueError):
                continue
            if isinstance(e, dict):
                store.setdefault(i, {}).update(e)


def build_timeline(events: Iterable, session_type: Optional[str] = None,
                   session_name: Optional[str] = None, source: str = "archive") -> SessionTimeline:
    """``events``: (ms, topic, data) tuples (or objects with .t/.topic/.data) in time order."""
    ec: dict = {}
    states: list[tuple[float, int, bool]] = []
    series: dict[int, dict] = {}
    status_series: dict[int, dict] = {}
    status_msgs: list[tuple[float, str]] = []
    part_msgs: list[tuple[float, int]] = []
    rc_flags: list[tuple[float, str]] = []
    info_type, info_name = session_type, session_name
    for ev in events:
        if isinstance(ev, tuple):
            ms, topic, data = ev
        else:
            ms, topic, data = ev.t.timestamp() * 1000, ev.topic, ev.data
        if not isinstance(data, dict):
            continue
        if topic == "ExtrapolatedClock":
            if data.get("_kf") is True:
                ec = {}
            ec.update({k: v for k, v in data.items() if k != "_kf"})
            u = _utc_ms(ec.get("Utc")) or ms
            rem = hms_ms(ec.get("Remaining"))
            if rem is None:
                continue
            if states and u <= states[-1][0]:
                continue                          # a later keyframe repeating an old post
            states.append((u, rem, bool(ec.get("Extrapolating"))))
        elif topic == "SessionData":
            _merge_series(series, data.get("Series"))
            _merge_series(status_series, data.get("StatusSeries"))
        elif topic == "SessionStatus":
            st = data.get("Status")
            if isinstance(st, str):
                status_msgs.append((ms, st))
        elif topic in ("TimingData", "TimingDataF1") and data.get("SessionPart") is not None:
            try:
                part_msgs.append((ms, int(data["SessionPart"])))
            except (TypeError, ValueError):
                pass
        elif topic == "RaceControlMessages":
            msgs = data.get("Messages")
            items = msgs.values() if isinstance(msgs, dict) else msgs if isinstance(msgs, list) else []
            for m in items:
                if not isinstance(m, dict) or str(m.get("Category") or "").upper() != "FLAG":
                    continue
                flag = str(m.get("Flag") or "").upper()
                if flag in ("RED", "CHEQUERED") and str(m.get("Scope") or "Track") == "Track":
                    u = _utc_ms(m.get("Utc"))
                    if u is not None:
                        rc_flags.append((u, flag))
        elif topic == "SessionInfo":
            info_type = info_type or data.get("Type")
            info_name = info_name or data.get("Name")

    kind = session_kind(info_type, info_name)
    tl = SessionTimeline(kind=kind, name=info_name, source=source)
    if kind not in ("qualifying", "practice"):
        return tl                               # races keep L / S / countdown (unchanged)

    # ---- status history (Utc inside the payload; the SessionStatus topic as a fallback)
    started = sorted({u for u in (_utc_ms(e.get("Utc")) for e in status_series.values()
                                  if e.get("SessionStatus") == "Started") if u is not None})
    finished = sorted({u for u in (_utc_ms(e.get("Utc")) for e in status_series.values()
                                   if e.get("SessionStatus") == "Finished") if u is not None})
    aborted = sorted({u for u in (_utc_ms(e.get("Utc")) for e in status_series.values()
                                  if e.get("SessionStatus") == "Aborted") if u is not None})
    if not started:
        started = sorted(ms for ms, s in status_msgs if s == "Started")
    if not finished:
        finished = sorted(ms for ms, s in status_msgs if s == "Finished")

    # ---- clock runs
    runs: list[ClockRun] = []
    cur: Optional[ClockRun] = None
    prev: Optional[tuple[float, int, bool]] = None
    for u, rem, ext in states:
        if ext and cur is None:
            cur = ClockRun(ref_u=u, ref_rem=rem)
            if prev is not None and not prev[2] and 0 <= prev[1] - rem <= 5000 and u - prev[0] >= prev[1] - rem:
                # posted when it started from the stationary value (14:59 one second after 15:00)
                cur.start, cur.rem0, cur.start_src = u - (prev[1] - rem), prev[1], "clock"
        elif not ext and cur is not None:
            cur.end, cur.rem_end = u, rem
            runs.append(cur)
            cur = None
        prev = (u, rem, ext)
    if cur is not None:
        runs.append(cur)
    for i, r in enumerate(runs):
        if r.start is not None:
            continue
        # start not posted in the data (recording began mid-run): the SessionStatus "Started"
        # before the first post gives it; the length is a whole number of seconds
        lo = runs[i - 1].end if i > 0 and runs[i - 1].end is not None else float("-inf")
        cand = [s for s in started if lo < s <= r.ref_u]
        if not cand:
            continue
        s = cand[-1]
        if r.zero:
            # anchored on the exact 0:00 post: start = end - length (whole seconds) - as exact as
            # the clock itself when the status message agrees (it follows the clock by ~0.15 s)
            dur = int(round((r.end - s) / 1000) * 1000)
            r.start, r.rem0 = r.end - dur, dur
            r.start_src = "clock" if abs(r.start - s) <= 1500 else "status"
        else:
            r.rem0 = int(round((r.ref_rem + (r.ref_u - s)) / 1000) * 1000)
            r.start, r.start_src = s, "status"

    # ---- phases
    prefix = phase_prefix(kind, info_name)
    if kind == "qualifying":
        parts = []
        for e in series.values():
            try:
                p = int(e.get("QualifyingPart"))
            except (TypeError, ValueError):
                continue
            u = _utc_ms(e.get("Utc"))
            if p > 0 and u is not None:
                parts.append((u, p))
        parts.sort()
        if not parts:
            parts = sorted((ms, p) for ms, p in part_msgs if p > 0)
        firsts: list[tuple[float, int]] = []
        for u, p in parts:
            if not firsts or p != firsts[-1][1]:
                firsts.append((u, p))
        if not firsts and runs:
            # no part information at all: every run that ends at 0:00 closes a part
            n, t0 = 1, float("-inf")
            firsts.append((t0, n))
            for r in runs[:-1]:
                if r.zero:
                    n += 1
                    firsts.append((r.end + 1, n))
        for i, (u, p) in enumerate(firsts):
            w1 = firsts[i + 1][0] if i + 1 < len(firsts) else float("inf")
            tl.phases.append(Phase(id=f"{prefix}{p}", label=f"{prefix}{p}", part=p,
                                   window=(u if i else float("-inf"), w1)))
    else:
        tl.phases.append(Phase(id=prefix, label=prefix))
    for r in runs:
        at = r.start if r.start is not None else r.ref_u
        for p in tl.phases:
            if p.window[0] <= at < p.window[1]:
                p.runs.append(r)
                break

    # ---- phase start / end and markers
    practice = kind == "practice"
    for p in tl.phases:
        name_s = "SESSION START" if practice else f"{p.label} START"
        name_e = "SESSION END" if practice else f"{p.label} END"
        if p.runs:
            first = p.runs[0]
            # the official length = the clock at the phase's start - only if this run IS the start
            # (data that begins after a red flag must not turn the restart value into the length)
            earlier = [x for x in started + aborted if p.window[0] <= x < (first.start or first.ref_u) - 2000]
            p.duration = first.rem0 if not earlier else None
            if first.start is not None and not earlier:
                p.start, p.start_src = first.start, first.start_src
            zero = [r for r in p.runs if r.zero]
            if zero:
                p.end, p.end_src = zero[-1].end, "clock"
        if p.start is None:
            s = [x for x in started if p.window[0] <= x < p.window[1]]
            if s:
                p.start, p.start_src = s[0], "status"
        if p.end is None:
            f = [x for x in finished if p.window[0] <= x < p.window[1]]
            if f:
                p.end, p.end_src = f[-1], "status"
        if p.start is not None:
            tl.markers.append(Marker(f"{p.id}_START", name_s, p.start, "start", p.id,
                                     sync=p.start_src == "clock",
                                     how="the session clock starts counting down (pit exit opens)"))
        if p.end is not None:
            tl.markers.append(Marker(f"{p.id}_END", name_e, p.end, "end", p.id, sync=p.end_src == "clock",
                                     how="the session clock reaches 0:00"))
        for k in range(1, len(p.runs)):
            a, b = p.runs[k - 1], p.runs[k]
            if a.end is not None and not a.zero:
                tl.markers.append(Marker(f"{p.id}_RED{k}", "RED FLAG", a.end, "red", p.id, sync=True,
                                         how=f"the session clock stops at {fmt_clock(a.rem_end)}"))
            if b.start is not None:
                tl.markers.append(Marker(f"{p.id}_RESUMED{k}", "SESSION RESUMED", b.start, "resumed", p.id,
                                         sync=b.start_src == "clock",
                                         how=f"the session clock restarts from {fmt_clock(b.rem0)}"))
    reds = [m.ms for m in tl.markers if m.kind == "red"]
    n_red = n_chq = 0
    for u, flag in sorted(set(rc_flags)):         # keyframes repeat the messages
        ph = tl.phase_at(u)
        if flag == "RED" and not any(abs(u - r) < 30_000 for r in reds):
            n_red += 1
            tl.markers.append(Marker(f"RC_RED{n_red}", "RED FLAG", u, "red", ph.id if ph else None))
        elif flag == "CHEQUERED":
            n_chq += 1
            tl.markers.append(Marker(f"CHQ{n_chq}", "CHEQUERED FLAG", u, "chequered", ph.id if ph else None))
    tl.markers.sort(key=lambda m: m.ms)
    return tl
