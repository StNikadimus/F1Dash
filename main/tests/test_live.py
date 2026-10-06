"""LIVE readiness: the live pipeline (Engine in "live" mode, messages arriving one by one on the
wall clock) must give the same state as a recording at the same absolute F1 time.

Simulator only - no actual live feed is used here (the F1 live-timing endpoints are not
reachable from the test environment). The data are real: the official live-timing recordings
of the 2026 Japanese GP qualifying / FP3 (tests/fixtures/suzuka-2026-*.json.gz, in the order
they were received) and OpenF1 race control of the 2024 Sao Paulo GP.

    STATE(live, T) == STATE(recording, T)      for every T

* messages are delivered only once the simulated wall clock reaches them (nothing from T+1 s,
  T+5 s, T+30 s is ever available), incl. the real out-of-order deliveries of the feed;
* reconnect: 3 s gap, then the subscribe snapshot;
* a wrong PC clock (+-30 s) and network latency;
* late (older) clock / track status posts after a red flag;
* race control (red flag / SC / VSC / restart / penalties / deletions) message by message;
* the 2026 Japanese GP race (data/recordings/sample-2026-japan-race.json.gz, when present).

Run:  python -m unittest tests.test_live
"""
import asyncio
import gzip
import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from server.config import load_config  # noqa: E402
from server.engine import Engine  # noqa: E402
from server.feedstate import FeedState  # noqa: E402
from server.hub import Hub  # noqa: E402
from server.models import Availability  # noqa: E402
from server.normalizer import Normalizer  # noqa: E402
from server.sources.base import Source  # noqa: E402
from server.telemetry import parse_utc  # noqa: E402
from server.track import TrackProvider  # noqa: E402
from test_replay_state import FP3, QUALI, RACE, SP, Replay, at, rc_feed  # noqa: E402

DAY = "2026-03-28"
TOPICS = set(load_config("/nonexistent.toml")["live"]["topics"])


class LiveClock(Source):
    """The live source as the engine sees it: mode "live", wall clock = what the test says
    (+ an optional error of this computer's clock)."""
    mode = "live"

    def __init__(self, skew_s: float = 0.0) -> None:
        self.t: datetime = None
        self.skew = timedelta(seconds=skew_s)

    def now(self) -> datetime:
        return self.t + self.skew

    async def run(self, sink) -> None:
        pass


def make_engine(src: Source, **cfg_over) -> Engine:
    d = tempfile.mkdtemp()
    cfg = load_config(Path(d) / "none.toml")
    for k, v in cfg_over.items():
        sec, key = k.split("__")
        cfg[sec][key] = v
    eng = Engine(cfg, src, TrackProvider(Path(d), cfg["tracks"]), Hub())

    async def no_track(*_a, **_k):
        return None
    eng.tracks.load = no_track                        # no network
    return eng


def arrivals(path: Path) -> list:
    """(arrival, F1 timestamp, topic, data) in the order the live feed delivered them; arrival =
    the newest timestamp seen so far (a message stamped earlier than that came late)."""
    out, newest = [], None
    with gzip.open(path, "rt", encoding="utf-8") as fh:
        records = json.load(fh)
    for r in records:
        t = parse_utc(r.get("timestamp"))
        if t is None or not isinstance(r.get("updates"), dict):
            continue
        first = newest is None
        newest = t if newest is None or t > newest else newest
        for topic, data in r["updates"].items():
            if topic in TOPICS:
                # the recording starts with the complete state: what the subscribe result is live
                out.append((newest, t, topic, data, first))
    return out


def comparable(st: dict, clock: bool = True) -> dict:
    st = dict(st)
    for k in ("availability", "timeline", "map"):   # diagnostics / VOD sync structure / map extras
        st.pop(k, None)
    s = dict(st["session"])
    s.pop("phase_duration_ms", None)
    if not clock:
        for k in ("clock", "now_ms"):
            s.pop(k, None)
    st["session"] = s
    return st


def diff(a, b, path=""):
    if isinstance(a, dict) and isinstance(b, dict):
        return [x for k in sorted(set(a) | set(b), key=str) for x in diff(a.get(k), b.get(k), f"{path}.{k}")]
    if isinstance(a, list) and isinstance(b, list) and len(a) == len(b):
        return [x for i, (p, q) in enumerate(zip(a, b)) for x in diff(p, q, f"{path}[{i}]")]
    return [] if a == b else [(path, a, b)]


