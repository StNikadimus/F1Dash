"""Replay-driven session clock, race control state (red flag / SC / VSC), FIA deletions,
driver-specific FIA messages and the qualifying lap state (OUT / PREP / HOT / COOLDOWN).

Real data only:
* tests/fixtures/suzuka-2026-qualifying.json.gz, suzuka-2026-fp3.json.gz - official live timing
  of the 2026 Japanese GP qualifying / FP3 (see LICENSE-f1-telemetry-samples.txt);
* data/recordings/sample-2026-japan-race.json.gz - the 2026 Japanese GP race;
* tests/fixtures/openf1_2024_saopaulo_race_control.json - OpenF1 race control of the 2024 Sao
  Paulo GP qualifying (five red flags) and race (VSC, safety car, red flag, restart). The
  official session clock of those sessions is not reachable here (the live-timing archive is
  blocked in this environment), so the clock stop / restart is verified on Suzuka, where the
  feed uses the same mechanism at every phase end / start.

Run:  python -m unittest discover tests
"""
import json
import shutil
import subprocess
import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from server.feedstate import FeedState  # noqa: E402
from server.models import Availability  # noqa: E402
from server.normalizer import Normalizer  # noqa: E402
from server.sources.replay import load_file  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
FIX = ROOT / "tests" / "fixtures"
QUALI = FIX / "suzuka-2026-qualifying.json.gz"
FP3 = FIX / "suzuka-2026-fp3.json.gz"
RACE = ROOT / "data" / "recordings" / "sample-2026-japan-race.json.gz"
SP = json.loads((FIX / "openf1_2024_saopaulo_race_control.json").read_text())


def at(day: str, hms: str) -> datetime:
    return datetime.fromisoformat(f"{day}T{hms}+00:00")


class Replay:
    """State at a moment: fresh (all messages up to T in order) or via the VOD seek path."""
    _cache: dict = {}

    def __init__(self, path: Path, day: str):
        if path not in Replay._cache:
            Replay._cache[path] = load_file(path)
        self.events, self.day = Replay._cache[path], day

    def feed(self, hms: str) -> FeedState:
        fs, t = FeedState(), at(self.day, hms)
        for e in self.events:
            if e.t > t:
                break
            fs.apply(e.topic, e.data, e.snap, e.t.timestamp() * 1000)
        return fs

    def state(self, hms: str, speed: float = 1.0, feed: FeedState = None) -> dict:
        t = at(self.day, hms)
        st = Normalizer().build(feed or self.feed(hms), t, speed, Availability(), rc_until=t)
        st.pop("availability", None)
        return st

    def vod(self):
        from server.openf1 import OpenF1Client
        from server.sources.vod import VodSource, _ms as ems
        from server.timeline import Timeline
        src = VodSource({"session_key": 1}, OpenF1Client(Path("/nonexistent")), Path("/nonexistent"))
        src.events, src._times = self.events, [ems(e) for e in self.events]
        src.ckpts, src.ref = VodSource._prepare(self.events)
        src.state = "ready"
        tl = Timeline(buffer_seconds=120)

        def go(hms: str) -> dict:
            t = at(self.day, hms).timestamp() * 1000
            src.ensure(t, lambda tp, d, m, snap: tl.ingest(tp, d, m, m, snap), tl)
            tl.advance(t)
            return self.state(hms, feed=tl.feed)
        return go


def tla(st: dict, name: str) -> dict:
    return next(d for d in st["drivers"].values() if d["tla"] == name)


