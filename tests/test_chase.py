"""Race: CHASING (consecutive close laps behind the same car), POSITION vs grid, TYRE AGE.

Synthetic feed for the rules, plus the real 2026 Japanese GP race recording.

Run:  python -m unittest tests.test_chase
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from server import chase as C  # noqa: E402
from server.feedstate import FeedState  # noqa: E402

T0 = 1_700_000_000_000


class Feed:
    def __init__(self):
        self.fs = FeedState()
        self.t = T0
        self.laps = {"1": 0, "2": 0, "3": 0}
        self.fs.apply("TrackStatus", {"Status": "1"}, True, self.t)
        self.fs.apply("TimingData", {"Lines": {
            "1": {"Position": "1", "NumberOfLaps": 0}, "2": {"Position": "2", "NumberOfLaps": 0},
            "3": {"Position": "3", "NumberOfLaps": 0}}}, True, self.t)

    def lap(self, num, gap, pos=None, **extra):
        """car ``num`` crosses the line ``gap`` s behind the car ahead."""
        self.t += 30_000
        self.laps[num] += 1
        upd = {"NumberOfLaps": self.laps[num], "IntervalToPositionAhead": {"Value": f"+{gap:.3f}"}, **extra}
        if pos is not None:
            upd["Position"] = str(pos)
        self.fs.apply("TimingData", {"Lines": {num: upd}}, False, self.t)
        return (self.fs.get(C.CHASE) or {}).get(num)


class ChaseRulesTest(unittest.TestCase):
    def test_consecutive_close_laps_count(self):
        f = Feed()
        self.assertEqual(f.lap("3", 0.8)["laps"], 1)
        self.assertEqual(f.lap("3", 1.1)["laps"], 2)
        c = f.lap("3", 0.4)
        self.assertEqual((c["laps"], c["ahead"]), (3, "2"))

    def test_gap_too_big_ends_the_chase(self):
        f = Feed()
        f.lap("3", 0.8), f.lap("3", 0.9)
        c = f.lap("3", C.CHASE_GAP_S + 0.5)
        self.assertEqual((c["laps"], c["why"]), (0, "gap"))
        self.assertEqual(f.lap("3", 0.7)["laps"], 1)           # a new chase starts at 1

    def test_another_car_ahead_resets(self):
        f = Feed()
        f.lap("3", 0.8), f.lap("3", 0.8)
        # car 3 passes car 2: now behind car 1
        f.fs.apply("TimingData", {"Lines": {"2": {"Position": "3"}}}, False, f.t + 1)
        c = f.lap("3", 0.6, pos=2)
        self.assertEqual((c["laps"], c["ahead"]), (1, "1"))

    def test_pit_lap_and_safety_car_end_the_chase(self):
        f = Feed()
        f.lap("3", 0.8), f.lap("3", 0.8)
        self.assertEqual(f.lap("3", 0.8, InPit=True)["why"], "pit")
        f.fs.apply("TimingData", {"Lines": {"3": {"InPit": False}}}, False, f.t + 1)
        self.assertEqual(f.lap("3", 0.8)["laps"], 0)           # the out lap went through the pit too
        self.assertEqual(f.lap("3", 0.8)["laps"], 1)           # then a new chase
        f.fs.apply("TrackStatus", {"Status": "4"}, False, f.t + 1)            # safety car
        c = f.lap("3", 0.3)
        self.assertEqual((c["laps"], c["why"]), (0, "neutral"))

    def test_leader_has_no_chase(self):
        f = Feed()
        c = f.lap("1", 0.0)
        self.assertEqual((c["laps"], c["why"]), (0, "leader"))

    def test_gap_parsing(self):
        self.assertEqual(C.gap_s("+0.512"), 0.512)
        self.assertEqual(C.gap_s({"Value": "1.2"}), 1.2)
        self.assertIsNone(C.gap_s("1 L"))
        self.assertIsNone(C.gap_s(""))


class RealRaceTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from test_replay_state import RACE, Replay
        cls.r = Replay(RACE, "2026-03-29")

    def drivers(self, hms):
        return {d["tla"]: d for d in self.r.state(hms)["drivers"].values()}

    def test_race_stats(self):
        late = self.drivers("06:25:00")
        ver = late["VER"]
        self.assertGreaterEqual(ver["chase"]["laps"], 5)                     # close behind GAS for laps
        self.assertEqual(ver["chase"]["ahead_tla"], "GAS")
        self.assertLessEqual(ver["chase"]["gap"], C.CHASE_GAP_S)
        self.assertEqual(late["ANT"]["chase"]["laps"], 0)                    # the leader chases nobody
        d = self.drivers("05:35:00")
        ant = d["ANT"]
        # position vs grid, tyre age of the current set (not the race laps)
        self.assertEqual((ant["grid_position"], ant["position"]), (1, 4))
        self.assertEqual(d["VER"]["grid_position"], 11)
        later = self.drivers("05:55:00")
        self.assertLess(later["ANT"]["tyre"]["tyre_age"], 10)                # new set after the stop
        self.assertGreater(d["ANT"]["tyre"]["tyre_age"], 10)

    def test_every_chase_is_behind_the_current_car_ahead(self):
        for hms in ("05:25:00", "05:35:00", "06:10:00", "06:25:00"):
            ds = self.r.state(hms)["drivers"].values()
            by_pos = {d["position"]: d for d in ds if d.get("position")}
            for d in ds:
                c = d.get("chase") or {}
                if c.get("laps"):
                    self.assertEqual(by_pos[d["position"] - 1]["number"], str(c["ahead"]), (hms, d["tla"]))


if __name__ == "__main__":
    unittest.main()
