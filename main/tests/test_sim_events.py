"""TEST mode: SIM_EVENT makes the simulator emit the real F1-style messages the dashboard animates from.

Run:  python -m unittest tests.test_sim_events
"""
import asyncio
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from server.config import load_config  # noqa: E402
from server.sources.simulator import SimulatorSource  # noqa: E402
from server.track import TrackProvider  # noqa: E402
from server.config import DATA_DIR  # noqa: E402


class Sink:
    def __init__(self):
        self.out = []

    async def feed(self, topic, data, ts, snapshot=False, origin="feed"):
        self.out.append((topic, data))


class SimEventTest(unittest.TestCase):
    def setUp(self):
        cfg = load_config("/nonexistent.toml")
        geo = TrackProvider(DATA_DIR, cfg["tracks"]).load_test(cfg["test"].get("circuit", "jp-1962"))
        self.sim = SimulatorSource(cfg["test"], geo)
        self.sim.rc_msgs, self.sim.sector_flags, self.sim.cars = [], set(), []
        self.sim.sim_t = 0.0

    def run_ev(self, name):
        sink = Sink()
        self.sim._emit = lambda s, topic, data: s.feed(topic, data, None)
        self.assertTrue(self.sim.inject(name).startswith("Simulated"))
        asyncio.run(self.sim._apply_events(sink))
        return sink.out

    def test_flags(self):
        self.sim._leader_lap = lambda: 5
        out = self.run_ev("red")
        self.assertIn(("TrackStatus", {"Status": "5", "Message": "Red"}), out)
        self.assertTrue(any(t == "RaceControlMessages" and "RED FLAG" in str(d) for t, d in out))
        self.assertIn(("TrackStatus", {"Status": "4", "Message": "SCDeployed"}), self.run_ev("sc"))
        self.assertIn(("TrackStatus", {"Status": "6", "Message": "VSCDeployed"}), self.run_ev("vsc"))
        self.assertTrue(any("DOUBLE YELLOW" in str(d) for _, d in self.run_ev("dy")))
        out = self.run_ev("chequered")
        self.assertTrue(any("CHEQUERED" in str(d) for _, d in out))
        self.assertIn(("SessionStatus", {"Status": "Finished"}), out)

    def test_unknown_event(self):
        self.assertTrue(self.sim.inject("boom").startswith("Unknown"))


if __name__ == "__main__":
    unittest.main()
