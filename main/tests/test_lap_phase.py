"""OUT LAP / IN LAP / IN PIT in place of S1-S3 (DriverState.lap_phase), from the official pit
and lap data of real sessions - never from slow times or missing sectors.

Real data: Suzuka 2026 qualifying and the 2026 Japanese GP race (see test_replay_state.py).

Run:  python -m unittest tests.test_lap_phase
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_replay_state import FP3, QUALI, RACE, Replay, tla  # noqa: E402


class LapPhaseTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.q = Replay(QUALI, "2026-03-28")
        cls.r = Replay(RACE, "2026-03-29")
        cls.p = Replay(FP3, "2026-03-28")

    def phase(self, rep, hms, who):
        return tla(rep.state(hms), who).get("lap_phase")

    def test_qualifying(self):
        self.assertEqual(self.phase(self.q, "06:01:00", "NOR"), "IN PIT")       # in the garage at the start
        self.assertEqual(self.phase(self.q, "06:04:00", "NOR"), "OUT LAP")      # left the pit at 06:03
        self.assertIsNone(self.phase(self.q, "06:06:00", "NOR"))                # timed lap: S1-S3
        self.assertEqual(self.phase(self.q, "06:33:10", "NOR"), "IN LAP")       # came in from the track

    def test_practice(self):
        self.assertEqual(self.phase(self.p, "03:06:30", "NOR"), "OUT LAP")
        self.assertEqual(self.phase(self.p, "03:35:50", "NOR"), "IN LAP")
        self.assertEqual(self.phase(self.p, "03:38:00", "NOR"), "IN PIT")       # parked in the garage
        # timing re-sent long after the car was parked: never an IN LAP
        self.assertEqual(self.phase(self.p, "05:31:30", "NOR"), "IN PIT")

    def test_race(self):
        # formation lap / start: the cars left the grid, not the pit - a normal lap, not an out lap
        self.assertIsNone(self.phase(self.r, "05:12:00", "VER"))
        self.assertEqual(self.phase(self.r, "05:49:55", "VER"), "IN LAP")       # pit stop
        self.assertEqual(self.phase(self.r, "05:51:00", "VER"), "OUT LAP")
        self.assertIsNone(self.phase(self.r, "05:53:00", "VER"))


if __name__ == "__main__":
    unittest.main()
