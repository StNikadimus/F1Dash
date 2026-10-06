"""Automatic track + pit lane: the pit lane is loaded when the circuit is known (cache, else the
F1 archive of the last finished race there) - not discovered from the session's own pit stops;
live pit passes only validate it; the map is validated against the car positions; the circuit
identity comes from the session metadata only.

Real data: the four 2025 Azerbaijan GP pit stops and one lap of car 1
(tests/fixtures/openf1_baku2025_pit.txt, F1 Position.z coordinates). The archive download is
replaced by those events (the F1 archive is not reachable from the test environment).

Run:  python -m unittest tests.test_track_pitlane_auto
"""
import asyncio
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import baku_real as B  # noqa: E402
from server import pitlane_seed  # noqa: E402
from server.sources.replay import Event  # noqa: E402
from server.telemetry import encode_z, parse_utc  # noqa: E402
from server.track import TrackGeometry  # noqa: E402

TRACK, PASSES = B.load()


def archive_events() -> list:
    """The real Baku pit stops as an archived session would deliver them."""
    evs = []
    for p in PASSES:
        for t, x, y in p["samples"]:
            entry = {"Timestamp": B.iso(t), "Entries": {p["num"]: {"Status": "OnTrack", "X": x, "Y": y, "Z": 0}}}
            evs.append(Event(parse_utc(B.iso(t)), "Position.z", encode_z({"Position": [entry]})))
        t_out = parse_utc(B.iso(p["t_out"]))
        evs.append(Event(t_out, "TimingDataF1", {"Lines": {p["num"]: {"PitOut": True}}}))
        evs.append(Event(t_out, "PitLaneTimeCollection", {"PitTimes": {p["num"]: {"Duration": str(p["lane_s"])}}}))
    evs.sort(key=lambda e: e.t)
    return evs


def fake_index(year):
    return {"Meetings": [
        {"Name": "Azerbaijan Grand Prix", "Circuit": {"Key": 144, "ShortName": "Baku"}, "Sessions": [
            {"Name": "Practice 1", "Type": "Practice", "Path": f"{year}/baku/fp1/",
             "EndDate": f"{year}-09-19T14:30:00", "GmtOffset": "04:00:00"},
            {"Name": "Race", "Type": "Race", "Path": f"{year}/baku/race/",
             "EndDate": f"{year}-09-21T17:00:00", "GmtOffset": "04:00:00"}]},
        {"Name": "Other", "Circuit": {"Key": 63}, "Sessions": [
            {"Name": "Race", "Type": "Race", "Path": f"{year}/other/race/", "EndDate": f"{year}-04-01T18:00:00",
             "GmtOffset": "03:00:00"}]}]}


class Patched:
    """Archive index + download replaced by the real Baku events (records what was read)."""

    def __enter__(self):
        self.read = []

        async def index(http, year):
            if year > 2025:
                raise RuntimeError("HTTP 404")
            return fake_index(year)

        async def loader(path, cache_dir, topics):
            self.read.append(path)
            return archive_events() if path == "2025/baku/race/" else []
        self._orig = (pitlane_seed._index, pitlane_seed.seed_pitlane.__kwdefaults__)
        pitlane_seed._index = index
        self.loader = loader
        orig_seed = pitlane_seed.seed_pitlane

        async def seed(*a, **k):
            k.setdefault("loader", loader)
            return await orig_seed(*a, **k)
        self._seed = orig_seed
        pitlane_seed.seed_pitlane = seed
        import server.engine as E
        self._eng_seed = E.seed_pitlane
        E.seed_pitlane = seed
        return self

    def __exit__(self, *exc):
        import server.engine as E
        pitlane_seed._index = self._orig[0]
        pitlane_seed.seed_pitlane = self._seed
        E.seed_pitlane = self._eng_seed


def engine():
    from test_live import LiveClock, make_engine
    src = LiveClock()
    src.t = datetime(2026, 9, 18, 9, tzinfo=timezone.utc)
    eng = make_engine(src)
    eng._track_id = (144, 2026)
    eng.geometry = TrackGeometry(144, "Baku", 2026, "multiviewer", [list(p) for p in TRACK])
    return eng