# ---------------------------------------------------------------------------
class SessionClockTest(unittest.TestCase):
    """The clock is a function of the shown F1 moment; the browser only interpolates between
    two states with the replay rate (0 when the video is paused)."""

    @classmethod
    def setUpClass(cls):
        cls.q = Replay(QUALI, "2026-03-28")
        cls.r = Replay(RACE, "2026-03-29")

    def clock(self, rep, hms, speed=1.0):
        return rep.state(hms, speed)["session"]["clock"]

    def test_clock_follows_the_replay_moment(self):
        # Q2: 15:00 started at 06:25:00.012 (the 14:59 post one second later)
        c = self.clock(self.q, "06:30:00")
        self.assertEqual((c["remaining_ms"], c["running"]), (600012, True))
        self.assertEqual(self.clock(self.q, "06:30:10")["remaining_ms"], 590012)

    def test_pause_freezes_the_clock(self):
        # video paused: the server sends rate 0 and the same moment again -> the same clock
        a, b = self.clock(self.q, "06:30:00", speed=0.0), self.clock(self.q, "06:30:00", speed=0.0)
        self.assertEqual(a, b)
        self.assertEqual(a["speed"], 0.0)

    def test_seek_recomputes_the_clock(self):
        go = self.q.vod()
        # (Q1 06:10: the recording joined Q1 mid-way - its only clock post then is "17:44" at
        # 06:00:15.113, whole seconds: the state at that moment is 1 s coarse, as the feed itself)
        for hms, rem in (("06:30:00", 600012), ("06:20:00", 0), ("06:30:10", 590012), ("06:10:00", 479114)):
            self.assertEqual(go(hms)["session"]["clock"]["remaining_ms"], rem, hms)

    def test_session_end_stops_at_zero(self):
        self.assertEqual(self.clock(self.q, "06:39:58")["remaining_ms"], 2012)
        for hms in ("06:40:00.2", "06:40:05", "06:45:00", "06:18:05", "06:23:00"):
            c = self.clock(self.q, hms)
            self.assertEqual((c["remaining_ms"], c["running"]), (0, False), hms)

    def test_stop_and_restart_between_phases(self):
        # the same messages as a red flag stop / restart: stopped at the value, then counting again
        self.assertEqual(self.clock(self.q, "06:24:30")["running"], False)
        self.assertEqual(self.clock(self.q, "06:24:30")["remaining_ms"], 900000)
        self.assertEqual(self.clock(self.q, "06:24:50")["remaining_ms"], 900000)
        c = self.clock(self.q, "06:25:30")
        self.assertEqual((c["running"], c["remaining_ms"]), (True, 870012))

    def test_race_clock_frozen_at_the_finish(self):
        # the feed never stops the race clock (real 2026 data) - the official finish does
        before = self.clock(self.r, "06:40:00")
        self.assertTrue(before["running"])
        a, b = self.clock(self.r, "06:43:00"), self.clock(self.r, "06:46:00")
        self.assertFalse(a["running"])
        self.assertEqual(a["remaining_ms"], b["remaining_ms"])
        self.assertEqual(self.r.state("06:43:00")["session"]["state"], "FINISHED")


@unittest.skipUnless(shutil.which("node"), "node not installed")
class ClockInterpolationJsTest(unittest.TestCase):
    """dashboard/components/f1time.js - what the TV shows between two server states."""

    def test_pause_end_and_play(self):
        js = (ROOT / "dashboard" / "components" / "f1time.js").read_text() + """
          const run = {remaining_ms: 5000, running: true, speed: 1};
          console.log(JSON.stringify({
            playing: F1Time.clockNow(run, 1500),
            paused: F1Time.clockNow({...run, speed: 0}, 60000),
            ended: F1Time.clockNow(run, 60000),
            stopped: F1Time.clockNow({remaining_ms: 900000, running: false, speed: 1}, 60000),
            f1paused: F1Time.f1Now(1000, 0, 60000), f1playing: F1Time.f1Now(1000, 1, 250),
            unknown: F1Time.clockNow({remaining_ms: null}, 10)}));"""
        out = subprocess.run(["node", "-e", js], capture_output=True, text=True, timeout=30)
        self.assertEqual(out.returncode, 0, out.stderr)
        r = json.loads(out.stdout)
        self.assertEqual(r, {"playing": 3500, "paused": 5000, "ended": 0, "stopped": 900000,
                             "f1paused": 1000, "f1playing": 1250, "unknown": None})


# ---------------------------------------------------------------------------
def rc_feed(records: list, until_iso: str = None) -> FeedState:
    """OpenF1 race_control records -> the F1 RaceControlMessages topic (field names only)."""
    msgs = []
    for x in records:
        m = {"Utc": x["date"].replace("+00:00", "Z"), "Category": x["category"], "Message": x["message"]}
        if x.get("flag"):
            m["Flag"] = x["flag"]
        if x.get("scope"):
            m["Scope"] = x["scope"]
        if x.get("lap_number") is not None:
            m["Lap"] = x["lap_number"]
        if x.get("driver_number") is not None:
            m["RacingNumber"] = str(x["driver_number"])
        msgs.append(m)
    fs = FeedState()
    fs.apply("RaceControlMessages", {"Messages": msgs}, True, 0)
    return fs


