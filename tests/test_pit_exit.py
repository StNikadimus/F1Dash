"""PIT EXIT OPEN / CLOSED from race control (track_status.pit_exit), at the shown moment of a replay.

Real data: the 2026 Japanese GP race (PIT EXIT CLOSED before the start, "GREEN LIGHT - PIT EXIT OPEN"
at 05:14:02 UTC).

Run:  python -m unittest tests.test_pit_exit
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_replay_state import RACE, Replay  # noqa: E402


class PitExitTest(unittest.TestCase):
    def test_real_race_and_seek(self):
        r = Replay(RACE, "2026-03-29")
        self.assertEqual(r.state("05:14:00")["track_status"]["pit_exit"], "CLOSED")
        self.assertEqual(r.state("05:14:30")["track_status"]["pit_exit"], "OPEN")
        go = r.vod()                                   # the VOD seek path: both directions
        self.assertEqual(go("05:30:00")["track_status"]["pit_exit"], "OPEN")
        self.assertEqual(go("05:00:00")["track_status"]["pit_exit"], "CLOSED")


if __name__ == "__main__":
    unittest.main()