def reference(events: list, t: datetime) -> dict:
    """What a recording shows at F1 time t: the given messages, in F1 time order (the
    subscribe snapshot as a snapshot)."""
    fs = FeedState()
    for _arr, ts, topic, data, snap in sorted(events, key=lambda e: e[1]):
        fs.apply(topic, data, snap, ts.timestamp() * 1000)
    return comparable(Normalizer().build(fs, t, 1.0, Availability(), rc_until=t))


class Sim:
    """Drives an Engine like F1LiveSource does: feed() on arrival, tick() every step."""

    def __init__(self, path: Path, skew_s: float = 0.0, latency_s: float = 0.0, startup_snapshot: bool = True,
                 **cfg) -> None:
        self.src = LiveClock(skew_s)
        self.eng = make_engine(self.src, **cfg)
        self.events = arrivals(path)
        if not startup_snapshot:
            self.events = [e[:4] + (False,) for e in self.events]
        self.lat = timedelta(seconds=latency_s)
        self.i = 0
        self.delivered: list = []

    async def run_to(self, t: datetime, drop=None) -> None:
        while self.i < len(self.events) and self.events[self.i][0] + self.lat <= t:
            ev = self.events[self.i]
            self.i += 1
            if drop and drop(ev):
                continue                                  # lost (disconnected)
            self.src.t = ev[0] + self.lat
            if ev[4]:
                await self.snapshot([e for e in self.events if e[4]])
                self.i = sum(1 for e in self.events if e[4])
                continue
            await self.eng.feed(ev[2], ev[3], ev[1])
            self.eng.tick()
            self.delivered.append(ev)
        self.src.t = t
        self.eng.tick()

    async def snapshot(self, topics: list) -> None:
        """The subscribe result, as F1LiveSource hands it over (Heartbeat Utc as hint)."""
        hb = next((parse_utc(e[3].get("Utc")) for e in topics if e[2] == "Heartbeat"), None)
        await self.eng.begin_snapshot()
        for e in sorted(topics, key=lambda e: e[2] != "SessionInfo"):
            await self.eng.feed(e[2], e[3], hb, snapshot=True)
        label = next(ev.event_ms for ev in self.eng.timeline.events if ev.snapshot and ev.kind == "topic")
        lab = datetime.fromtimestamp(label / 1000, timezone.utc)
        self.delivered += [(e[0], lab, e[2], e[3], True) for e in topics]
        self.eng.tick()

    def state(self, clock: bool = True) -> dict:
        return comparable(self.eng.snapshot(), clock)


def moments(start: str, end: str, step_s: float):
    t, stop = at(DAY, start), at(DAY, end)
    while t <= stop:
        yield t
        t += timedelta(seconds=step_s)


