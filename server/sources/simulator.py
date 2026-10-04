"""TEST MODE simulator.

Produces F1-shaped raw topic messages (TimingData, TimingAppData, Position.z,
CarData.z, RaceControlMessages, ...) so the complete pipeline - decoder,
normalizer, race-control parser, websocket and UI - is exercised exactly as in
live mode. Everything produced here is SIMULATED and the dashboard shows a
permanent TEST MODE banner. The roster is fictional on purpose.

The circuit outline is a real circuit (bacinger/f1-circuits GeoJSON). The pit
lane and the 20 marshal sectors of the test track are synthetic test geometry.
"""
from __future__ import annotations

import asyncio
import logging
import math
import random
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from ..telemetry import encode_z
from ..track import TrackGeometry
from .base import Sink, Source

log = logging.getLogger("simulator")

TEAMS = [
    ("Aurora Racing", "E8002D"), ("Blackwater GP", "3671C6"), ("Cobalt Motorsport", "00A3E0"),
    ("Delta Velocity", "FF8000"), ("Evergreen F1", "229971"), ("Falcon Works", "B6BABD"),
    ("Granite Racing", "6692FF"), ("Helios Team", "FFD200"), ("Ironclad GP", "B00020"),
    ("Jade Performance", "52E252"), ("Kestrel Racing", "9B59FF"),
]
DRIVERS = [
    ("7", "ARN", "Arne", "Nordin"), ("21", "BEL", "Bela", "Lukacs"), ("4", "CAS", "Cas", "Vermeer"),
    ("15", "DAV", "Davi", "Moreira"), ("9", "EKS", "Elias", "Ekstrom"), ("28", "FON", "Felipe", "Fonseca"),
    ("2", "GAR", "Gael", "Arnaud"), ("33", "HAN", "Hana", "Sato"), ("11", "IVE", "Ivo", "Vesely"),
    ("26", "JOR", "Jonas", "Reiter"), ("17", "KAI", "Kai", "Lindqvist"), ("38", "LOR", "Lorenzo", "Rinaldi"),
    ("5", "MAT", "Matej", "Kovac"), ("19", "NIL", "Nils", "Hagen"), ("24", "OSC", "Oscar", "Duval"),
    ("8", "PAB", "Pablo", "Serrano"), ("36", "QUI", "Quinn", "Maddox"), ("13", "RIO", "Rio", "Tanaka"),
    ("22", "SAM", "Sami", "Virtanen"), ("30", "TOM", "Tomas", "Blaha"), ("12", "UGO", "Ugo", "Ferrand"),
    ("40", "VIK", "Viktor", "Stal"),
]
COMPOUNDS = ["SOFT", "MEDIUM", "HARD"]


