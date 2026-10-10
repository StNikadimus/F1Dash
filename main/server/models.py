"""Normalized internal data model.

The frontend only ever sees these structures (serialised with ``to_dict``),
never raw F1 feed packets. A different data source only needs to produce the
same raw topics (or fill these models directly) to drive the dashboard.

Convention: ``None`` means "not provided by the data source" and is rendered
as N/A by the dashboard. Nothing in here is ever estimated.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Optional


@dataclass(slots=True)
class ClockState:
    remaining_ms: Optional[int] = None     # time remaining at the moment of sending
    running: bool = False                  # counting down (extrapolating)
    speed: float = 1.0                     # replay speed factor (1.0 live)


@dataclass(slots=True)
class SessionState:
    meeting_name: Optional[str] = None     # "Japanese Grand Prix"
    official_name: Optional[str] = None
    location: Optional[str] = None
    country: Optional[str] = None
    circuit_key: Optional[int] = None
    circuit_name: Optional[str] = None
    session_key: Optional[int] = None
    session_type: Optional[str] = None     # Practice / Qualifying / Race
    session_name: Optional[str] = None     # "Practice 1", "Sprint Qualifying", "Sprint", "Race"
    session_kind: str = "unknown"          # practice | qualifying | race | unknown
    session_part: Optional[int] = None     # qualifying part 1..3
    quali_cutoff: Optional[int] = None     # positions below this line are in the knock-out zone
    status: Optional[str] = None           # Inactive / Started / Aborted / Finished / Finalised / Ends
    start_utc: Optional[str] = None
    gmt_offset: Optional[str] = None
    year: Optional[int] = None
    path: Optional[str] = None
    lap: Optional[int] = None
    total_laps: Optional[int] = None
    clock: ClockState = field(default_factory=ClockState)
    live: bool = False                     # session currently running
    phase: Optional[str] = None            # Q1 / Q2 / Q3, SQ1..3, FP1..3 (None for races)
    phase_state: Optional[str] = None      # NOT STARTED / RUNNING / SUSPENDED / ENDED
    phase_duration_ms: Optional[int] = None  # official length of the phase (its clock at the start)
    title: Optional[str] = None            # "QUALIFYING — Q2", "FP2 — BEST LAP"
    now_ms: Optional[float] = None         # F1 time this state is for (running lap / sector times)
    state: str = "UNKNOWN"                 # RUNNING / SUSPENDED / FINISHED / NOT STARTED / UNKNOWN
    red_flag: bool = False                 # session suspended (red flag) at the shown moment
    rc_coverage: str = "NONE"              # COMPLETE / PARTIAL / NONE: race control + track + session status data


@dataclass(slots=True)
class TrackStatusState:
    code: Optional[str] = None             # raw F1 code "1".."7"
    status: str = "UNKNOWN"                # GREEN / YELLOW / SC / VSC / VSC_ENDING / RED / CHEQUERED / UNKNOWN
    message: Optional[str] = None
    sc_phase: Optional[str] = None         # "DEPLOYED", "IN THIS LAP", "ENDING" (from race control)
    sector_flags: dict[str, str] = field(default_factory=dict)   # marshal sector -> YELLOW / DOUBLE YELLOW
    overtake: Optional[str] = None         # "ENABLED"/"DISABLED" (session-wide, from race control)
    drs: Optional[str] = None              # "ENABLED"/"DISABLED" (pre-2026 seasons)
    chequered: bool = False
    source: Optional[str] = None           # TrackStatus (official topic) | RaceControl (derived from messages)
    # canonical state for clients: GREEN / YELLOW / DOUBLE_YELLOW / SAFETY_CAR / VSC / VSC_ENDING /
    # RED_FLAG / CHEQUERED / UNKNOWN, or TRACK_STATUS_<code> for a code this version does not know
    state: str = "UNKNOWN"
    timestamp: Optional[int] = None        # F1 ms of the message that set the status (None: unknown)
    pit_exit: Optional[str] = None         # OPEN / CLOSED - only when race control said so
    pit_entry: Optional[str] = None        # OPEN / CLOSED - only when race control said so
    red_flag_restart: bool = False         # running again after a red flag in this session


@dataclass(slots=True)
class WeatherState:
    air_temp: Optional[float] = None
    track_temp: Optional[float] = None
    humidity: Optional[float] = None
    pressure: Optional[float] = None
    wind_speed: Optional[float] = None
    wind_direction: Optional[int] = None
    rainfall: Optional[bool] = None


@dataclass(slots=True)
class TimeValue:
    value: Optional[str] = None
    personal_best: bool = False
    overall_best: bool = False


@dataclass(slots=True)
class StintState:
    compound: Optional[str] = None
    new: Optional[bool] = None
    tyre_age: Optional[int] = None         # TotalLaps (age of the tyre set)
    laps: Optional[int] = None             # laps driven in this stint (TotalLaps - StartLaps)
    stint: Optional[int] = None            # 1 = first set of the session
    source: Optional[str] = None           # TimingAppData | TyreStintSeries | CurrentTyres


@dataclass(slots=True)
class RaceControlFlags:
    investigation: Optional[str] = None    # None / "NOTED" / "UNDER INVESTIGATION" / "AFTER RACE"
    penalties: list[dict[str, Any]] = field(default_factory=list)   # [{"label": "+5s", "served": False, "text": ...}]
    disqualified: bool = False
    black_white: bool = False
    deleted_laps: int = 0
    deleted_times: list[str] = field(default_factory=list)   # lap times deleted (and not reinstated)
    deleted_lap_numbers: list[int] = field(default_factory=list)  # lap numbers named in the deletions
    messages: list[dict[str, Any]] = field(default_factory=list)  # latest race control messages naming this car


@dataclass(slots=True)
class DriverState:
    number: str
    tla: Optional[str] = None
    full_name: Optional[str] = None
    first_name: Optional[str] = None
    last_name: Optional[str] = None
    team: Optional[str] = None
    team_color: Optional[str] = None
    position: Optional[int] = None
    show_position: bool = True
    gap: Optional[str] = None              # to leader (race) / to fastest (practice & quali)
    interval: Optional[str] = None         # to car ahead
    catching: Optional[bool] = None
    last_lap: TimeValue = field(default_factory=TimeValue)
    best_lap: TimeValue = field(default_factory=TimeValue)
    sectors: list[TimeValue] = field(default_factory=list)
    speed_trap: Optional[str] = None
    laps: Optional[int] = None
    pit_stops: Optional[int] = None
    in_pit: bool = False
    in_garage: bool = False       # in the pit for more than GARAGE_SECONDS (or retired there): not on the map
    dnf: bool = False             # out of the race: Retired, or Stopped and not moving for DNF_STOPPED_SECONDS
    pit_out: bool = False
    retired: bool = False
    stopped: bool = False
    knocked_out: bool = False
    cutoff: bool = False
    grid_position: Optional[int] = None
    tyre: StintState = field(default_factory=StintState)
    stints: list[StintState] = field(default_factory=list)
    rc: RaceControlFlags = field(default_factory=RaceControlFlags)
    # ---- lap / sector progress (server/laps.py; None = not known from the timing data)
    lap_now: Optional[int] = None          # lap being driven now (not yet completed)
    lap_start_ms: Optional[float] = None   # F1 time it started (line crossing / pit exit)
    lap_how: Optional[str] = None          # line | pit (out lap) | reset (start time not known)
    # what the car does instead of a normal lap (the sector boxes show this instead of S1-S3):
    # OUT LAP (lap began at the pit exit) | IN LAP (entered the pit lane from a lap on track) |
    # IN PIT (pit lane / garage, not coming from the track) | RETIRED | STOPPED | None = on a lap
    lap_phase: Optional[str] = None
    # race: close behind the same car for this many consecutive completed laps (server/chase.py);
    # {"laps": n, "ahead": number, "ahead_tla": "VER", "gap": 0.8, "why": ...}; None = no lap yet
    chase: Optional[dict] = None
    sector_now: Optional[int] = None       # 1..3: sector being driven now
    sector_start_ms: Optional[float] = None
    last_sector: Optional[dict] = None     # last completed sector {"n": 2, "value": "41.066"}
    best_sectors: list[TimeValue] = field(default_factory=list)   # personal best S1..S3 (TimingStats)
    lap_marks: list = field(default_factory=list)                  # completed laps [[F1 ms, lap], ...]
    # ---- qualifying / practice ranking (best valid lap up to the shown moment)
    no_time: bool = False                  # no valid lap time in the current phase yet
    best_deleted: bool = False             # F1's best lap was deleted by race control (not used)
    last_deleted: bool = False             # the last completed lap was deleted
    out_phase: Optional[str] = None        # knocked out in (Q1 / Q2 / SQ1 ...)
    # ---- qualifying: what the car is doing (server/lap_state.py)
    lap_state: Optional[str] = None        # OUT LAP / PREP / HOT LAP / COOLDOWN / PIT / UNKNOWN
    lap_state_conf: Optional[str] = None   # HIGH / MEDIUM / LOW (LOW is shown as UNKNOWN)
    lap_state_why: Optional[str] = None    # the evidence (debug)


@dataclass(slots=True)
class TelemetryState:
    """Per-car telemetry. Only fields the feed actually carries are filled."""
    speed: Optional[int] = None            # km/h        (CarData channel 2)
    rpm: Optional[int] = None              #             (channel 0)
    gear: Optional[int] = None             #             (channel 3)
    throttle: Optional[int] = None         # 0..100 %    (channel 4)
    brake: Optional[bool] = None           # on/off only (channel 5)
    drs: Optional[str] = None              # OFF / ELIGIBLE / OPEN (channel 45, pre-2026 only)
    ch45_raw: Optional[int] = None
    # Not present in any public F1 live timing topic -> always None (N/A)
    ers_percent: None = None
    ers_mode: None = None
    overtake_mode: None = None


@dataclass(slots=True)
class RaceControlMessage:
    id: str
    utc: Optional[str]
    lap: Optional[int]
    category: Optional[str]
    flag: Optional[str]
    scope: Optional[str]
    sector: Optional[int]
    driver: Optional[str]
    text: str
    severity: str                          # info / yellow / red / green / blue / sc / penalty / investigation / chequered
    tags: list[str] = field(default_factory=list)  # what the message literally is about (see race_control._tags)
    importance: str = "low"                # high / medium / low (for highlighting; from the tags)
    status: Optional[str] = None           # F1's own Status field (safety car messages), if any
    mode: Optional[str] = None             # F1's own Mode field (SAFETY CAR / VIRTUAL SAFETY CAR), if any


@dataclass(slots=True)
class Availability:
    positions: bool = False                # Position.z data received this session
    car_data: bool = False                 # CarData.z data received this session
    token_configured: bool = False
    positions_source: Optional[str] = None # "feed" (live socket / replay / test) or "archive" (public archive stream)
    car_data_source: Optional[str] = None
    positions_age_s: Optional[float] = None  # age of the newest position sample when the state was sent


def to_dict(obj: Any) -> Any:
    return asdict(obj)