# ---------------------------------------------------------------------------
class LiveEqualsRecordingTest(unittest.TestCase):
    """Leaderboard, laps, best laps, sectors, current lap / sector, OUT / PREP / HOT / COOL,
    phase (Q1 / Q2 / Q3), clock, deletions, pit / garage, race control - live == recording."""

    def check(self, path: Path, start: str, end: str, step: float, **sim) -> int:
        async def go():
            s = Sim(path, **sim)
            bad = []
            for t in moments(start, end, step):
                await s.run_to(t + s.lat)
                # reference: exactly the messages received by then, in F1 time order
                d = diff(s.state(), reference(s.delivered, t))
                if d:
                    bad.append((t, d[:3]))
            return bad, len(s.delivered)
        bad, n = asyncio.run(go())
        self.assertEqual(bad, [])
        return n

    def test_qualifying_q1_q2_q3_in_arrival_order(self):
        # (from 06:01: the live F1 time needs a few messages to measure the feed latency)
        n = self.check(QUALI, "06:01:00", "07:03:30", 20)
        self.assertGreater(n, 10000)

    def test_practice_in_arrival_order(self):
        self.check(FP3, "02:38:15", "05:34:40", 60)

    @unittest.skipUnless(RACE.exists(), "data/recordings/sample-2026-japan-race.json.gz not present")
    def test_race_in_arrival_order(self):
        """Positions, gaps, intervals, laps, tyres, pit stops, DNF (BEA, STR), garage, finish."""
        async def go():
            s = Sim(RACE)
            bad, seen = [], set()
            t = at("2026-03-29", "04:55:00")
            while t <= at("2026-03-29", "06:46:00"):
                await s.run_to(t)
                live = s.state()
                d = diff(live, reference(s.delivered, t))
                if d:
                    bad.append((t, d[:3]))
                seen |= {(k, x["tla"]) for x in live["drivers"].values() for k in ("dnf", "in_garage") if x[k]}
                t += timedelta(seconds=333)
            return bad, seen
        bad, seen = asyncio.run(go())
        self.assertEqual(bad, [])
        self.assertTrue({("dnf", "STR"), ("in_garage", "STR")} <= seen)

    def test_wrong_pc_clock_and_latency(self):
        """F1 time comes from the feed timestamps: a PC clock 30 s off (either way) and 0.3 s
        latency change nothing - not the clock, not the lap / pit / DNF timers."""
        for skew in (30.0, -30.0):
            self.check(QUALI, "06:15:00", "06:27:00", 30, skew_s=skew, latency_s=0.3, startup_snapshot=False)

    def test_first_snapshot_with_a_pc_clock_behind(self):
        """First connect, nothing measured yet, PC clock 30 s behind: the snapshot is never
        dated before F1's own last heartbeat in it."""
        async def go():
            s = Sim(QUALI, skew_s=-30.0)
            await s.run_to(at(DAY, "06:00:30"))
            return [e for e in s.delivered if e[4]][0][1], [e for e in s.delivered if e[4] and e[2] == "Heartbeat"]
        label, hb = asyncio.run(go())
        self.assertTrue(hb)
        self.assertGreaterEqual(label, parse_utc(hb[0][3]["Utc"]))

    def test_same_as_the_vod_seek_path(self):
        """The live state at T equals the VOD state after seeking to T (Q3 -> Q1 -> Q2 -> Q3)."""
        rep = Replay(QUALI, DAY)
        vod = rep.vod()

        async def go():
            s = Sim(QUALI)
            out = {}
            for hms in ("06:12:00", "06:31:00", "06:52:30"):
                await s.run_to(at(DAY, hms))
                out[hms] = s.state()
            return out
        live = asyncio.run(go())
        for hms in ("06:52:30", "06:12:00", "06:31:00", "06:52:30"):
            self.assertEqual(diff(live[hms], comparable(vod(hms))), [], hms)

    def test_lap_series_is_subscribed(self):
        """LapSeries (2nd line-crossing signal of the lap tracker, present in every recording):
        without it 535 of 544 live moments of this qualifying showed another current lap /
        sector / lap state than the recording."""
        from server.sources.f1_live import F1LiveSource
        self.assertIn("LapSeries", TOPICS)
        src = F1LiveSource({"topics": ["TimingData", "SessionInfo"]})       # an old config.toml
        self.assertIn("LapSeries", src.topics)


class NoFutureDataTest(unittest.TestCase):
    def test_nothing_after_t_is_shown(self):
        """At T only messages stamped <= T exist: the board at T differs from T+1 / T+3 / T+30
        exactly as a recording does - Q1 / Q2 / Q3, a Q3 lap and its deletion."""
        async def go():
            s = Sim(QUALI)
            res = {}
            for hms in ("06:10:00", "06:30:00", "06:51:10", "06:51:11", "06:51:13", "06:51:40"):
                await s.run_to(at(DAY, hms))
                res[hms] = (s.state(), list(s.delivered))
            return res
        out = asyncio.run(go())
        st = {}
        for hms, (live, delivered) in out.items():
            self.assertTrue(all(e[1] <= at(DAY, hms) for e in delivered), hms)     # nothing stamped after T
            self.assertEqual(diff(live, reference(delivered, at(DAY, hms))), [], hms)
            st[hms] = live
        lin = lambda s: next(d for d in s["drivers"].values() if d["tla"] == "LIN")   # noqa: E731
        # LIN 1:31.537 set at 06:51:10.382, deleted by race control at 06:51:12.279
        self.assertNotEqual(lin(st["06:51:10"])["best_lap"]["value"], "1:31.537")
        self.assertEqual(lin(st["06:51:11"])["best_lap"]["value"], "1:31.537")
        self.assertNotEqual(lin(st["06:51:13"])["best_lap"]["value"], "1:31.537")
        self.assertEqual([st[h]["session"]["phase"] for h in ("06:10:00", "06:30:00", "06:51:10")], ["Q1", "Q2", "Q3"])


