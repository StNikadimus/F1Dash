"""Weather radar (server/radar.py): grid parsing, intensity / movement / rain ETA analysis, cache and
failure handling, RainViewer frame list, the TEST scenarios and the weather report integration.

RainViewer and Open-Meteo are not reachable from the test environment: their responses are given
in their documented JSON format.

Run:  python -m unittest tests.test_radar
"""
import asyncio
import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from server import radar as R  # noqa: E402
from server import weather as W  # noqa: E402

TH = W.DEFAULT_THRESHOLDS
N, RAD = 9, 100.0
NOW = time.time() * 1000


def om_grid(frames: list) -> list:
    """Open-Meteo multi-location minutely_15: precipitation = mm in the PRECEDING 15 min."""
    times = [int((f["t"] + 15 * 60_000) / 1000) for f in frames]
    return [{"minutely_15": {"time": times, "precipitation": [f["cells"][k] / 4 for f in frames]}}
            for k in range(N * N)]


class FakeHTTP:
    def __init__(self, grid=None, maps=None, fail=False):
        self.grid, self.maps, self.fail, self.calls = grid, maps, fail, []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def get(self, url, params=None):
        self.calls.append(url)
        if self.fail:
            raise R.httpx.ConnectError("blocked")
        body = self.grid if "open-meteo" in url else self.maps

        class Resp:
            def raise_for_status(self):
                pass

            def json(self):
                return body
        return Resp()


class AnalysisTest(unittest.TestCase):
    def ana(self, name):
        fr = R.scenario_frames(name, NOW, N, RAD)
        return R.analyse(fr, NOW, N, RAD, TH)

    def test_parse_grid(self):
        fr = R.scenario_frames("approaching", NOW, N, RAD)
        back = R.parse_grid(om_grid(fr), N)
        self.assertEqual([f["t"] for f in back], [f["t"] for f in fr])
        self.assertEqual(back[2]["cells"], [round(x, 2) for x in fr[2]["cells"]])
        with self.assertRaises(ValueError):
            R.parse_grid(om_grid(fr)[:10], N)
        pts = R.grid_points(34.84, 136.54, RAD, N)
        self.assertEqual(len(pts), 81)
        self.assertEqual(pts[40][2:], (0.0, 0.0))                       # the middle point is the circuit

    def test_states(self):
        a = self.ana("approaching")
        self.assertEqual(a["movement"], "APPROACHING")
        self.assertIsNotNone(a["rain_eta_minutes"])
        self.assertGreater(a["rain_eta_minutes"], 0)
        self.assertEqual(a["current_intensity"], "NONE")
        o = self.ana("over")
        self.assertEqual((o["movement"], o["rain_eta_minutes"], o["eta_label"]), ("OVER CIRCUIT", 0, "NOW"))
        self.assertIn(o["current_intensity"], ("LIGHT", "MEDIUM"))
        w = self.ana("away")
        self.assertEqual(w["movement"], "MOVING AWAY")
        self.assertIsNone(w["rain_eta_minutes"])
        n = self.ana("norain")
        self.assertEqual((n["movement"], n["eta_label"]), (None, "NO RAIN"))
        self.assertEqual(self.ana("heavyrain")["area_max_intensity"], "HEAVY")
        self.assertIsNone(R.scenario_frames("radaroff", NOW, N, RAD))

    def test_no_eta_without_support(self):
        fr = R.scenario_frames("approaching", NOW, N, RAD)
        only_past = [f for f in fr if f["t"] <= NOW][-1:]                 # one frame: no movement, no nowcast
        a = R.analyse(only_past, NOW, N, RAD, TH)
        self.assertIsNone(a["rain_eta_minutes"])
        self.assertIn(a["eta_label"], ("RAIN POSSIBLE", "NOT EXPECTED"))

    def test_zoom(self):
        self.assertLessEqual(R.tile_zoom(45.6, 100, 7), 7)
        self.assertLess(R.tile_zoom(45.6, 400, 7), R.tile_zoom(45.6, 50, 10))