class SeedTest(unittest.TestCase):
    def test_sessions_ranked_races_first_running_session_excluded(self):
        now = datetime(2026, 9, 25, tzinfo=timezone.utc)
        c = pitlane_seed.candidate_sessions({2025: fake_index(2025), 2026: fake_index(2026)}, 144,
                                            exclude_path="2026/baku/race/", now=now)
        self.assertEqual([x["path"] for x in c], ["2025/baku/race/", "2026/baku/fp1/", "2025/baku/fp1/"])
        self.assertTrue(all("other" not in x["path"] for x in c))          # another circuit: never

    def test_pit_lane_loaded_at_circuit_identification_without_any_live_pit_stop(self):
        async def go():
            with Patched() as P:
                eng = engine()
                pit_before = eng.geometry.pitlane
                eng._pit_seed_maybe(144, 2026, "Baku", eng.geometry)
                await eng._seed_task
                return pit_before, eng.geometry, P.read, eng.map_report()
        before, geo, read, rep = asyncio.run(go())
        self.assertIsNone(before)
        self.assertIsNotNone(geo.pitlane)                                 # drawn - no pit stop of this session
        self.assertEqual(read, ["2025/baku/race/"])                       # the last finished race there
        self.assertIn("F1 archive", geo.pitlane_info.get("source") or "")
        self.assertIn(geo.pitlane_info.get("confidence"), ("HIGH", "MEDIUM"))
        self.assertTrue(rep["pit_lane"]["ok"])

    def test_cached_verified_pit_lane_needs_no_download(self):
        async def go():
            with Patched() as P:
                eng = engine()
                eng._pit_seed_maybe(144, 2026, "Baku", eng.geometry)
                await eng._seed_task
                P.read.clear()
                eng2 = engine()
                eng2.tracks = eng.tracks                                  # same cache
                eng2.tracks.attach_pitlane(eng2.geometry, 2026)
                eng2._pit_seed_maybe(144, 2026, "Baku", eng2.geometry)
                return eng2.geometry.pitlane is not None, P.read, eng2._seed_task
        drawn, read, task = asyncio.run(go())
        self.assertTrue(drawn)
        self.assertEqual(read, [])
        self.assertIsNone(task)

    def test_unavailable_is_reported_not_invented(self):
        async def go():
            with Patched():
                eng = engine()
                eng._track_id = (999, 2026)
                eng.geometry = TrackGeometry(999, "Nowhere", 2026, "multiviewer", [list(p) for p in TRACK])
                eng._pit_seed_maybe(999, 2026, "Nowhere", eng.geometry)
                await eng._seed_task
                return eng.geometry, eng.map_report()
        geo, rep = asyncio.run(go())
        self.assertIsNone(geo.pitlane)
        self.assertFalse(rep["pit_lane"]["ok"])
        self.assertIn("NOT AVAILABLE", rep["pit_lane"]["detail"])
        self.assertEqual(geo.pitlane_info["state"], "unavailable")

    def test_live_pass_validates_but_never_replaces_a_verified_pit_lane(self):
        from server.pitlane import PitLaneCollector

        async def go():
            with Patched():
                eng = engine()
                eng._pit_seed_maybe(144, 2026, "Baku", eng.geometry)
                await eng._seed_task
                before = [list(p) for p in eng.geometry.pitlane]
                stored = eng.tracks.pitcache.read(144)
                # one real pass of this "session" (car 1) arrives live
                col = PitLaneCollector(TRACK, keep_ms=None)
                p = PASSES[0]
                for t, x, y in p["samples"]:
                    col.hist.add(p["num"], t, x, y)
                col.timing("TimingDataF1", {"Lines": {p["num"]: {"PitOut": True}}}, p["t_out"])
                col.timing("PitLaneTimeCollection", {"PitTimes": {p["num"]: {"Duration": str(p["lane_s"])}}}, p["t_out"])
                new = col.poll(float("inf"))
                eng.pit_collector = col
                eng._pit_ctx = {"key": 144, "year": 2026, "name": "Baku", "cached": None, "prior": None}
                await eng._pit_live_apply(new)
                return before, [list(p) for p in eng.geometry.pitlane], stored, eng.tracks.pitcache.read(144), \
                    eng._pit_validation
        before, after, stored, now, val = asyncio.run(go())
        self.assertEqual(before, after)
        self.assertEqual(stored, now)                                     # cache untouched
        self.assertEqual((val["passes"], val["agree"]), (1, 1))           # the live pass matches it


class MapValidationTest(unittest.TestCase):
    def test_positions_mapped_and_off_track_reported(self):
        async def go():
            eng = engine()
            t = eng.source.t
            ents = {"1": {"Status": "OnTrack", "X": TRACK[10][0], "Y": TRACK[10][1], "Z": 0},
                    "44": {"Status": "OnTrack", "X": TRACK[100][0] + 3000, "Y": TRACK[100][1], "Z": 0}}
            await eng.feed("DriverList", {"1": {"Tla": "VER"}, "44": {"Tla": "HAM"}}, t)
            await eng.feed("Position.z", encode_z({"Position": [{"Timestamp": t.isoformat(), "Entries": ents}]}), t)
            eng.tick()
            return eng.map_report()
        rep = asyncio.run(go())
        pm = rep["positions"]
        self.assertEqual((pm["mapped"], pm["with_position"]), (1, 2))
        self.assertFalse(pm["ok"])
        self.assertIn("#44", pm["detail"])                                # 300 m off: reported, not hidden

    def test_no_circuit_guessing_from_positions(self):
        async def go():
            eng = engine()
            eng.geometry = None                                           # nothing loaded, no name in the session
            for x, y in TRACK * 4:
                eng._learn({"t": 0, "cars": [["1", x, y, 1]]})
            eng._ref_poll(1000.0)
            return eng._ref_task, eng.circuit_identity()
        task, ident = asyncio.run(go())
        self.assertIsNone(task)                                           # never "this looks like Baku"
        self.assertIsNone(ident["layout"])


if __name__ == "__main__":
    unittest.main()