class RaceControlStateTest(unittest.TestCase):
    """2024 Sao Paulo GP (OpenF1 race control, real timestamps): the session flow at any moment."""

    def state(self, records, day_hms: str) -> dict:
        t = datetime.fromisoformat(day_hms).replace(tzinfo=timezone.utc)
        st = Normalizer().build(rc_feed(records), t, 1.0, Availability(), rc_until=t)
        return st

    def flow(self, records, day_hms):
        st = self.state(records, day_hms)
        return st["session"]["state"], st["session"]["red_flag"], st["track_status"]["status"]

    def test_qualifying_red_flags(self):
        q = SP["qualifying_9627"]
        d = "2024-11-03T"
        self.assertEqual(self.flow(q, d + "10:35:00"), ("RUNNING", False, "GREEN"))
        self.assertEqual(self.flow(q, d + "10:39:12"), ("SUSPENDED", True, "RED"))       # red 10:39:10
        self.assertEqual(self.flow(q, d + "10:45:00"), ("SUSPENDED", True, "RED"))       # track clear, not restarted
        self.assertEqual(self.flow(q, d + "10:47:10"), ("RUNNING", False, "GREEN"))      # resumed 10:47:00
        self.assertEqual(self.flow(q, d + "11:12:30")[:2], ("SUSPENDED", True))
        self.assertEqual(self.flow(q, d + "11:28:00")[:2], ("FINISHED", False))          # Q2 ended under red
        self.assertEqual(self.flow(q, d + "12:12:00"), ("FINISHED", False, "CHEQUERED"))
        st = self.state(q, d + "10:35:00")
        self.assertEqual(st["session"]["rc_coverage"], "PARTIAL")                        # messages only
        self.assertEqual(st["track_status"]["source"], "RaceControl")

    def test_race_vsc_sc_red_restart_chequered(self):
        r = SP["race_9636"]
        d = "2024-11-03T"
        self.assertEqual(self.flow(r, d + "16:00:00"), ("RUNNING", False, "GREEN"))
        self.assertEqual(self.flow(r, d + "16:28:30")[2], "VSC")
        self.assertEqual(self.flow(r, d + "16:29:55")[2], "VSC_ENDING")
        self.assertEqual(self.flow(r, d + "16:31:00")[2], "GREEN")
        self.assertEqual(self.flow(r, d + "16:35:00")[2], "SC")
        self.assertEqual(self.flow(r, d + "16:40:00"), ("SUSPENDED", True, "RED"))
        self.assertEqual(self.flow(r, d + "17:05:00"), ("RUNNING", False, "GREEN"))      # restart 17:02:00.390
        st = self.state(r, d + "17:18:30")
        self.assertEqual((st["track_status"]["status"], st["track_status"]["sc_phase"]), ("SC", "SAFETY CAR IN THIS LAP"))
        self.assertEqual(self.flow(r, d + "17:20:00")[2], "GREEN")
        self.assertEqual(self.flow(r, d + "17:57:00"), ("FINISHED", False, "CHEQUERED"))

    def test_no_future_events_and_seek_both_ways(self):
        r = SP["race_9636"]
        d = "2024-11-03T"
        early = self.state(r, d + "16:36:00")
        self.assertFalse(any("RED FLAG" in m["text"] for m in early["race_control"]))    # red is at 16:37:22
        seq = [self.flow(r, d + x) for x in ("16:40:00", "16:36:00", "16:40:00", "17:05:00", "16:40:00")]
        self.assertEqual(seq, [("SUSPENDED", True, "RED"), ("RUNNING", False, "SC"), ("SUSPENDED", True, "RED"),
                               ("RUNNING", False, "GREEN"), ("SUSPENDED", True, "RED")])

    def test_missing_data_is_unknown_not_green(self):
        st = Normalizer().build(FeedState(), datetime(2024, 11, 3, 12, tzinfo=timezone.utc), 1.0, Availability())
        self.assertEqual((st["session"]["state"], st["track_status"]["status"], st["session"]["rc_coverage"]),
                         ("UNKNOWN", "UNKNOWN", "NONE"))

    def test_deletions_are_attached_only_when_reliable(self):
        q = SP["qualifying_9627"]
        d = "2024-11-03T"
        before = self.state(q, d + "10:53:40")["drivers"]
        self.assertEqual(before, {})                                                     # no timing: no cars
        from server.race_control import process_messages
        raw = rc_feed(q).get("RaceControlMessages")
        cut = lambda hms: process_messages(raw, datetime.fromisoformat(d + hms).replace(tzinfo=timezone.utc).timestamp() * 1000)
        self.assertNotIn("1:30.003", cut("10:53:40").driver_flags.get("1").deleted_times
                         if "1" in cut("10:53:40").driver_flags else [])
        self.assertEqual(cut("10:53:50").driver_flags["1"].deleted_times, ["1:30.003"])          # VER, time named
        self.assertEqual(cut("10:57:00").driver_flags["81"].deleted_lap_numbers, [11])          # PIA, lap named
        bot = cut("10:40:00").driver_flags["77"]                                                 # "LAP DELETED - DOUBLE YELLOW"
        self.assertEqual((bot.deleted_laps, bot.deleted_times, bot.deleted_lap_numbers), (1, [], []))

    def test_driver_specific_messages(self):
        from server.race_control import process_messages
        raw = rc_feed(SP["qualifying_9627"]).get("RaceControlMessages")
        res = process_messages(raw, datetime(2024, 11, 3, 12, 20, tzinfo=timezone.utc).timestamp() * 1000)
        tsu = [m["text"] for m in res.driver_messages["22"]]
        self.assertTrue(any("CROSSING THE LINE AT PIT ENTRY" in t for t in tsu))
        for num, msgs in res.driver_messages.items():
            for m in msgs:
                self.assertIn(f"CAR {num} (", m["text"].replace("CARS ", "CAR "))           # never another car's
        self.assertFalse(any("TSU" in m["text"] for m in res.driver_messages.get("44", [])))


