"""Weather report popup (server/weather.py): intensity classes, forecast parsing (Open-Meteo multi
model, MET Norway), comparison of the sources, time -> lap estimate, the every-15-laps trigger on
the real 2026 Japanese GP race, failure handling (no fake values), simulated TEST scenarios.

The forecast services are not reachable from the test environment: their responses are given in
their documented JSON format.

Run:  python -m unittest tests.test_weather
"""
import asyncio
import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from server import weather as W  # noqa: E402

HR = 3600_000
NOW = (int(time.time() * 1000) // HR) * HR + 10 * 60_000          # 10 min past the hour


def om_response(mm_by_model: dict, temps=(20.0, 19.0, 18.0, 17.0, 16.0, 15.0)) -> dict:
    """Open-Meteo hourly, several models: precipitation = sum of the PRECEDING hour."""
    base = NOW - NOW % HR
    times = [int((base + (i + 1) * HR) / 1000) for i in range(-1, 5)]       # end of each interval
    h = {"time": times}
    for m, mm in mm_by_model.items():
        h[f"precipitation_{m}"] = mm
        h[f"precipitation_probability_{m}"] = [None] * len(times)
        h[f"temperature_2m_{m}"] = list(temps)
    return {"hourly": h}


def state(lap=7, total=53, rain=False, leader_lap_s=90.0, weather=True):
    marks = [[NOW - (6 - i) * leader_lap_s * 1000, lap - 6 + i] for i in range(6)]
    w = {"air_temp": 22.4, "track_temp": 31.2, "humidity": 68.0, "wind_speed": 3.9, "wind_direction": 225,
         "rainfall": rain} if weather else {}
    return {"session": {"session_kind": "race", "live": True, "lap": lap, "total_laps": total, "now_ms": NOW},
            "weather": w, "drivers": {"1": {"position": 1, "lap_marks": marks}}}


class UnitTest(unittest.TestCase):
    def test_intensity_classes(self):
        self.assertEqual([W.intensity(x) for x in (0, 0.05, 0.2, 1.0, 3.0, 10.0)],
                         ["NONE", "NONE", "DRIZZLE", "LIGHT", "MEDIUM", "HEAVY"])
        self.assertIsNone(W.intensity(None))
        self.assertEqual(W.compass(225), "SW")

    def test_parse_sources(self):
        s = W.parse_open_meteo(om_response({"ecmwf_ifs025": [0, 0, 1.2, 1.5, 0, 0],
                                            "gfs_seamless": [0, 0, 1.0, 0, 0, 0],
                                            "icon_seamless": [0, 0, 0.8, 0.9, 0, 0]}))
        self.assertEqual(len(s), 3)
        # interval start = time - 1 h: the value of the 3rd row is the rain of [base + 1 h, base + 2 h)
        base = NOW - NOW % HR
        self.assertEqual(s[0].points[2][:2], (base + HR, 1.2))
        met = {"properties": {"timeseries": [
            {"time": W.datetime.fromtimestamp((base + i * HR) / 1000, W.timezone.utc).isoformat().replace("+00:00", "Z"),
             "data": {"instant": {"details": {"air_temperature": 20 - i}},
                      "next_1_hours": {"details": {"precipitation_amount": 1.1 if i == 1 else 0.0}}}} for i in range(5)]}}
        m = W.parse_met_no(met)
        self.assertEqual(m[0].source, "MET Norway")
        self.assertEqual(m[0].points[1][1], 1.1)

    def test_agreeing_sources_give_a_lap_estimate(self):
        series = W.parse_open_meteo(om_response({"ecmwf_ifs025": [0, 0, 1.2, 1.5, 0, 0],
                                                 "gfs_seamless": [0, 0, 1.0, 1.1, 0, 0],
                                                 "icon_seamless": [0, 0, 0.8, 0.9, 0, 0]}))
        rep = W.build_report(state(), NOW, series, [], {}, None, "auto")
        f = rep.forecast
        self.assertIs(f["rain_expected"], True)
        self.assertEqual(f["intensity"], "LIGHT")
        self.assertEqual(f["confidence"], "HIGH")
        # rain from the next full hour (50 min) at 90 s laps from lap 7 -> ~lap 40
        self.assertEqual(f["expected_lap"], "~lap 40")
        self.assertIn("around lap 40", f["text"])
        self.assertTrue(f["lap_estimate"])
        self.assertIn("slippery", rep.race_impact["summary"])
        self.assertIn("F1 live timing (WeatherData)", rep.sources)

    def test_disagreeing_sources_show_uncertainty(self):
        series = W.parse_open_meteo(om_response({"ecmwf_ifs025": [0, 0, 1.2, 0, 0, 0],
                                                 "gfs_seamless": [0, 0, 0, 0, 0, 0],
                                                 "icon_seamless": [0, 0, 0, 0, 3.0, 0]}))
        f = W.build_report(state(), NOW, series, [], {}, None, "auto").forecast
        self.assertEqual((f["rain_expected"], f["confidence"]), ("POSSIBLE", "LOW"))
        self.assertTrue(f["expected_lap"].startswith("laps "))           # a window, not one lap
        self.assertIn("disagree", f["text"])

    def test_no_rain(self):
        series = W.parse_open_meteo(om_response({"ecmwf_ifs025": [0] * 6, "gfs_seamless": [0] * 6, "icon_seamless": [0] * 6}))
        rep = W.build_report(state(), NOW, series, [], {}, None, "auto")
        self.assertIs(rep.forecast["rain_expected"], False)
        self.assertIn("No significant weather changes", rep.race_impact["summary"])

    def test_forecast_unavailable_keeps_current(self):
        rep = W.build_report(state(), NOW, [], ["Open-Meteo: ConnectError", "MET Norway: ConnectError"], {}, None, "auto")
        self.assertFalse(rep.forecast["available"])
        self.assertIn("Weather forecast unavailable", rep.forecast["text"])
        self.assertEqual(rep.current["air_temperature"], 22.4)
        self.assertEqual(rep.current["wind_speed_kmh"], 14.0)             # 3.9 m/s
        self.assertEqual(rep.current["condition"], "DRY")

    def test_missing_f1_weather_is_none_not_zero(self):
        rep = W.build_report(state(weather=False), NOW, [], [], {}, None, "auto")
        c = rep.current
        self.assertFalse(c["available"])
        self.assertEqual([c[k] for k in ("air_temperature", "humidity", "wind_speed_kmh", "rainfall", "condition")],
                         [None] * 5)

    def test_rain_stopping_and_track_trend(self):
        series = W.scenario_series("stopping", NOW)
        rep = W.build_report(state(rain=True), NOW, series, [], {}, -3.0, "auto")
        self.assertTrue(rep.forecast["end_lap"] or rep.forecast["end_time"])
        self.assertTrue(any("drying" in x for x in [rep.race_impact["summary"]] + rep.race_impact["details"]))
        self.assertTrue(any("fallen 3°C" in x for x in rep.race_impact["details"]))

    def test_after_the_race(self):
        series = W.parse_open_meteo(om_response({"ecmwf_ifs025": [0, 0, 0, 0, 2.0, 2.0]}, ))
        f = W.build_report(state(lap=50, total=53), NOW, series, [], {}, None, "auto").forecast
        self.assertEqual(f["expected_lap"], "after the race")

    def test_scenarios(self):
        async def go(name):
            r = W.WeatherReporter({}, "test")
            return await r.report(state(), NOW, None, "manual", scenario=name)
        for name, inten in (("drizzle", "DRIZZLE"), ("light", "LIGHT"), ("medium", "MEDIUM"), ("heavy", "HEAVY")):
            rep = asyncio.run(go(name))
            self.assertTrue(rep["simulated"])
            self.assertEqual(rep["forecast"]["intensity"], inten, name)
        self.assertIs(asyncio.run(go("dry"))["forecast"]["rain_expected"], False)
        self.assertFalse(asyncio.run(go("noforecast"))["forecast"]["available"])
        self.assertEqual(asyncio.run(go("disagree"))["forecast"]["confidence"], "LOW")

    def test_forecast_cached_and_not_for_recordings(self):
        calls = []

        async def fetch(lat, lon):
            calls.append((lat, lon))
            return W.parse_open_meteo(om_response({"ecmwf_ifs025": [0] * 6})), []

        async def go():
            r = W.WeatherReporter({}, "live", fetch=fetch)
            now = time.time() * 1000
            await r.report(state(), now, (34.84, 136.54), "auto")
            await r.report(state(), now, (34.84, 136.54), "auto")            # cached
            old = await r.report(state(), now - 30 * 86400_000, (34.84, 136.54), "auto")
            return old
        old = asyncio.run(go())
        self.assertEqual(len(calls), 1)
        self.assertEqual(old["forecast"]["reason"], "no forecast for a recording")


class TriggerTest(unittest.TestCase):
    def test_every_15_laps_once_no_backlog(self):
        r = W.WeatherReporter({}, "live")
        hits = []
        for lap in range(5, 48):
            hit = r.observe(state(lap=lap), NOW + lap)
            if hit:
                hits.append((lap, hit))
        self.assertEqual(hits, [(16, 15), (31, 30), (46, 45)])         # after laps 15, 30, 45
        # joining at lap 37: no popup for lap 30, the next one after lap 45
        r2 = W.WeatherReporter({}, "live")
        self.assertIsNone(r2.observe(state(lap=37), NOW))
        self.assertIsNone(r2.observe(state(lap=40), NOW))
        self.assertEqual(r2.observe(state(lap=46), NOW), 45)

    def test_not_in_test_mode_or_other_sessions(self):
        r = W.WeatherReporter({}, "test")
        self.assertFalse(any(r.observe(state(lap=x), NOW) for x in range(1, 40)))
        r = W.WeatherReporter({"test_auto": True}, "test")
        self.assertTrue(any(r.observe(state(lap=x), NOW) for x in range(1, 40)))
        r = W.WeatherReporter({}, "live")
        q = state()
        q["session"]["session_kind"] = "qualifying"
        self.assertFalse(any(r.observe(dict(q, session={**q["session"], "lap": x}), NOW) for x in range(1, 40)))

    def test_real_race_triggers_after_lap_15(self):
        from test_replay_state import RACE, Replay
        rep = Replay(RACE, "2026-03-29")
        r = W.WeatherReporter({}, "live")
        hits = []
        for hh, mm in [(5, m) for m in range(10, 60, 2)] + [(6, m) for m in range(0, 20, 2)]:
            st = rep.state(f"{hh:02d}:{mm:02d}:00")
            hit = r.observe(st, st["session"]["now_ms"] or 0)
            if hit:
                hits.append((st["session"]["lap"], hit, st))
        self.assertTrue(hits)
        lap, mult, st = hits[0]
        self.assertEqual(mult, 15)
        self.assertGreaterEqual(lap, 16)
        report = asyncio.run(r.report(st, st["session"]["now_ms"], (34.84, 136.54), "auto"))
        self.assertTrue(report["current"]["available"])                  # real F1 WeatherData
        self.assertIsNotNone(report["current"]["track_temperature"])
        self.assertEqual(report["forecast"]["reason"], "no forecast for a recording")


if __name__ == "__main__":
    unittest.main()