class CacheTest(unittest.TestCase):
    def test_cache_failure_and_age(self):
        fr = R.scenario_frames("approaching", NOW, N, RAD)
        maps = {"host": "https://tilecache.rainviewer.com", "radar": {
            "past": [{"time": int(NOW / 1000) - 600 * k, "path": f"/v2/radar/{k}"} for k in range(5, -1, -1)],
            "nowcast": []}}
        good = FakeHTTP(om_grid(fr), maps)
        bad = FakeHTTP(fail=True)
        use = [good]
        rad = R.Radar({"refresh_seconds": 0, "max_age_seconds": 900}, TH, http_factory=lambda: use[0])

        async def go():
            a = await rad.get(45.6, 9.28, NOW)
            use[0] = bad
            b = await rad.get(45.6, 9.28, NOW)                            # provider down: the cache, with its age
            rad._grid = {k: (v[0] - 1000, v[1], v[2]) for k, v in rad._grid.items()}
            rad._maps = (rad._maps[0] - 1000, rad._maps[1])
            c = await rad.get(45.6, 9.28, NOW)                            # too old: not shown
            return a, b, c
        a, b, c = asyncio.run(go())
        self.assertIsNotNone(a[0])
        self.assertEqual(a[3], [])
        self.assertIsNotNone(b[0])
        self.assertTrue(b[3])                                             # the failure is reported
        self.assertIsNone(c[0])
        rep = rad.build(45.6, 9.28, NOW, *a, outline=[[45.6, 9.28]] * 5, name="Monza")
        self.assertTrue(rep["available"])
        self.assertEqual(rep["rainviewer"]["host"], maps["host"])
        self.assertIn(len(rep["rainviewer"]["frames"]), (3, 4))           # -30 .. 0 min of the 6 past frames
        self.assertIn("RainViewer", rep["provider"])
        off = rad.build(45.6, 9.28, NOW, *c, outline=None, name="Monza")
        self.assertFalse(off["available"])
        self.assertIn("RainViewer", off["reason"])

    def test_refresh_interval(self):
        fr = R.scenario_frames("norain", NOW, N, RAD)
        http = FakeHTTP(om_grid(fr), {"host": "h", "radar": {"past": []}})
        rad = R.Radar({"refresh_seconds": 180}, TH, http_factory=lambda: http)

        async def go():
            await rad.get(45.6, 9.28, NOW)
            await rad.get(45.6, 9.28, NOW)
        asyncio.run(go())
        self.assertEqual(len(http.calls), 2)                              # one grid + one frame list, then cached


class ReportTest(unittest.TestCase):
    def state(self):
        marks = [[NOW - (6 - i) * 90_000, 24 + i] for i in range(6)]
        return {"session": {"session_kind": "race", "live": True, "lap": 30, "total_laps": 70, "now_ms": NOW},
                "weather": {"air_temp": 21.0, "track_temp": 29.0, "humidity": 72.0, "wind_speed": 3.3,
                            "wind_direction": 270, "rainfall": False},
                "drivers": {"1": {"position": 1, "lap_marks": marks}}}

    def report(self, scenario):
        r = W.WeatherReporter({}, "test")
        return asyncio.run(r.report(self.state(), NOW, (45.62, 9.28), "manual", scenario=scenario,
                                    outline=[[45.62, 9.28], [45.63, 9.29], [45.61, 9.29]], name="Monza"))

    def test_scenarios_in_the_report(self):
        rep = self.report("approaching")
        rd = rep["radar"]
        self.assertTrue(rd["available"] and rd["simulated"])
        self.assertEqual(rd["movement"], "APPROACHING")
        self.assertIsNotNone(rd["rain_eta_lap"])                          # minutes -> laps from the pace
        self.assertIn("approaching the circuit", rep["race_impact"]["summary"])
        self.assertEqual(rd["circuit"], "Monza")
        self.assertIsNone(rd["rainviewer"])                               # never real imagery for a simulation
        off = self.report("radaroff")
        self.assertFalse(off["radar"]["available"])
        self.assertTrue(off["current"]["available"])                      # the rest of the report stays
        self.assertTrue(off["forecast"]["available"])
        self.assertEqual(self.report("over")["radar"]["movement"], "OVER CIRCUIT")
        self.assertEqual(self.report("away")["radar"]["movement"], "MOVING AWAY")
        self.assertEqual(self.report("heavyrain")["radar"]["area_max_intensity"], "HEAVY")
        self.assertEqual(self.report("norain")["radar"]["eta_label"], "NO RAIN")

    def test_no_radar_for_a_recording_or_unknown_circuit(self):
        r = W.WeatherReporter({}, "replay", fetch=lambda la, lo: asyncio.sleep(0, ([], [])))

        async def go():
            old = await r.report(self.state(), NOW - 10 * 86400_000, (45.6, 9.3), "auto")
            nowhere = await r.report(self.state(), time.time() * 1000, None, "manual")
            return old, nowhere
        old, nowhere = asyncio.run(go())
        self.assertIn("recording", old["radar"]["reason"])
        self.assertEqual(nowhere["radar"]["reason"], "circuit location unknown")

    def test_outline_from_the_bundled_layouts(self):
        from server.track_match import circuit_location, circuit_outline_latlon
        ol = circuit_outline_latlon("it-1922")
        lat, lon = circuit_location("it-1922")
        self.assertGreater(len(ol), 20)
        self.assertTrue(all(abs(p[0] - lat) < 0.05 and abs(p[1] - lon) < 0.05 for p in ol))   # Monza, real coordinates


if __name__ == "__main__":
    unittest.main()