# ---------------------------------------------------------------------------
class DeletedLapReplayTest(unittest.TestCase):
    """Real Suzuka 2026 Q3: LIN 1:31.537 (06:51:10.382), deleted by race control 06:51:12.279."""

    @classmethod
    def setUpClass(cls):
        cls.q = Replay(QUALI, "2026-03-28")

    def test_valid_deleted_back_forward(self):
        go = self.q.vod()
        seq = []
        for hms in ("06:51:11", "06:51:13", "06:51:11", "06:51:13", "06:53:00"):
            d = tla(go(hms), "LIN")
            seq.append((d["best_lap"]["value"], d["no_time"], d["last_lap"]["value"], d["last_deleted"]))
        self.assertEqual(seq, [("1:31.537", False, "1:31.537", False),
                               (None, True, None, True),
                               ("1:31.537", False, "1:31.537", False),
                               (None, True, None, True),
                               ("1:46.227", False, "1:46.227", False)])                  # next valid lap
        msgs = [m["text"] for m in tla(go("06:51:13"), "LIN")["rc"]["messages"]]
        self.assertTrue(any("1:31.537 DELETED" in t for t in msgs))
        self.assertFalse(any("1:31.537 DELETED" in t for t in (m["text"] for m in tla(go("06:51:11"), "LIN")["rc"]["messages"])))


