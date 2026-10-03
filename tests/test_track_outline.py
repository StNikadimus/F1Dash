"""Track outline: known layout fitted onto real car positions, validation of learned outlines,
and "track map wrong" (rebuild the outline of one circuit - the pit lane is never touched).

Real data: tests/fixtures/openf1_baku2025_pit.txt - one racing lap of car 1, 2025 Azerbaijan GP
(F1 Position.z coordinates via OpenF1). Reference layouts: server/reference_tracks (bacinger).

Run:  python -m unittest tests.test_track_outline
"""
import asyncio
import json
import math
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from server.track import TrackProvider, outline_problem  # noqa: E402
from server.track_match import fit_reference, reference_id, reference_points  # noqa: E402

FIX = Path(__file__).resolve().parent / "fixtures" / "openf1_baku2025_pit.txt"


def baku_lap() -> list:
    pts, sec = [], None
    for line in FIX.read_text().splitlines():
        line = line.strip()
        if line.startswith("["):
            sec = line
            continue
        if line and not line.startswith("#") and sec == "[track]":
            _t, x, y = line.split(",")
            pts.append((float(x), float(y)))
    return pts


class ReferenceFitTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.lap = baku_lap()
        cls.fit = fit_reference(reference_points("az-2016"), cls.lap * 3)

    def test_names(self):
        self.assertEqual(reference_id("Sakhir", "Sakhir", "Bahrain Grand Prix"), "bh-2002")
        self.assertEqual(reference_id("Baku"), "az-2016")
        self.assertEqual(reference_id("Suzuka"), "jp-1962")
        self.assertIsNone(reference_id("Nowhere Park"))                 # never guessed

    def test_known_layout_fits_real_positions(self):
        f = self.fit
        self.assertTrue(f.ok, f.reason)
        self.assertLess(f.median_m, 5.0)
        self.assertLess(f.p90_m, 10.0)
        self.assertGreater(f.coverage, 0.95)
        self.assertAlmostEqual(f.scale, 1.0, delta=0.03)                # F1 units are decimetres
        # every real position lies near the fitted outline - the whole lap, nothing missing
        pts = f.points
        far = [p for p in self.lap if min(math.dist(p, q) for q in pts) > 150]
        self.assertEqual(far, [])

    def test_other_layouts_are_rejected(self):
        for rid in ("jp-1962", "mc-1929", "it-1922"):
            f = fit_reference(reference_points(rid), self.lap * 3)
            self.assertFalse(f.ok, rid)

    def test_half_a_lap_is_not_enough(self):
        half = self.lap[: len(self.lap) // 2]
        f = fit_reference(reference_points("az-2016"), half * 6)
        self.assertFalse(f.ok)
        self.assertFalse(fit_reference(reference_points("az-2016"), self.lap[:50]).ok)   # too few


class LearnedOutlineTest(unittest.TestCase):
    def test_full_lap_ok_half_lap_or_gap_rejected(self):
        lap = baku_lap()
        dense = []
        for a, b in zip(lap, lap[1:] + lap[:1]):         # ~10 m spacing like the learner records
            n = max(1, int(math.dist(a, b) // 100))
            dense += [(a[0] + (b[0] - a[0]) * k / n, a[1] + (b[1] - a[1]) * k / n) for k in range(n)]
        dense.append(dense[0])
        self.assertIsNone(outline_problem(dense))
        self.assertIsNotNone(outline_problem(dense[: len(dense) // 2]))                   # half the track
        holed = dense[:200] + dense[400:]
        self.assertIn("gap", outline_problem(holed))


class TrackReportTest(unittest.TestCase):
    def test_reset_deletes_the_outline_only_never_the_pit_lane(self):
        with tempfile.TemporaryDirectory() as d:
            tp = TrackProvider(Path(d), {"api_url": ""})
            t = Path(d) / "tracks"
            for name in ("mv_63_2026.json", "mv_63_2025.json", "ref_63.json", "learned_63.json",
                         "pitlane_geometry_63.json", "mv_46_2026.json"):
                (t / name).write_text("{}")
            removed = tp.reset_circuit(63, "multiviewer")
            self.assertEqual(sorted(removed), ["learned_63.json", "mv_63_2025.json", "mv_63_2026.json", "ref_63.json"])
            self.assertTrue((t / "pitlane_geometry_63.json").exists())          # the pit lane stays
            self.assertTrue((t / "mv_46_2026.json").exists())                   # other circuits stay
            self.assertEqual(tp.rejected(63), {"multiviewer"})
            tp.reset_circuit(63, "reference")
            self.assertEqual(tp.rejected(63), {"multiviewer", "reference"})
            tp.reset_circuit(63, "learned")                                     # all rejected: start over
            self.assertEqual(tp.rejected(63), {"learned"})

    def test_rejected_multiviewer_is_skipped_reference_used(self):
        with tempfile.TemporaryDirectory() as d:
            tp = TrackProvider(Path(d), {"api_url": ""})
            t = Path(d) / "tracks"
            mv = {"x": [0, 100, 100, 0], "y": [0, 0, 100, 100], "circuitName": "MV"}
            (t / "mv_63_2026.json").write_text(json.dumps(mv))
            geo = asyncio.run(tp.load(63, 2026, "Sakhir"))
            self.assertEqual(geo.source, "multiviewer")
            tp.reset_circuit(63, "multiviewer")
            (t / "mv_63_2026.json").write_text(json.dumps(mv))                 # e.g. downloaded again
            (t / "ref_63.json").write_text(json.dumps({"name": "Sakhir", "ref_id": "bh-2002", "points": [[1, 2], [3, 4]]}))
            geo = asyncio.run(tp.load(63, 2026, "Sakhir"))
            self.assertEqual(geo.source, "reference")

    def test_engine_report_needs_two_presses_and_keeps_pit_learning(self):
        from test_live import LiveClock, make_engine
        from datetime import datetime, timezone
        from server.track import TrackGeometry

        async def go():
            src = LiveClock()
            src.t = datetime(2026, 10, 3, 12, tzinfo=timezone.utc)
            eng = make_engine(src)
            eng._track_id = (63, 2026)
            eng.geometry = TrackGeometry(63, "Sakhir", 2026, "learned", [[0, 0], [1, 1]])
            sentinel = object()
            eng.pit_collector = sentinel                       # pit-lane learning in progress
            eng._pit_ctx = {"key": 63}
            calls = []
            eng._pit_start_learning = lambda *a, **k: calls.append(a)
            first = eng.track_report()
            self.assertIn("Press again", first)
            self.assertIsNotNone(eng.geometry)                 # nothing deleted yet
            second = eng.track_report()
            self.assertIn("cleared", second)
            self.assertIsNone(eng.geometry)
            await eng._track_task
            return calls, eng.pit_collector is sentinel, eng.tracks.rejected(63)
        calls, pit_kept, rejected = asyncio.run(go())
        self.assertEqual(calls, [])                            # pit-lane learning not restarted
        self.assertTrue(pit_kept)
        self.assertEqual(rejected, {"learned"})

    def test_engine_fits_the_known_layout_from_positions(self):
        from test_live import LiveClock, make_engine
        from datetime import datetime, timezone

        async def go():
            src = LiveClock()
            src.t = datetime(2026, 10, 3, 12, tzinfo=timezone.utc)
            eng = make_engine(src)
            await eng.feed("SessionInfo", {"Meeting": {"Name": "Azerbaijan Grand Prix", "Location": "Baku",
                                                       "Circuit": {"Key": 144, "ShortName": "Baku"}},
                                           "Path": "2025/x/", "Name": "Race"}, src.t)
            eng.tick()                                         # (the state picks the message up)
            if eng._track_task:
                await eng._track_task                          # no geometry reachable here
            eng._track_id = (144, 2025)
            eng.geometry = None
            for x, y in baku_lap() * 3:
                eng._learn({"t": 0, "cars": [["1", x, y, 1]]})
            eng._ref_poll(1000.0)
            await eng._ref_task
            return eng.geometry
        geo = asyncio.run(go())
        self.assertIsNotNone(geo)
        self.assertEqual(geo.source, "reference")


class RemoteCommandTest(unittest.TestCase):
    def test_track_report_command(self):
        from server.remote import RemoteController

        async def go():
            msgs = []

            async def pub(m):
                msgs.append(m)
            r = RemoteController({"enabled": True}, lambda: [], pub)
            r.track_hook = lambda: "armed"
            ok = await r.handle_command("TRACK_REPORT", None, "test")
            return ok, msgs
        ok, msgs = asyncio.run(go())
        self.assertTrue(ok)
        self.assertEqual(msgs[-1]["toast"]["text"], "armed")


if __name__ == "__main__":
    unittest.main()


class CheckAndChoiceTest(unittest.TestCase):
    """The drawn outline is checked against the car positions; the layout can be chosen."""

    @classmethod
    def setUpClass(cls):
        cls.lap = baku_lap()
        cls.full = fit_reference(reference_points("az-2016"), cls.lap * 3).points

    def test_check_outline(self):
        from server.track_match import check_outline
        samples = self.lap * 8
        self.assertTrue(check_outline(self.full, samples).ok)
        half = check_outline(self.full[: len(self.full) // 2], samples)
        self.assertFalse(half.ok)                                       # part of the track missing
        self.assertGreater(half.far_share, 0.2)
        self.assertIsNone(check_outline(self.full, self.lap).ok)        # too few positions to tell

    def test_identify_without_the_name(self):
        from server.track_match import identify
        rid, fit = identify(self.lap * 3)
        self.assertEqual(rid, "az-2016")
        self.assertTrue(fit.ok)

    def engine(self):
        from test_live import LiveClock, make_engine
        from datetime import datetime, timezone
        src = LiveClock()
        src.t = datetime(2026, 10, 3, 12, tzinfo=timezone.utc)
        eng = make_engine(src)
        eng._track_id = (144, 2025)
        return eng

    def test_half_multiviewer_outline_is_replaced(self):
        from server.track import TrackGeometry

        async def go():
            eng = self.engine()
            eng.geometry = TrackGeometry(144, "Baku", 2025, "multiviewer", self.full[: len(self.full) // 2])
            pit_calls = []
            eng._pit_start_learning = lambda *a, **k: pit_calls.append(a)
            for x, y in self.lap * 8:
                eng._learn({"t": 0, "cars": [["1", x, y, 1]]})
            eng._ref_poll(1000.0)
            await eng._ref_task
            return eng.geometry, pit_calls
        geo, pit_calls = asyncio.run(go())
        self.assertEqual(geo.source, "reference")
        self.assertEqual(geo.info["ref_id"], "az-2016")
        self.assertIn("replaced the multiviewer outline", geo.info["note"])
        self.assertEqual(pit_calls, [])                                 # the pit lane is not touched

    def test_good_multiviewer_outline_stays(self):
        from server.track import TrackGeometry

        async def go():
            eng = self.engine()
            eng.geometry = TrackGeometry(144, "Baku", 2025, "multiviewer", self.full)
            for x, y in self.lap * 8:
                eng._learn({"t": 0, "cars": [["1", x, y, 1]]})
            eng._ref_poll(1000.0)
            await eng._ref_task
            return eng.geometry
        geo = asyncio.run(go())
        self.assertEqual(geo.source, "multiviewer")
        self.assertIn("match", geo.info["check"])

    def test_choose_the_circuit_yourself(self):
        from server.track import TrackGeometry

        async def go():
            eng = self.engine()
            eng.geometry = TrackGeometry(144, "Baku", 2025, "multiviewer", self.full)
            self.assertEqual(eng.set_track_choice("nonsense"), "Unknown layout")
            msg = eng.set_track_choice("az-2016")
            for x, y in self.lap * 3:
                eng._learn({"t": 0, "cars": [["1", x, y, 1]]})
            eng._ref_poll(1000.0)
            await eng._ref_task
            chosen = (eng.geometry.source, dict(eng.geometry.info), eng.tracks.choice(144), msg)
            # a reload keeps your choice (its cached fit), even with MultiViewer reachable
            again = await TrackProvider.load(eng.tracks, 144, 2025, "Baku")   # (make_engine stubs .load)
            eng.set_track_choice("auto")
            if eng._track_task:
                await eng._track_task
            return chosen, again.source, eng.tracks.choice(144)
        (src, info, stored, msg), reloaded, after_auto = asyncio.run(go())
        self.assertIn("chosen", msg)
        self.assertEqual((src, info["ref_id"], info["chosen"], stored), ("reference", "az-2016", True, "az-2016"))
        self.assertEqual(reloaded, "reference")
        self.assertIsNone(after_auto)

    def test_layouts_api(self):
        from server.track_match import known_layouts
        ids = {e["id"] for e in known_layouts()}
        self.assertGreaterEqual(len(ids), 40)
        self.assertTrue({"bh-2002", "az-2016", "jp-1962", "es-2026"} <= ids)