class ReconnectTest(unittest.TestCase):
    """LIVE -> connection lost -> 3 s gap (its messages are lost) -> reconnect with the subscribe
    snapshot. Compared with the same live session without the disconnect."""

    T_DISC, GAP = "06:31:00", 3.0

    def run_reconnect(self, end: str, step: float, delay: float = 0.0):
        tdisc = at(DAY, self.T_DISC)
        trec = tdisc + timedelta(seconds=self.GAP)
        cfg = {}
        if delay:
            cfg = {"sync__mode": "DELAY", "sync__broadcast_delay_seconds": delay, "source__delay_seconds": delay}

        async def go():
            s, ok = Sim(QUALI, **cfg), Sim(QUALI, **cfg)          # with / without the disconnect
            lost = lambda ev: tdisc < ev[0] <= trec              # noqa: E731
            out, rec = [], False
            for t in moments("06:29:00", end, step):
                if t >= trec and not rec:
                    rec = True
                    await s.run_to(trec, drop=lost)
                    # the subscribe result: F1's complete state at the reconnect (live edge)
                    fs = FeedState()
                    for e in sorted((e for e in ok.events if e[0] <= trec), key=lambda e: e[1]):
                        fs.apply(e[2], e[3], e[4], e[1].timestamp() * 1000)
                    await s.snapshot([(trec, trec, k, v, True) for k, v in fs.topics.items()
                                      if not k.startswith("_")])
                await s.run_to(t, drop=lost)
                await ok.run_to(t)
                out.append((t, s.state(clock=False), ok.state(clock=False)))
            return out
        return asyncio.run(go()), tdisc, trec

    def test_state_survives_the_gap(self):
        res, tdisc, trec = self.run_reconnect("06:34:30", 2)
        gap_cars = {"1"}            # the only car that crossed the line inside the gap
        for t, live, ref in res:
            d = diff(live, ref)
            if t < tdisc:
                self.assertEqual(d, [], t)
                continue
            if t < trec:
                continue                                   # disconnected: the last state stays
            # session not reset; knocked-out part, pit / garage, lap history kept: only the car
            # whose line crossing was lost in the gap differs (its lap start is UNKNOWN)
            self.assertEqual({p.split(".")[2] for p, *_ in d if p.startswith(".drivers.")} - gap_cars, set(), t)
            self.assertEqual([p for p, *_ in d if not p.startswith(".drivers.")], [], t)
            for p, a, b in d:
                self.assertNotIn(p.split(".")[-1], ("best_lap", "position", "in_pit", "in_garage", "out_phase"), t)
        last = res[-1][1]
        self.assertEqual(len(last["drivers"]), 22)
        self.assertEqual(last["drivers"]["87"]["out_phase"], "Q1")      # BEA - was "Q2" before the fix

    def test_delayed_board_never_sees_the_snapshot_early(self):
        """A delayed (video-synced) board: the snapshot must not appear before its content
        happened. Car 1's PersonalFastest S3 (06:31:02.211) was shown at 06:31:01.5 before."""
        res, tdisc, trec = self.run_reconnect("06:31:20", 0.25, delay=10.0)
        for t, live, _ref in res:
            shown = t - timedelta(seconds=10)
            if tdisc <= shown < trec:
                self.assertFalse(live["drivers"]["1"]["sectors"][2]["personal_best"], shown)


class LatePacketTest(unittest.TestCase):
    """An older post delivered after a newer one never undoes it (applied in F1 time order)."""

    def test_late_clock_and_track_status_after_red_flag(self):
        async def go():
            src = LiveClock()
            eng = make_engine(src)
            t0 = datetime(2026, 10, 2, 13, 0, tzinfo=timezone.utc)
            iso = lambda t: t.isoformat().replace("+00:00", "Z")          # noqa: E731

            async def send(arrive, ts, topic, data):
                src.t = arrive
                await eng.feed(topic, data, ts)
                eng.tick()
            await send(t0, t0, "SessionStatus", {"Status": "Started"})
            await send(t0, t0, "ExtrapolatedClock", {"Utc": iso(t0), "Remaining": "00:20:00", "Extrapolating": True})
            await send(t0, t0, "TrackStatus", {"Status": "1", "Message": "AllClear"})
            red = t0 + timedelta(seconds=60)
            await send(red, red, "TrackStatus", {"Status": "5", "Message": "Red"})
            await send(red, red, "ExtrapolatedClock", {"Utc": iso(red), "Remaining": "00:19:00", "Extrapolating": False})
            old = t0 + timedelta(seconds=55)                    # stamped before the red flag ...
            await send(red + timedelta(seconds=1), old, "ExtrapolatedClock",
                       {"Utc": iso(old), "Remaining": "00:19:05", "Extrapolating": True})
            await send(red + timedelta(seconds=1), old, "TrackStatus", {"Status": "1", "Message": "AllClear"})
            src.t = red + timedelta(seconds=40)
            eng.tick()
            return eng.snapshot()
        st = asyncio.run(go())
        self.assertEqual(st["session"]["clock"], {"remaining_ms": 1140000, "running": False, "speed": 1.0})
        self.assertEqual((st["track_status"]["status"], st["session"]["state"], st["session"]["red_flag"]),
                         ("RED", "SUSPENDED", True))

    def test_clock_counts_down_and_never_below_zero(self):
        async def go():
            src = LiveClock()
            eng = make_engine(src)
            t0 = datetime(2026, 10, 2, 13, 0, tzinfo=timezone.utc)
            src.t = t0
            await eng.feed("SessionStatus", {"Status": "Started"}, t0)
            await eng.feed("ExtrapolatedClock", {"Utc": t0.isoformat(), "Remaining": "00:00:30",
                                                 "Extrapolating": True}, t0)
            out = []
            for s in (0, 10, 29, 31, 120):
                src.t = t0 + timedelta(seconds=s)
                eng.tick()
                out.append(eng.snapshot()["session"]["clock"]["remaining_ms"])
            return out
        self.assertEqual(asyncio.run(go()), [30000, 20000, 1000, 0, 0])