# ---------------------------------------------------------------------------
class LapStateTest(unittest.TestCase):
    """OUT LAP / PREP / HOT LAP / COOLDOWN on real laps (each expectation checked against the
    lap's real sector / lap times in the recording)."""

    @classmethod
    def setUpClass(cls):
        cls.q = Replay(QUALI, "2026-03-28")
        cls.fp = Replay(FP3, "2026-03-28")

    def ls(self, rep, name, hms):
        d = tla(rep.state(hms), name)
        return d["lap_now"], d["lap_state"], d["lap_state_conf"]

    def test_case_a_pit_out_hot_cooldown_q2(self):
        # LEC Q2: pit exit 06:33:41, crossing 06:35:28 at 262 km/h, S1 31.778 (PB), hot 1:29.303,
        # then S1 39.745 (cooldown)
        self.assertEqual(self.ls(self.q, "LEC", "06:33:30")[0], None)                     # standing in the pit
        self.assertEqual(self.ls(self.q, "LEC", "06:34:30"), (11, "OUT LAP", "HIGH"))
        self.assertEqual(self.ls(self.q, "LEC", "06:35:40"), (12, "HOT LAP", "MEDIUM"))   # from the line speed
        self.assertEqual(self.ls(self.q, "LEC", "06:36:45"), (12, "HOT LAP", "HIGH"))     # two push sectors
        self.assertEqual(self.ls(self.q, "LEC", "06:37:45")[1:], ("COOLDOWN", "MEDIUM"))

    def test_hot_laps_in_q1_q2_q3(self):
        for hms, lap in (("06:11:40", 6), ("06:36:30", 12), ("06:51:00", 15)):
            self.assertEqual(self.ls(self.q, "LEC", hms)[:2], (lap, "HOT LAP"), hms)

    def test_case_d_hot_cooldown_hot(self):
        # HAM Q2: lap 12 hot, lap 13 cooldown (S2 1.37x own pace), lap 14 hot again
        self.assertEqual(self.ls(self.q, "HAM", "06:36:45")[:2], (12, "HOT LAP"))
        self.assertEqual(self.ls(self.q, "HAM", "06:38:00")[:2], (13, "COOLDOWN"))
        self.assertEqual(self.ls(self.q, "HAM", "06:40:20")[:2], (14, "HOT LAP"))

    def test_extra_preparation_lap_before_the_push(self):
        # RUS Q2: 12 hot, 13 cooldown, 14 a second slow lap (preparation), 15 hot
        self.assertEqual(self.ls(self.q, "RUS", "06:36:55")[:2], (13, "COOLDOWN"))
        self.assertEqual(self.ls(self.q, "RUS", "06:39:00")[:2], (14, "PREP"))
        self.assertEqual(self.ls(self.q, "RUS", "06:40:35")[:2], (15, "HOT LAP"))

    def test_case_b_out_prep_hot(self):
        # LEC FP3: pit exit 03:14:4x, lap 12 began at 229 km/h (0.87x best) and took 1:45.916 ->
        # preparation; lap 13 began at 264 km/h and was 1:30.305 -> hot
        self.assertEqual(self.ls(self.fp, "LEC", "03:15:30")[:2], (11, "OUT LAP"))
        self.assertEqual(self.ls(self.fp, "LEC", "03:17:30")[:2], (12, "PREP"))
        self.assertEqual(self.ls(self.fp, "LEC", "03:19:30")[:2], (13, "HOT LAP"))

    def test_consecutive_slow_laps_and_a_given_up_push(self):
        # HAD FP3: 16 cooldown, 17 preparation (1:54.147), 18 pushed S1 then slow (given up ->
        # UNKNOWN, not HOT), 19 preparation again
        self.assertEqual(self.ls(self.fp, "HAD", "03:22:00")[:2], (16, "COOLDOWN"))
        self.assertEqual(self.ls(self.fp, "HAD", "03:24:40")[:2], (17, "PREP"))
        self.assertEqual(self.ls(self.fp, "HAD", "03:26:40")[:2], (18, "UNKNOWN"))
        self.assertEqual(self.ls(self.fp, "HAD", "03:28:00")[:2], (19, "PREP"))

    def test_unknown_rather_than_a_guess(self):
        # LEC Q1 lap 3 (06:05:40) was a push, but only a few cars had set a lap: the session pace
        # is not established -> UNKNOWN, not a guessed HOT LAP
        self.assertEqual(self.ls(self.q, "LEC", "06:06:00")[:2], (3, "UNKNOWN"))
        # knocked-out / garage cars: no lap state
        st = self.q.state("06:58:00")
        self.assertTrue(all(d["lap_state"] in (None, "PIT") for d in st["drivers"].values() if d["out_phase"]))

    def test_lap_state_is_deterministic_after_seeking(self):
        go = self.q.vod()
        a = [tla(go(h), "RUS")["lap_state"] for h in ("06:36:55", "06:39:00", "06:40:35")]
        b = [tla(go(h), "RUS")["lap_state"] for h in ("06:40:35", "06:36:55", "06:39:00")]
        self.assertEqual(a, ["COOLDOWN", "PREP", "HOT LAP"])
        self.assertEqual(b, ["HOT LAP", "COOLDOWN", "PREP"])


class StatusRegressionTest(unittest.TestCase):
    """DNF / garage / pit lane on the real 2026 race are unchanged (see also test_pitlane)."""

    def test_japan_race_statuses(self):
        r = Replay(RACE, "2026-03-29")
        s = r.state("06:08:30")
        self.assertEqual(sorted(d["tla"] for d in s["drivers"].values() if d["in_garage"]), ["STR"])
        self.assertEqual(sorted(d["tla"] for d in s["drivers"].values() if d["dnf"]), ["BEA"])
        s = r.state("05:46:00")
        self.assertFalse(tla(s, "LIN")["in_pit"])                                     # stuck InPit, racing
        self.assertIsNone(tla(s, "LIN")["lap_state"])                                 # no lap state in a race


if __name__ == "__main__":
    unittest.main()
