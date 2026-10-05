"""LIGHTS OUT resolver - the one canonical actual race start for the sync (Event Sync, L, the
start state, the stream start, the dashboards).

Where the actual start is in F1's data (verified on the recorded 2026 Japanese GP race):
* ``SessionData.StatusSeries`` entry {"SessionStatus": "Started", "Utc": "...05:14:02.078Z"} - a
  status change with its own millisecond Utc. In a race this is lights out: lap 1 of the leader
  starts at exactly that millisecond (OpenF1 laps.date_start of lap 1 is the same) and the leader
  completes lap 1 one racing lap (~1:35) later - not a formation lap plus a race lap. The F1
  archive has no ``SessionStatus`` topic, so for recordings this is the exact source;
* ``SessionStatus`` {"Status": "Started"} on the live socket (message time, ms);
* OpenF1 ``race_control`` category SessionStatus "SESSION STARTED" (the same millisecond);
* ``ExtrapolatedClock`` starting to run (whole seconds -> about +-1 s): fallback only, never exact.
There is no literal "LIGHTS OUT" message, and the scheduled start (``SessionInfo.StartDate`` /
``sessions.date_start``) is never a start: it is kept as metadata only.

Several "Started" in one session (aborted start, red-flag restart): the race start is the last
one before the first completed lap. Reports of the same start from several sources (within
``CLUSTER_MS``) are one start; the most authoritative source gives its time:

    LIVE   1 F1 TV timing (authenticated socket)   2 F1 SignalR (public socket, also the
           fallback connection)   ...   no result
    VOD    1 F1 archive (stored official timing)   2 OpenF1 (historical)   ...   no result
    any    the session clock (+-1 s) only when nothing exact exists

Confidence: VERY HIGH = F1 TV timing, or two independent sources agreeing (<= AGREE_MS);
HIGH = one exact source; MEDIUM = the session clock only; a disagreement between exact sources
is reported (``conflict``) and lowers the confidence one level - the hierarchy decides the time.
"""
from __future__ import annotations

import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

# source family of an observation label "<family> · <topic>"
F1TV, SIGNALR, ARCHIVE, OPENF1, SIM = "F1 TV timing", "F1 SignalR", "F1 archive", "OpenF1", "Simulator"
RANK = {F1TV: 0, SIGNALR: 1, ARCHIVE: 2, OPENF1: 3, SIM: 1}
BASE_CONF = {F1TV: "VERY HIGH", SIGNALR: "HIGH", ARCHIVE: "HIGH", OPENF1: "HIGH", SIM: "HIGH"}
LEVELS = ("LOW", "MEDIUM", "HIGH", "VERY HIGH")
CLUSTER_MS = 30_000          # reports of one start (sources disagree by seconds, restarts are minutes apart)
AGREE_MS = 1_000             # two exact sources agree within this
APPROX_RANK = 9


def family(label: str) -> str:
    return (label or "").split(" · ", 1)[0]


def rank(label: str, approx: bool) -> int:
    return APPROX_RANK if approx else RANK.get(family(label), 5)


def utc(ms: Optional[float]) -> Optional[str]:
    if ms is None:
        return None
    return datetime.fromtimestamp(ms / 1000, timezone.utc).strftime("%H:%M:%S.%f")[:-3]


def _shift(level: str, d: int) -> str:
    i = LEVELS.index(level) if level in LEVELS else 1
    return LEVELS[max(0, min(len(LEVELS) - 1, i + d))]


@dataclass
class LightsOutResult:
    """The canonical LIGHTS OUT event (the same model for LIVE and VOD, whatever the source)."""
    event_type: str = "LIGHTS_OUT"
    timestamp_ms: Optional[float] = None       # None = unavailable (see reason)
    source: Optional[str] = None               # label of the observation that gives the time
    family: Optional[str] = None
    confidence: str = "UNKNOWN"
    meeting_key: Any = None
    session_key: Any = None
    session_name: Optional[str] = None
    historical: bool = False
    scheduled_ms: Optional[float] = None       # metadata only - never the start
    sources: list = field(default_factory=list)   # every observation of this start: source, time, agrees
    agreeing: int = 0                          # independent source families within AGREE_MS
    conflict: bool = False
    conflict_detail: Optional[str] = None
    start_index: int = 0                       # which "Started" of the session (restarts after it)
    starts: int = 0
    reason: Optional[str] = None
    resolved_at: float = 0.0
    cached: bool = False

    @property
    def ok(self) -> bool:
        return self.timestamp_ms is not None

    def to_json(self) -> dict:
        d = asdict(self)
        d["utc"] = utc(self.timestamp_ms)
        d["scheduled_utc"] = utc(self.scheduled_ms)
        return d