class LiveRaceControlTest(unittest.TestCase):
    """2024 Sao Paulo (real OpenF1 race control, no TrackStatus / SessionStatus: race control is
    the fallback): message by message over the live path == the recording at the same time."""

    def check(self, records: list, step_s: float) -> None:
        msgs = rc_feed(records).get("RaceControlMessages")["Messages"]

        async def go():
            src = LiveClock()
            eng = make_engine(src)
            first = parse_utc(msgs[0]["Utc"])
            last = parse_utc(msgs[-1]["Utc"])
            t, i, bad = first - timedelta(seconds=5), 0, []
            while t <= last + timedelta(seconds=30):
                while i < len(msgs) and parse_utc(msgs[i]["Utc"]) <= t:
                    ts = parse_utc(msgs[i]["Utc"])
                    src.t = ts
                    # first message as F1 sends it (a list), the others as indexed updates
                    data = {"Messages": [msgs[0]]} if i == 0 else {"Messages": {str(i): msgs[i]}}
                    await eng.feed("RaceControlMessages", data, ts)
                    eng.tick()
                    i += 1
                src.t = t
                eng.tick()
                live = eng.snapshot()
                fs = FeedState()                     # the messages received so far
                if i:
                    fs.apply("RaceControlMessages", {"Messages": msgs[:i]}, True, 0)
                ref = Normalizer().build(fs, t, 1.0, Availability(), rc_until=t)
                for k in ("track_status", "race_control"):
                    if live[k] != ref[k]:
                        bad.append((t, k))
                for k in ("state", "red_flag", "rc_coverage"):
                    if live["session"][k] != ref["session"][k]:
                        bad.append((t, k, live["session"][k], ref["session"][k]))
                t += timedelta(seconds=step_s)
            return bad
        self.assertEqual(asyncio.run(go()), [])

    def test_qualifying_red_flags_and_deletions(self):
        self.check(SP["qualifying_9627"], 20)

    def test_race_vsc_sc_red_restart_finish(self):
        self.check(SP["race_9636"], 20)


class ConnectionStateTest(unittest.TestCase):
    def test_silent_socket_is_delayed_then_live_again(self):
        async def go():
            src = LiveClock()
            eng = make_engine(src)
            src.t = datetime(2026, 10, 2, 13, 0, tzinfo=timezone.utc)
            eng.set_status(state="connected", transport="core", attempt=0, detail="Connected")
            states = [eng._status["state"]]
            base = eng._last_rx
            eng._check_feed_age(base + 10)
            states.append(eng._status["state"])
            eng._check_feed_age(base + 30)
            states.append((eng._status["state"], eng._status.get("feed_age_s"), eng.hub.status.get("state")))
            await eng.feed("Heartbeat", {"Utc": "2026-10-02T13:00:30Z"}, src.t)
            eng._check_feed_age(eng._last_rx + 1)
            states.append((eng._status["state"], eng.hub.status.get("state")))
            return states
        self.assertEqual(asyncio.run(go()), ["connected", "connected", ("stale", 30, "stale"),
                                             ("connected", "connected")])

    def test_snapshot_applies_session_info_first(self):
        """After a reconnect into another session, the session change must not wipe the
        snapshot topics that came before SessionInfo."""
        from server.sources.f1_live import F1LiveSource

        class Rec:
            def __init__(self):
                self.topics = []

            async def begin_snapshot(self):
                pass

            async def feed(self, topic, data, ts, snapshot=False, origin="feed"):
                self.topics.append(topic)
        rec = Rec()
        asyncio.run(F1LiveSource({"topics": []})._handle_snapshot(
            rec, {"TimingData": {}, "DriverList": {}, "SessionInfo": {}, "Heartbeat": {}}))
        self.assertEqual(rec.topics[0], "SessionInfo")
        self.assertEqual(sorted(rec.topics), ["DriverList", "Heartbeat", "SessionInfo", "TimingData"])


if __name__ == "__main__":
    unittest.main()