def fmt_lap(sec: float) -> str:
    m = int(sec // 60)
    return f"{m}:{sec - m * 60:06.3f}" if m else f"{sec:.3f}"


@dataclass
class Car:
    num: str
    tla: str
    idx: int
    pace: float
    dist: float = 0.0              # metres from start line (race distance)
    speed: float = 0.0             # m/s
    lap_start_t: float = 0.0
    sector_start_t: float = 0.0
    sector: int = 0
    laps: int = 0
    best: Optional[float] = None
    best_sectors: list = field(default_factory=lambda: [None, None, None])
    stints: list = field(default_factory=list)   # [{"Compound","New","TotalLaps","StartLaps"}]
    pit_plan: list = field(default_factory=list)
    pit_state: Optional[str] = None             # None / "in" / "stopped" / "out"
    pit_timer: float = 0.0
    pit_stops: int = 0
    pit_out_until: float = -1.0
    retired: bool = False
    last_acc: float = 0.0


class SimulatorSource(Source):
    mode = "test"

    def __init__(self, cfg: dict[str, Any], geometry: TrackGeometry) -> None:
        self.total_laps = int(cfg.get("laps", 50))
        self.scale = max(0.1, float(cfg.get("time_scale", 1.0)))
        self.speed = 1.0          # all emitted timestamps are wall-clock based
        self.geometry = geometry
        self._prepare_geometry()
        self.rng = random.Random(42)
        self._t0_wall = datetime.now(timezone.utc)
        self.pending_events: list[str] = []       # TEST: events requested from the dashboard (SIM_EVENT)

    EVENTS = ("green", "yellow", "dy", "vsc", "sc", "red", "chequered", "overtake", "pit", "fastest", "longrc")

    def inject(self, name: str) -> str:
        """TEST mode only: emit the real F1-style messages for an event now (the dashboard animates
        from them exactly as from a live feed)."""
        name = (name or "").lower()
        if name not in self.EVENTS:
            return "Unknown event - one of: " + ", ".join(self.EVENTS)
        self.pending_events.append(name)
        return f"Simulated event: {name}"

    async def _apply_events(self, sink: Sink) -> None:
        while self.pending_events:
            ev = self.pending_events.pop(0)
            if ev == "green":
                self.sector_flags.clear()
                await self._track(sink, "1")
                await self._emit(sink, "SessionStatus", {"Status": "Started"})
                await self._rc(sink, {"Category": "Flag", "Flag": "GREEN", "Scope": "Track",
                                      "Message": "GREEN LIGHT - PIT EXIT OPEN"})
            elif ev == "yellow":
                self.sector_flags.add(7)
                await self._rc(sink, {"Category": "Flag", "Flag": "YELLOW", "Scope": "Sector", "Sector": 7,
                                      "Message": "YELLOW IN TRACK SECTOR 7"})
                await self._track(sink, "2")
            elif ev == "dy":
                self.sector_flags.add(15)
                await self._rc(sink, {"Category": "Flag", "Flag": "DOUBLE YELLOW", "Scope": "Sector", "Sector": 15,
                                      "Message": "DOUBLE YELLOW IN TRACK SECTOR 15"})
                await self._track(sink, "2")
            elif ev == "vsc":
                await self._track(sink, "6")
                await self._rc(sink, {"Category": "SafetyCar", "Status": "DEPLOYED", "Mode": "VIRTUAL SAFETY CAR",
                                      "Message": "VIRTUAL SAFETY CAR DEPLOYED"})
            elif ev == "sc":
                await self._track(sink, "4")
                await self._rc(sink, {"Category": "SafetyCar", "Status": "DEPLOYED", "Mode": "SAFETY CAR",
                                      "Message": "SAFETY CAR DEPLOYED"})
            elif ev == "red":
                await self._track(sink, "5")
                await self._rc(sink, {"Category": "Flag", "Flag": "RED", "Scope": "Track", "Message": "RED FLAG"})
                await self._rc(sink, {"Category": "Other", "Message": "PIT EXIT CLOSED"})
                await self._emit(sink, "SessionStatus", {"Status": "Aborted"})
            elif ev == "longrc":                      # a long message (two-line race control layout)
                await self._rc(sink, {"Category": "Other", "Message": "FIA STEWARDS: TURN 4 INCIDENT INVOLVING CAR 44 (HAM) "
                                      "WILL BE INVESTIGATED AFTER THE RACE - LEAVING THE TRACK AND GAINING AN ADVANTAGE"})
            elif ev == "chequered":
                await self._track(sink, "1")
                await self._rc(sink, {"Category": "Flag", "Flag": "CHEQUERED", "Scope": "Track", "Message": "CHEQUERED FLAG"})
                await self._emit(sink, "SessionStatus", {"Status": "Finished"})
            elif ev in ("overtake", "pit", "fastest"):
                running = sorted((c for c in self.cars if not c.retired), key=lambda c: -c.dist)
                if len(running) < 5:
                    continue
                if ev == "overtake":                 # P5 passes P4 (a real gap change, timing follows)
                    a, b = running[3], running[4]
                    b.dist, a.dist = a.dist + 3.0, b.dist
                elif ev == "pit":                   # P3 pits at the pit entry of this lap
                    car = running[2]
                    if car.pit_state is None:
                        car.pit_plan = [int(car.dist // self.length) + 1] + car.pit_plan
                else:                               # P2 sets the fastest lap at its next line crossing
                    running[1].pace *= 1.02             # (higher pace = faster)

    # ------------------------------------------------------------------
    # geometry
    # ------------------------------------------------------------------
    def _prepare_geometry(self) -> None:
        pts = self.geometry.points
        self.seg = [math.dist(pts[i], pts[i + 1]) / 10.0 for i in range(len(pts) - 1)]   # metres
        self.cum = [0.0]
        for s in self.seg:
            self.cum.append(self.cum[-1] + s)
        self.length = self.cum[-1]
        n = len(pts)
        # speed profile from curvature
        kappa = []
        for i in range(n):
            a, b, c = pts[(i - 4) % (n - 1)], pts[i % (n - 1)], pts[(i + 4) % (n - 1)]
            ab, bc = math.dist(a, b) / 10, math.dist(b, c) / 10
            ang1 = math.atan2(b[1] - a[1], b[0] - a[0])
            ang2 = math.atan2(c[1] - b[1], c[0] - b[0])
            d = abs((ang2 - ang1 + math.pi) % (2 * math.pi) - math.pi)
            kappa.append(d / max(ab + bc, 1e-3))
        vmax = 90.0
        v = [min(vmax, math.sqrt(24.0 / k)) if k > 1e-5 else vmax for k in kappa]
        v = [max(19.0, x) for x in v]
        for _ in range(2):                      # braking / acceleration limits
            for i in range(n - 2, -1, -1):
                ds = max(self.seg[i % len(self.seg)], 0.1)
                v[i] = min(v[i], math.sqrt(v[i + 1] ** 2 + 2 * 38.0 * ds))
            for i in range(1, n):
                ds = max(self.seg[(i - 1) % len(self.seg)], 0.1)
                v[i] = min(v[i], math.sqrt(v[i - 1] ** 2 + 2 * 11.0 * ds))
        self.vprof = v
        # 20 synthetic marshal sectors for the test track
        ms = []
        for k in range(20):
            s = int(k * (n - 1) / 20)
            e = int((k + 1) * (n - 1) / 20) % (n - 1)
            ms.append({"n": k + 1, "start": s, "end": e})
        self.geometry.marshal_sectors = ms
        # synthetic pit lane parallel to the start/finish straight
        self.pit_entry = self.length - 260.0
        self.pit_exit = 240.0
        lane = []
        steps = 60
        for k in range(steps + 1):
            d = self.pit_entry + (self.pit_exit + self.length - self.pit_entry) * k / steps
            x, y, hx, hy = self._point_heading(d % self.length)
            f = min(1.0, k / 10, (steps - k) / 10)          # taper in/out
            off = 220.0 * f                                  # 22 m offset (decimetres)
            lane.append([x - hy * off, y + hx * off])
        self.geometry.pitlane = lane
        self.geometry.pitlane_source = "TEST: synthetic pit lane"
        self.geometry.name = f"{self.geometry.name} (TEST)"
        self.lane_len = sum(math.dist(lane[i], lane[i + 1]) for i in range(len(lane) - 1)) / 10.0

    def _locate(self, d: float) -> tuple[int, float]:
        d %= self.length
        lo, hi = 0, len(self.cum) - 1
        while lo < hi - 1:
            mid = (lo + hi) // 2
            if self.cum[mid] <= d:
                lo = mid
            else:
                hi = mid
        seg = self.seg[lo] or 1e-6
        return lo, (d - self.cum[lo]) / seg

    def _point_heading(self, d: float) -> tuple[float, float, float, float]:
        i, t = self._locate(d)
        a, b = self.geometry.points[i], self.geometry.points[i + 1]
        dx, dy = b[0] - a[0], b[1] - a[1]
        norm = math.hypot(dx, dy) or 1.0
        return a[0] + dx * t, a[1] + dy * t, dx / norm, dy / norm

    def _vtarget(self, d: float) -> float:
        i, t = self._locate(d)
        return self.vprof[i] * (1 - t) + self.vprof[i + 1] * t

    def _pit_xy(self, frac: float) -> tuple[float, float]:
        lane = self.geometry.pitlane
        pos = max(0.0, min(1.0, frac)) * (len(lane) - 1)
        i = min(int(pos), len(lane) - 2)
        t = pos - i
        return (lane[i][0] + (lane[i + 1][0] - lane[i][0]) * t,
                lane[i][1] + (lane[i + 1][1] - lane[i][1]) * t)

    # ------------------------------------------------------------------
    # time
    # ------------------------------------------------------------------
    def now(self) -> datetime:
        return self._t0_wall + timedelta(seconds=self.sim_t / self.scale) if hasattr(self, "sim_t") else datetime.now(timezone.utc)

    def _ts(self) -> datetime:
        return self._t0_wall + timedelta(seconds=self.sim_t / self.scale)

    def map_time(self, ts: datetime) -> int:
        return int(ts.timestamp() * 1000)

    # ------------------------------------------------------------------
    async def run(self, sink: Sink) -> None:
        while True:
            await self._race(sink)
            await asyncio.sleep(20)

    async def _emit(self, sink: Sink, topic: str, data: Any) -> None:
        await sink.feed(topic, data, self._ts())

    async def _race(self, sink: Sink) -> None:
        rng = self.rng
        self._t0_wall = datetime.now(timezone.utc)
        self.sim_t = 0.0
        self.rc_msgs: list[dict] = []
        self.track_status = "1"
        self.sector_flags: set[int] = set()
        self.phase = "GREEN"
        self.overall_best: Optional[float] = None
        self.overall_best_sec = [None, None, None]
        await sink.begin_snapshot()
        sink.set_status(state="connected", detail="Simulator running")

        order = list(range(len(DRIVERS)))
        rng.shuffle(order)
        self.cars: list[Car] = []
        for grid, di in enumerate(order):
            num, tla, _, _ = DRIVERS[di]
            car = Car(num=num, tla=tla, idx=di, pace=1.0 - grid * 0.0018 + rng.uniform(-0.002, 0.002))
            car.dist = -8.0 * grid - 5.0
            comp = COMPOUNDS[rng.choice([0, 1, 1, 2])]
            car.stints = [{"Compound": comp, "New": "true", "TotalLaps": 0, "StartLaps": 0, "LapFlags": 0,
                           "TyresNotChanged": "0"}]
            first = rng.randint(max(2, self.total_laps // 5), self.total_laps // 2)
            car.pit_plan = [first] + ([rng.randint(first + 8, self.total_laps - 4)] if rng.random() < 0.4 else [])
            self.cars.append(car)
        await self._emit_static(sink, order)

        tick = 0.1
        next_pos, next_timing, next_weather = 0.0, 0.0, 0.0
        pos_buffer: list[dict] = []
        car_buffer: list[dict] = []
        finished = False
        while not finished:
            await asyncio.sleep(tick / self.scale)
            self.sim_t += tick
            await self._script(sink)
            await self._apply_events(sink)
            self._physics(tick)
            await self._lap_events(sink)

            if self.sim_t >= next_pos:
                next_pos = self.sim_t + 0.25
                pos_buffer.append(self._position_entry())
                car_buffer.append(self._cardata_entry())
                if len(pos_buffer) >= 2:
                    await self._emit(sink, "Position.z", encode_z({"Position": pos_buffer}))
                    await self._emit(sink, "CarData.z", encode_z({"Entries": car_buffer}))
                    pos_buffer, car_buffer = [], []
            if self.sim_t >= next_timing:
                next_timing = self.sim_t + 1.0
                await self._emit_gaps(sink)
            if self.sim_t >= next_weather:
                next_weather = self.sim_t + 60
                await self._emit_weather(sink)
            leader = max(self.cars, key=lambda c: c.dist)
            if leader.laps >= self.total_laps:
                await self._rc(sink, {"Category": "Flag", "Flag": "CHEQUERED", "Scope": "Track",
                                      "Message": "CHEQUERED FLAG"})
                await self._emit(sink, "SessionStatus", {"Status": "Finished"})
                await self._emit(sink, "SessionInfo", {"SessionStatus": "Finished"})
                finished = True
        sink.set_status(state="finished", detail="Simulated race finished - restarting")

    # ------------------------------------------------------------------
    async def _emit_static(self, sink: Sink, order: list[int]) -> None:
        start = self._ts()
        await self._emit(sink, "SessionInfo", {
            "Meeting": {"Key": 0, "Name": "Test Grand Prix", "OfficialName": "SIMULATED TEST GRAND PRIX",
                        "Location": self.geometry.name, "Country": {"Name": "Simulation"},
                        "Circuit": {"Key": -1, "ShortName": self.geometry.name}},
            "SessionStatus": "Started", "Key": 1, "Type": "Race", "Name": "Race",
            "StartDate": start.strftime("%Y-%m-%dT%H:%M:%S"), "GmtOffset": "00:00:00",
            "Path": f"{start.year}/TEST/", "_kf": True})
        await self._emit(sink, "SessionStatus", {"Status": "Started"})
        dl = {}
        for grid, di in enumerate(order):
            num, tla, first, last = DRIVERS[di]
            team, colour = TEAMS[di // 2]
            dl[num] = {"RacingNumber": num, "Tla": tla, "FirstName": first, "LastName": last,
                       "FullName": f"{first} {last.upper()}", "BroadcastName": f"{first[0]} {last.upper()}",
                       "TeamName": team, "TeamColour": colour, "Line": grid + 1}
        await self._emit(sink, "DriverList", dl)
        lines, app = {}, {}
        for grid, car in enumerate(self.cars):
            lines[car.num] = {"RacingNumber": car.num, "Position": str(grid + 1), "Line": grid + 1,
                              "ShowPosition": True, "GapToLeader": "", "IntervalToPositionAhead": {"Value": ""},
                              "NumberOfLaps": 0, "NumberOfPitStops": 0, "InPit": False, "PitOut": False,
                              "Retired": False, "Stopped": False,
                              "Sectors": [{"Value": ""}, {"Value": ""}, {"Value": ""}],
                              "LastLapTime": {"Value": ""}, "BestLapTime": {"Value": ""},
                              "Speeds": {"ST": {"Value": ""}}}
            app[car.num] = {"RacingNumber": car.num, "GridPos": str(grid + 1), "Stints": list(car.stints)}
        await self._emit(sink, "TimingData", {"Lines": lines, "_kf": True})
        await self._emit(sink, "TimingAppData", {"Lines": app, "_kf": True})
        await self._emit(sink, "LapCount", {"CurrentLap": 1, "TotalLaps": self.total_laps, "_kf": True})
        await self._emit(sink, "TrackStatus", {"Status": "1", "Message": "AllClear", "_kf": True})
        await self._emit(sink, "RaceControlMessages", {"Messages": [], "_kf": True})
        await self._emit(sink, "ExtrapolatedClock", {"Utc": start.isoformat().replace("+00:00", "Z"),
                                                     "Remaining": "02:00:00", "Extrapolating": True})
        await self._rc(sink, {"Category": "Other", "Message": "SIMULATION: TEST SESSION STARTED"})
        await self._emit_weather(sink)

    async def _emit_weather(self, sink: Sink) -> None:
        r = self.rng
        rain = 1 if 300 <= (self.sim_t % 620) < 380 else 0
        await self._emit(sink, "WeatherData", {
            "AirTemp": f"{22 + r.uniform(-0.5, 0.5):.1f}", "TrackTemp": f"{38 + r.uniform(-1, 1):.1f}",
            "Humidity": f"{55 + r.uniform(-3, 3):.1f}", "Pressure": f"{1012 + r.uniform(-1, 1):.1f}",
            "Rainfall": str(rain), "WindDirection": str(int(r.uniform(90, 140))),
            "WindSpeed": f"{r.uniform(1, 4):.1f}"})

    async def _rc(self, sink: Sink, msg: dict) -> None:
        full = {"Utc": self._ts().strftime("%Y-%m-%dT%H:%M:%S"), "Lap": self._leader_lap(), **msg}
        idx = len(self.rc_msgs)
        self.rc_msgs.append(full)
        await self._emit(sink, "RaceControlMessages", {"Messages": {str(idx): full}})

    def _leader_lap(self) -> int:
        return max(1, min(self.total_laps, max(c.laps for c in self.cars) + 1))

    async def _track(self, sink: Sink, code: str) -> None:
        names = {"1": "AllClear", "2": "Yellow", "4": "SCDeployed", "5": "Red", "6": "VSCDeployed", "7": "VSCEnding"}
        self.track_status = code
        self.phase = {"1": "GREEN", "2": "GREEN", "4": "SC", "5": "RED", "6": "VSC", "7": "VSC"}[code]
        await self._emit(sink, "TrackStatus", {"Status": code, "Message": names[code]})

    def _car_label(self, k: int) -> str:
        car = self.cars[k % len(self.cars)]
        return f"CAR {car.num} ({car.tla})"

    async def _script(self, sink: Sink) -> None:
        """Scripted event cycle (repeats every 620 simulated seconds)."""
        prev = (self.sim_t - 0.1) % 620
        now = self.sim_t % 620
        cyc = int(self.sim_t // 620)

        def at(sec: float) -> bool:
            return prev < sec <= now

        c = lambda k: self._car_label(k + cyc * 3)          # noqa: E731
        if at(20):
            await self._rc(sink, {"Category": "Other", "Message": "OVERTAKE ENABLED"})
        if at(40):
            self.sector_flags.add(7)
            await self._rc(sink, {"Category": "Flag", "Flag": "YELLOW", "Scope": "Sector", "Sector": 7,
                                  "Message": "YELLOW IN TRACK SECTOR 7"})
            await self._track(sink, "2")
        if at(70):
            self.sector_flags.discard(7)
            await self._rc(sink, {"Category": "Flag", "Flag": "CLEAR", "Scope": "Sector", "Sector": 7,
                                  "Message": "CLEAR IN TRACK SECTOR 7"})
            await self._track(sink, "1")
        if at(90):
            await self._rc(sink, {"Category": "Other",
                                  "Message": f"TURN 1 INCIDENT INVOLVING {c(2).replace('CAR', 'CARS')} AND {c(5)[4:]} NOTED - CAUSING A COLLISION"})
        if at(110):
            await self._rc(sink, {"Category": "Other",
                                  "Message": f"FIA STEWARDS: TURN 1 INCIDENT INVOLVING {c(2).replace('CAR', 'CARS')} AND {c(5)[4:]} UNDER INVESTIGATION - CAUSING A COLLISION"})
        if at(150):
            await self._rc(sink, {"Category": "Other",
                                  "Message": f"FIA STEWARDS: 5 SECOND TIME PENALTY FOR {c(2)} - CAUSING A COLLISION"})
            await self._rc(sink, {"Category": "Other",
                                  "Message": f"FIA STEWARDS: TURN 1 INCIDENT INVOLVING {c(2).replace('CAR', 'CARS')} AND {c(5)[4:]} REVIEWED NO FURTHER INVESTIGATION FOR {c(5)}"})
        if at(170):
            await self._rc(sink, {"Category": "Other",
                                  "Message": f"{c(8)} TIME 1:35.123 DELETED - TRACK LIMITS AT TURN 13 LAP 4"})
        if at(180):
            await self._track(sink, "6")
            await self._rc(sink, {"Category": "SafetyCar", "Status": "DEPLOYED", "Mode": "VIRTUAL SAFETY CAR",
                                  "Message": "VIRTUAL SAFETY CAR DEPLOYED"})
        if at(205):
            await self._track(sink, "7")
            await self._rc(sink, {"Category": "SafetyCar", "Status": "ENDING", "Mode": "VIRTUAL SAFETY CAR",
                                  "Message": "VIRTUAL SAFETY CAR ENDING"})
        if at(215):
            await self._track(sink, "1")
            await self._rc(sink, {"Category": "Flag", "Flag": "GREEN", "Scope": "Track", "Message": "TRACK CLEAR"})
        if at(240):
            self.sector_flags.add(15)
            await self._rc(sink, {"Category": "Flag", "Flag": "DOUBLE YELLOW", "Scope": "Sector", "Sector": 15,
                                  "Message": "DOUBLE YELLOW IN TRACK SECTOR 15"})
            await self._track(sink, "2")
        if at(250):
            await self._track(sink, "4")
            await self._rc(sink, {"Category": "SafetyCar", "Status": "DEPLOYED", "Mode": "SAFETY CAR",
                                  "Message": "SAFETY CAR DEPLOYED"})
        if at(300):
            await self._rc(sink, {"Category": "SafetyCar", "Status": "IN THIS LAP", "Mode": "SAFETY CAR",
                                  "Message": "SAFETY CAR IN THIS LAP"})
        if at(330):
            self.sector_flags.discard(15)
            await self._rc(sink, {"Category": "Flag", "Flag": "CLEAR", "Scope": "Sector", "Sector": 15,
                                  "Message": "CLEAR IN TRACK SECTOR 15"})
            await self._track(sink, "1")
            await self._rc(sink, {"Category": "Flag", "Flag": "CLEAR", "Scope": "Track", "Message": "TRACK CLEAR"})
        if at(360):
            await self._rc(sink, {"Category": "Other",
                                  "Message": f"FIA STEWARDS: TURN 16 INCIDENT INVOLVING {c(11)} UNDER INVESTIGATION - LEAVING THE TRACK AND GAINING AN ADVANTAGE"})
        if at(390):
            await self._rc(sink, {"Category": "Other",
                                  "Message": f"FIA STEWARDS: 10 SECOND TIME PENALTY FOR {c(11)} - LEAVING THE TRACK AND GAINING AN ADVANTAGE"})
        if at(400):
            car = self.cars[(14 + cyc) % len(self.cars)]
            await self._rc(sink, {"Category": "Flag", "Flag": "BLACK AND WHITE", "Scope": "Driver",
                                  "RacingNumber": car.num,
                                  "Message": f"BLACK AND WHITE FLAG FOR CAR {car.num} ({car.tla}) - MOVING UNDER BRAKING"})
        if at(420):
            await self._track(sink, "5")
            await self._rc(sink, {"Category": "Flag", "Flag": "RED", "Scope": "Track", "Message": "RED FLAG"})
            await self._rc(sink, {"Category": "Other", "Message": "PIT EXIT CLOSED"})
            await self._emit(sink, "SessionStatus", {"Status": "Aborted"})
        if at(470):
            await self._track(sink, "1")
            await self._emit(sink, "SessionStatus", {"Status": "Started"})
            await self._rc(sink, {"Category": "Flag", "Flag": "GREEN", "Scope": "Track",
                                  "Message": "GREEN LIGHT - PIT EXIT OPEN"})
        if at(500):
            await self._rc(sink, {"Category": "Other",
                                  "Message": f"FIA STEWARDS: DRIVE THROUGH PENALTY FOR {c(17)} - SPEEDING IN THE PIT LANE"})
        if at(540):
            await self._rc(sink, {"Category": "Other",
                                  "Message": f"FIA STEWARDS: PENALTY SERVED - DRIVE THROUGH PENALTY FOR {c(17)} - SPEEDING IN THE PIT LANE"})
        if at(560) and cyc == 0:
            await self._rc(sink, {"Category": "Other",
                                  "Message": f"FIA STEWARDS: {c(20)} DISQUALIFIED - TECHNICAL INFRINGEMENT (SIMULATED)"})
        if at(580):
            await self._rc(sink, {"Category": "Other",
                                  "Message": f"FIA STEWARDS: TURN 4 INCIDENT INVOLVING {c(6)} WILL BE INVESTIGATED AFTER THE RACE - UNSAFE REJOIN"})

    # ------------------------------------------------------------------
    def _physics(self, dt: float) -> None:
        ranked = sorted(self.cars, key=lambda c: -c.dist)
        for rank, car in enumerate(ranked):
            if car.retired:
                car.speed = 0
                continue
            if car.pit_state:
                self._pit_physics(car, dt)
                continue
            v = self._vtarget(max(car.dist, 0.0)) * car.pace * (1.0 + 0.004 * math.sin(self.sim_t / 7 + car.idx))
            if self.phase == "VSC":
                v *= 0.62
            elif self.phase == "SC":
                ahead = ranked[rank - 1] if rank > 0 else None
                v = min(v, 62.0 if ahead and ahead.dist - car.dist > 60 else 44.0)
            elif self.phase == "RED":
                v = min(v, 22.0)
            if self.sim_t < 3.0:
                v = 0.0                                   # standing start
            acc = max(-38.0, min(11.0, (v - car.speed) / dt))
            car.last_acc = acc
            car.speed = max(0.0, car.speed + acc * dt)
            car.dist += car.speed * dt

    def _pit_physics(self, car: Car, dt: float) -> None:
        if car.pit_state == "in":
            car.speed = 22.2                                # 80 km/h pit limiter
            car.dist += car.speed * dt
            car.pit_timer += car.speed * dt
            if car.pit_timer >= self.lane_len * 0.5:
                car.pit_state, car.pit_timer, car.speed = "stopped", 0.0, 0.0
        elif car.pit_state == "stopped":
            car.pit_timer += dt
            if car.pit_timer >= 2.6:
                car.pit_state, car.pit_timer = "out", self.lane_len * 0.5
                car.pit_stops += 1
                used = {s["Compound"] for s in car.stints}
                choices = [c for c in COMPOUNDS if c not in used] or COMPOUNDS
                car.stints.append({"Compound": self.rng.choice(choices), "New": "true",
                                   "TotalLaps": 0, "StartLaps": 0, "LapFlags": 0, "TyresNotChanged": "0"})
                car._new_stint = True            # type: ignore[attr-defined]
        elif car.pit_state == "out":
            car.speed = 22.2
            car.dist += car.speed * dt
            car.pit_timer += car.speed * dt
            if car.pit_timer >= self.lane_len:
                # rejoin the track at the pit exit, losing the time spent in the lane
                lap_base = math.floor(car.dist / self.length + 0.5) * self.length
                car.dist = lap_base + self.pit_exit
                car.pit_state = None
                car.pit_out_until = self.sim_t + 8

    def _pit_fraction(self, car: Car) -> float:
        return car.pit_timer / self.lane_len if self.lane_len else 0.0

    # ------------------------------------------------------------------
    async def _lap_events(self, sink: Sink) -> None:
        lines: dict[str, dict] = {}
        app: dict[str, dict] = {}
        for car in self.cars:
            if car.retired:
                continue
            upd: dict[str, Any] = {}
            laps_now = int(car.dist // self.length) if car.dist > 0 else 0
            # pit entry
            if (car.pit_state is None and car.pit_plan and laps_now + 1 >= car.pit_plan[0]
                    and self.phase != "RED" and (car.dist % self.length) >= self.pit_entry
                    and laps_now < self.total_laps - 1):
                car.pit_plan.pop(0)
                car.pit_state, car.pit_timer = "in", 0.0
                upd.update({"InPit": True, "PitOut": False})
            if getattr(car, "_new_stint", False):
                car._new_stint = False            # type: ignore[attr-defined]
                app[car.num] = {"Stints": {str(len(car.stints) - 1): car.stints[-1]}}
                upd["NumberOfPitStops"] = car.pit_stops
            if car.pit_state is None and car.pit_out_until > self.sim_t:
                upd.update({"InPit": False, "PitOut": True})
            elif car.pit_out_until > 0 and car.pit_out_until <= self.sim_t:
                car.pit_out_until = -1
                upd["PitOut"] = False
            # sector crossing
            within = car.dist - laps_now * self.length
            sector_now = min(2, int(within / (self.length / 3))) if car.dist > 0 else 0
            if laps_now > car.laps and car.dist > 0:
                sec_time = self.sim_t - car.sector_start_t
                lap_time = self.sim_t - car.lap_start_t
                upd.setdefault("Sectors", {})["2"] = self._sector_value(car, 2, sec_time)
                car.laps = laps_now
                car.stints[-1]["TotalLaps"] = car.stints[-1]["TotalLaps"] + 1
                app.setdefault(car.num, {}).setdefault("Stints", {})[str(len(car.stints) - 1)] = {
                    "TotalLaps": car.stints[-1]["TotalLaps"]}
                if car.laps > 1:              # first lap from standing start is not a flying lap
                    pb = car.best is None or lap_time < car.best
                    ob = pb and (self.overall_best is None or lap_time < self.overall_best)
                    upd["LastLapTime"] = {"Value": fmt_lap(lap_time), "PersonalFastest": pb, "OverallFastest": ob}
                    if pb:
                        car.best = lap_time
                        upd["BestLapTime"] = {"Value": fmt_lap(lap_time)}
                    if ob:
                        self.overall_best = lap_time
                else:
                    upd["LastLapTime"] = {"Value": fmt_lap(lap_time), "PersonalFastest": False,
                                          "OverallFastest": False}
                upd["NumberOfLaps"] = car.laps
                upd["Speeds"] = {"ST": {"Value": str(int(290 + self.rng.uniform(-6, 12)))}}
                car.lap_start_t = car.sector_start_t = self.sim_t
                car.sector = 0
                # new lap: clear sectors 1-2 of the previous lap
                upd["Sectors"].update({"0": {"Value": "", "PersonalFastest": False, "OverallFastest": False},
                                       "1": {"Value": "", "PersonalFastest": False, "OverallFastest": False}})
            elif sector_now > car.sector and car.dist > 0:
                sec_time = self.sim_t - car.sector_start_t
                upd.setdefault("Sectors", {})[str(car.sector)] = self._sector_value(car, car.sector, sec_time)
                if car.sector == 0:
                    upd["Sectors"]["2"] = {"Value": "", "PersonalFastest": False, "OverallFastest": False}
                car.sector = sector_now
                car.sector_start_t = self.sim_t
            if upd:
                lines[car.num] = upd
        if lines:
            await self._emit(sink, "TimingData", {"Lines": lines})
        if app:
            await self._emit(sink, "TimingAppData", {"Lines": app})
        lap = self._leader_lap()
        if lap != getattr(self, "_last_lap", None):
            self._last_lap = lap
            await self._emit(sink, "LapCount", {"CurrentLap": lap})

    def _sector_value(self, car: Car, s: int, t: float) -> dict:
        pb = car.best_sectors[s] is None or t < car.best_sectors[s]
        ob = pb and (self.overall_best_sec[s] is None or t < self.overall_best_sec[s])
        if pb:
            car.best_sectors[s] = t
        if ob:
            self.overall_best_sec[s] = t
        return {"Value": f"{t:.3f}", "PersonalFastest": pb, "OverallFastest": ob}

    async def _emit_gaps(self, sink: Sink) -> None:
        ranked = sorted(self.cars, key=lambda c: (c.retired, -c.dist))
        leader = ranked[0]
        ref_speed = self.length / 95.0
        lines = {}
        for pos, car in enumerate(ranked, 1):
            upd: dict[str, Any] = {"Position": str(pos), "Line": pos}
            if pos == 1:
                upd["GapToLeader"] = f"LAP {self._leader_lap()}"
                upd["IntervalToPositionAhead"] = {"Value": f"LAP {self._leader_lap()}", "Catching": False}
            else:
                ahead = ranked[pos - 2]
                lapped = int((leader.dist - car.dist) // self.length)
                gap = (leader.dist - car.dist) / ref_speed
                itv = (ahead.dist - car.dist) / ref_speed
                upd["GapToLeader"] = f"{lapped}L" if lapped >= 1 else f"+{gap:.3f}"
                upd["IntervalToPositionAhead"] = {
                    "Value": f"{int((ahead.dist - car.dist) // self.length)}L" if ahead.dist - car.dist >= self.length
                    else f"+{itv:.3f}", "Catching": itv < 1.0}
            lines[car.num] = upd
        await self._emit(sink, "TimingData", {"Lines": lines})

    # ------------------------------------------------------------------
    def _position_entry(self) -> dict:
        entries = {}
        for car in self.cars:
            if car.pit_state:
                x, y = self._pit_xy(self._pit_fraction(car))
            else:
                x, y, _, _ = self._point_heading(car.dist % self.length)
            entries[car.num] = {"Status": "OnTrack", "X": int(x), "Y": int(y), "Z": 100}
        return {"Timestamp": self._ts().isoformat().replace("+00:00", "Z"), "Entries": entries}

    def _cardata_entry(self) -> dict:
        cars = {}
        for car in self.cars:
            kmh = car.speed * 3.6
            gear = 0 if kmh < 1 else min(8, 1 + int(kmh / 42))
            lo = (gear - 1) * 42
            rpm = 0 if gear == 0 else int(9800 + min(1.0, max(0.0, (kmh - lo) / 42)) * 2200)
            throttle = 0 if car.last_acc < -2 else (100 if car.last_acc > 0.5 or kmh > 300 else 60)
            brake = 100 if car.last_acc < -3 else 0
            cars[car.num] = {"Channels": {"0": rpm, "2": int(kmh), "3": gear, "4": throttle, "5": brake, "45": 0}}
        return {"Utc": self._ts().isoformat().replace("+00:00", "Z"), "Cars": cars}