def clusters(obs: list) -> list[list]:
    """Observations [(ms, label, approx)] -> start events (time order): reports within CLUSTER_MS
    of each other belong to the same start."""
    out: list[list] = []
    for o in sorted(obs, key=lambda x: x[0]):
        if out and o[0] - out[-1][-1][0] <= CLUSTER_MS:
            out[-1].append(o)
        else:
            out.append([o])
    return out


def best(cluster: list) -> tuple:
    """The most authoritative report of one start (exact before the session clock, then the
    source hierarchy, then the earliest)."""
    return min(cluster, key=lambda o: (rank(o[1], o[2]), o[0]))


def resolve_lights_out(obs: list, first_crossing: Optional[float], session: Optional[dict], *,
                       historical: bool, at_ms: Optional[float] = None, cache: Optional[dict] = None,
                       race: bool = True) -> LightsOutResult:
    """The actual race start of ``session`` from the observations of THIS session's data
    (``obs`` = [(ms, label, approx)]). ``at_ms``: only what is known at that F1 time (live).
    ``cache``: a saved result of the same session key, used when the data has none (yet)."""
    sess = session or {}
    from .telemetry import parse_utc
    sched = parse_utc(sess.get("date_start")) if sess.get("date_start") else None
    res = LightsOutResult(meeting_key=sess.get("meeting_key"), session_key=sess.get("session_key"),
                          session_name=sess.get("session_name"), historical=historical,
                          scheduled_ms=sched.timestamp() * 1000 if sched else None, resolved_at=time.time())
    known = [o for o in obs if at_ms is None or o[0] <= at_ms]
    cl = clusters(known)
    idx = len(cl) - 1
    if cl and first_crossing is not None:
        # the race start: the last start before the first completed lap (an aborted start is
        # followed by another start; a restart after a red flag comes after laps)
        before = [i for i, c in enumerate(cl) if best(c)[0] <= first_crossing + 1000]
        idx = before[-1] if before else -1          # only restarts known: the start itself is not
    if cl and idx >= 0:
        c = cl[idx]
        b = best(c)
        res.timestamp_ms, res.source, res.family = b[0], b[1], family(b[1])
        res.start_index, res.starts = idx, len(cl)
        exact = [o for o in c if not o[2]]
        fams: dict[str, tuple] = {}
        for o in sorted(exact, key=lambda x: (rank(x[1], False), x[0])):
            fams.setdefault(family(o[1]), o)
        agree = [f for f, o in fams.items() if abs(o[0] - b[0]) <= AGREE_MS]
        disagree = [f for f, o in fams.items() if abs(o[0] - b[0]) > AGREE_MS]
        res.sources = [{"source": o[1], "utc": utc(o[0]), "approx": o[2],
                        "agrees": abs(o[0] - b[0]) <= AGREE_MS} for o in sorted(c, key=lambda x: x[0])]
        res.agreeing = len(agree)
        if b[2]:
            res.confidence = "MEDIUM"                    # the session clock only (+-1 s)
        else:
            level = BASE_CONF.get(res.family, "HIGH")
            if len(agree) >= 2:
                level = _shift(level, +1)
            if disagree:
                res.conflict = True
                res.conflict_detail = "; ".join(f"{f} {utc(fams[f][0])} ({(fams[f][0] - b[0]) / 1000:+.2f} s)"
                                                for f in disagree) + f" vs {res.family} {utc(b[0])}"
                level = _shift(BASE_CONF.get(res.family, "HIGH"), -1)
            res.confidence = level
        if not race:
            res.event_type = "SESSION_START"
        return res
    if cache and cache.get("session_key") is not None and cache.get("session_key") == sess.get("session_key") \
            and cache.get("timestamp_ms") is not None:
        res.timestamp_ms = float(cache["timestamp_ms"])
        res.source = cache.get("source")
        res.family = family(res.source or "")
        res.confidence = cache.get("confidence") or "HIGH"
        res.cached = True
        res.resolved_at = float(cache.get("resolved_at") or 0)
        res.starts = 1
        return res
    res.reason = "No verified actual race-start timestamp found" + (
        " (only restarts after the first lap are in the data)" if cl else "")
    return res
