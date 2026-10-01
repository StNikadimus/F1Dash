"""Session-aware dashboard: qualifying (Q1/Q2/Q3) and practice, on REAL timing data.

Fixtures (tests/fixtures, MIT - see LICENSE-f1-telemetry-samples.txt): recordings of the official
live timing of the 2026 Japanese GP qualifying and FP3 at Suzuka. Nothing here is synthetic:
every expected value below was read from these recordings (they are lossy - some messages are
missing - which the lap tracker must survive without inventing anything).

Run:  python -m unittest discover tests
"""
import json
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from server.feedstate import FeedState  # noqa: E402
from server.models import Availability  # noqa: E402
from server.normalizer import Normalizer  # noqa: E402
from server.openf1 import RefEvents  # noqa: E402
from server.session_phases import build_timeline  # noqa: E402
from server.sources.replay import load_file  # noqa: E402
from server.sync import SyncManager, parse_voyo_sample  # noqa: E402

FIX = Path(__file__).resolve().parent / "fixtures"
QUALI = FIX / "suzuka-2026-qualifying.json.gz"
FP3 = FIX / "suzuka-2026-fp3.json.gz"
DAY = "2026-03-28"


def ms(hms: str) -> float:
    return datetime.fromisoformat(f"{DAY}T{hms}+00:00").timestamp() * 1000


def dt(hms: str) -> datetime:
    return datetime.fromisoformat(f"{DAY}T{hms}+00:00")


class Replay:
    """The state at a moment, built two ways: (a) fresh - every message up to T applied in order;
    (b) the VOD path the dashboard uses - checkpoint restore + timeline, seeking both ways."""

    def __init__(self, path: Path):
        self.events = load_file(path)
        self.timeline = build_timeline(self.events)

    def fresh(self, hms: str) -> dict:
        fs = FeedState()
        t = ms(hms)
        for e in self.events:
            et = e.t.timestamp() * 1000
            if et > t:
                break
            fs.apply(e.topic, e.data, e.snap, et)
        return self.build(fs, hms)

    def build(self, feed, hms: str) -> dict:
        nz = Normalizer()
        nz.timeline = self.timeline
        st = nz.build(feed, dt(hms), 1.0, Availability(), rc_until=dt(hms))
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

        def at(hms: str) -> dict:
            t = ms(hms)
            src.ensure(t, lambda tp, d, m, snap: tl.ingest(tp, d, m, m, snap), tl)
            tl.advance(t)
            return self.build(tl.feed, hms)
        return at


def board(st: dict) -> list[dict]:
    return [st["drivers"][n] for n in st["order"]]


def tla(st: dict, name: str) -> dict:
    return next(d for d in st["drivers"].values() if d["tla"] == name)


def lap_ms(v):
    m, s = (v.split(":") + [None])[:2] if ":" in v else ("0", v)
    return int(m) * 60000 + round(float(s) * 1000)


# ---------------------------------------------------------------------------
class QualifyingTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.r = Replay(QUALI)

    def assert_ranking(self, st):
        rows = board(st)
        self.assertEqual([d["position"] for d in rows], list(range(1, len(rows) + 1)))
        timed = [d for d in rows if not d["out_phase"] and not d["no_time"]]
        self.assertEqual(rows[:len(timed)], timed, "cars with a time first")
        times = [lap_ms(d["best_lap"]["value"]) for d in timed]
        self.assertEqual(times, sorted(times), "ranked by best valid lap")
        p1 = times[0] if times else None
        for d, t in zip(timed, times):
            self.assertEqual(d["gap"], None if t == p1 and d is timed[0] else f"+{(t - p1) / 1000:.3f}")
        for d in rows[len(timed):]:
            self.assertIsNone(d["gap"])
        return rows

    def test_phases_from_the_timing_clock(self):
        tl = self.r.timeline
        self.assertEqual([p.id for p in tl.phases], ["Q1", "Q2", "Q3"])
        # official lengths from the clock (Q3 at Suzuka 2026 was 13:00 - never assumed)
        self.assertEqual([p.duration for p in tl.phases], [18 * 60000, 15 * 60000, 13 * 60000])
        m = {x.id: x.ms for x in tl.markers}
        self.assertEqual(m["Q1_END"], ms("06:18:00.011"))          # ExtrapolatedClock 00:00:00
        self.assertEqual(m["Q2_START"], ms("06:25:00.012"))        # 14:59 posted at 06:25:01.012
        self.assertEqual(m["Q2_END"], ms("06:40:00.011"))
        self.assertEqual(m["Q3_START"], ms("06:47:00.010"))
        self.assertEqual(m["Q3_END"], ms("07:00:00.011"))
        self.assertEqual(m["Q1_START"], ms("06:00:00.011"))        # recording began in Q1: from the 0:00 end
        self.assertTrue(all(x.sync for x in tl.markers if x.kind in ("start", "end")))

    def test_q1_leaderboard(self):
        st = self.r.fresh("06:10:00")
        self.assertEqual((st["session"]["phase"], st["session"]["title"]), ("Q1", "QUALIFYING — Q1"))
        rows = self.assert_ranking(st)
        self.assertEqual([(d["tla"], d["best_lap"]["value"], d["gap"]) for d in rows[:4]],
                         [("HAM", "1:30.628", None), ("BEA", "1:31.421", "+0.793"),
                          ("COL", "1:31.458", "+0.830"), ("OCO", "1:31.466", "+0.838")])
        self.assertTrue(rows[0]["best_lap"]["overall_best"])
        self.assertFalse(any(d["out_phase"] for d in rows))                  # everybody runs in Q1

    def test_no_time_driver_and_new_phase_starts_empty(self):
        st = self.r.fresh("06:24:30")                                         # Q2 begun, nobody out yet
        self.assertEqual(st["session"]["phase"], "Q2")
        rows = self.assert_ranking(st)
        running = [d for d in rows if not d["out_phase"]]
        self.assertEqual(len(running), 16)
        self.assertTrue(all(d["no_time"] and d["best_lap"]["value"] is None for d in running),
                        "Q1 times must not stay on the Q2 leaderboard")
        out = [d for d in rows if d["out_phase"]]
        self.assertEqual(sorted(d["tla"] for d in out), ["ALB", "ALO", "BEA", "BOT", "PER", "STR"])
        self.assertTrue(all(d["out_phase"] == "Q1" for d in out))
        self.assertEqual(rows[-6:], out, "knocked-out cars below the running ones")

    def test_q2_leaderboard(self):
        st = self.r.fresh("06:35:00")
        rows = self.assert_ranking(st)
        self.assertEqual((rows[0]["tla"], rows[0]["best_lap"]["value"]), ("RUS", "1:30.117"))
        q1 = self.r.fresh("06:20:00")
        self.assertNotEqual(tla(q1, "RUS")["best_lap"]["value"], "1:30.117")   # a Q2 time, not Q1's

    def test_q3_leaderboard_and_knocked_out_order(self):
        st = self.r.fresh("06:58:00")
        self.assertEqual(st["session"]["phase"], "Q3")
        rows = self.assert_ranking(st)
        self.assertEqual(rows[0]["tla"], "LEC")
        self.assertEqual(rows[0]["best_lap"]["value"], "1:29.434")
        outs = [d["out_phase"] for d in rows if d["out_phase"]]
        self.assertEqual(outs, ["Q2"] * 6 + ["Q1"] * 6)                       # Q2 eliminations above Q1's
        self.assertEqual(sorted(d["tla"] for d in rows if d["out_phase"] == "Q2"),
                         ["COL", "HUL", "LAW", "OCO", "SAI", "VER"])

    def test_invalid_lap_is_never_the_best_lap(self):
        # 06:51:10.382 LIN sets 1:31.537 (fastest in Q3 so far); 06:51:12.279 race control:
        # "CAR 41 (LIN) TIME 1:31.537 DELETED - TRACK LIMITS AT TURN 13 LAP 18"; the feed never
        # corrects BestLapTimes - the deletion is applied from the race control message
        before = self.r.fresh("06:51:11")
        self.assertEqual((board(before)[0]["tla"], board(before)[0]["best_lap"]["value"]), ("LIN", "1:31.537"))
        after = self.r.fresh("06:51:13")
        lin = tla(after, "LIN")
        self.assertTrue(lin["no_time"])
        self.assertIsNone(lin["best_lap"]["value"])
        self.assertTrue(lin["best_deleted"])
        self.assertNotEqual(board(after)[0]["tla"], "LIN")
        self.assertEqual(lin["rc"]["deleted_times"], ["1:31.537"])
        # his next completed lap (1:46.227 at 06:52:56.728) is his valid Q3 best
        later = self.r.fresh("06:53:00")
        self.assertEqual(tla(later, "LIN")["best_lap"]["value"], "1:46.227")
        self.assertFalse(tla(later, "LIN")["no_time"])
        self.assert_ranking(later)

    def test_current_lap_and_current_sector(self):
        # LIN: line at 06:51:10.382 (lap 18 done), S1 33.x at 06:51:48.311 (Value), S2 at 06:52:33.756,
        # line again at 06:52:56.728
        d = tla(self.r.fresh("06:51:30"), "LIN")
        self.assertEqual((d["lap_now"], d["sector_now"]), (19, 1))
        self.assertEqual(d["lap_start_ms"], ms("06:51:10.382"))
        self.assertEqual(d["sector_start_ms"], ms("06:51:10.382"))
        self.assertEqual(d["last_lap"]["value"], None)                        # the deleted lap is not LAST
        self.assertTrue(d["last_deleted"])
        d = tla(self.r.fresh("06:51:49"), "LIN")                              # just crossed S1 -> S2, not S1
        self.assertEqual((d["lap_now"], d["sector_now"]), (19, 2))
        self.assertEqual(d["sector_start_ms"], ms("06:51:48.311"))
        self.assertEqual(d["last_sector"], {"n": 1, "value": "37.724"})
        d = tla(self.r.fresh("06:52:40"), "LIN")
        self.assertEqual((d["lap_now"], d["sector_now"], d["sector_start_ms"]), (19, 3, ms("06:52:33.756")))
        d = tla(self.r.fresh("06:52:57"), "LIN")                              # lap 19 completed -> lap 20
        self.assertEqual((d["lap_now"], d["sector_now"], d["lap_start_ms"]), (20, 1, ms("06:52:56.728")))
        self.assertEqual(d["last_lap"]["value"], "1:46.227")
        self.assertIn([ms("06:52:56.728"), 19], d["lap_marks"])
        # a car in the garage drives no lap
        self.assertTrue(all(x["lap_now"] is None for x in board(self.r.fresh("06:58:00")) if x["in_garage"]))

    def test_part_change_does_not_move_cars_in_the_garage(self):
        # every car gets its values cleared when Q2 begins (06:23:59 / 06:24:01) - that is no
        # "activity": cars sitting InPit stay in the pit (not on the map), and nobody starts a lap
        st = self.r.fresh("06:24:30")
        for d in board(st):
            self.assertIsNone(d["lap_now"], d["tla"])
        garage = [d["tla"] for d in board(st) if d["in_garage"]]
        self.assertGreaterEqual(len(garage), 10)
        for d in board(st):
            if d["in_pit"]:
                self.assertTrue(d["in_garage"], d["tla"])

    def test_phase_transitions_and_states(self):
        s = self.r.fresh("06:23:59")["session"]
        self.assertEqual((s["phase"], s["phase_state"]), ("Q1", "ENDED"))
        s = self.r.fresh("06:24:10")["session"]
        self.assertEqual((s["phase"], s["phase_duration_ms"]), ("Q2", 900000))
        s = self.r.fresh("06:30:00")["session"]
        self.assertEqual((s["phase"], s["phase_state"]), ("Q2", "RUNNING"))
        s = self.r.fresh("06:46:30")["session"]
        self.assertEqual((s["phase"], s["phase_duration_ms"]), ("Q3", 780000))

    def test_timeline_has_nothing_from_the_future(self):
        st = self.r.fresh("06:30:00")
        self.assertEqual([m["id"] for m in st["timeline"]["markers"]], ["Q1_START", "CHQ1", "Q1_END", "Q2_START"])
        ph = {p["id"]: p for p in st["timeline"]["phases"]}
        self.assertIsNone(ph["Q2"]["end_ms"])                                # Q2 still running
        self.assertIsNone(ph["Q3"]["start_ms"])
        self.assertEqual(ph["Q3"]["duration_ms"], 780000)                    # the official length is known
        self.assertNotIn("runs", ph["Q2"])
        self.assertEqual(ph["Q1"]["end_ms"], ms("06:18:00.011"))

    def test_topic_order_at_the_same_moment_does_not_matter(self):
        # LapSeries may arrive before TimingData for the same crossing (separate messages): the
        # completed lap must still be filed with how it started (an out lap stays an out lap)
        from server.feedstate import FeedState as FS
        evs = sorted(self.r.events, key=lambda e: (e.t, e.topic != "LapSeries"))
        for hms in ("06:35:00", "06:58:00"):
            fs = FS()
            for e in evs:
                if e.t > dt(hms):
                    break
                fs.apply(e.topic, e.data, e.snap, e.t.timestamp() * 1000)
            other = self.r.build(fs, hms)
            ref = self.r.fresh(hms)
            self.assertEqual([(d["tla"], d["best_lap"]["value"], d["no_time"]) for d in board(other)],
                             [(d["tla"], d["best_lap"]["value"], d["no_time"]) for d in board(ref)], hms)
            normal = FS()
            for e in self.r.events:
                if e.t > dt(hms):
                    break
                normal.apply(e.topic, e.data, e.snap, e.t.timestamp() * 1000)
            for num, st in (normal.get("_Laps") or {}).items():
                self.assertEqual(st["hist"], (fs.get("_Laps") or {})[num]["hist"], (hms, num))

    def test_snapshot_of_the_twin_topic_is_not_activity(self):
        # connecting in the middle of Q3: TimingData and TimingDataF1 arrive as snapshots - no car
        # may get a lap / an elimination part / "activity" from them
        from server.feedstate import FeedState as FS
        full = FS()
        for e in self.r.events:
            if e.t > dt("06:50:00"):
                break
            full.apply(e.topic, e.data, e.snap, e.t.timestamp() * 1000)
        fs = FS()
        t = ms("06:50:00")
        for topic in ("DriverList", "SessionInfo", "SessionData", "TimingData"):
            fs.apply(topic, full.get(topic), True, t)
        fs.apply("TimingDataF1", full.get("TimingData"), True, t)
        self.assertEqual(fs.get("_Laps") or {}, {})
        self.assertFalse(any(v[1] for v in (fs.get("_InPitSince") or {}).values()))
        st = self.r.build(fs, "06:50:00")
        self.assertTrue(all(d["lap_now"] is None for d in board(st)))
        # knocked-out cars: the part is not tracked here - it comes from their classification
        self.assertEqual(sorted({d["out_phase"] for d in board(st) if d["out_phase"]}), ["Q1", "Q2"])

    def test_seek_backwards_across_q1_q2_q3_is_deterministic(self):
        at = self.r.vod()
        a = at("06:58:00")                                                    # Q3
        b = at("06:10:00")                                                    # back to Q1
        c = at("06:35:00")                                                    # Q2
        a2 = at("06:58:00")                                                   # Q3 again
        dump = lambda x: json.dumps(x, sort_keys=True)
        self.assertEqual(dump(a), dump(a2))
        self.assertEqual(dump(b), dump(self.r.fresh("06:10:00")), "no data from the future after a seek back")
        self.assertEqual(dump(c), dump(self.r.fresh("06:35:00")))
        self.assertEqual(dump(a), dump(self.r.fresh("06:58:00")))
        self.assertEqual((b["session"]["phase"], c["session"]["phase"], a["session"]["phase"]), ("Q1", "Q2", "Q3"))
        # the deleted lap: before / after the race control message, both directions
        self.assertEqual(tla(at("06:51:11"), "LIN")["best_lap"]["value"], "1:31.537")
        self.assertTrue(tla(at("06:51:13"), "LIN")["no_time"])
        self.assertEqual(tla(at("06:51:11"), "LIN")["best_lap"]["value"], "1:31.537")


# ---------------------------------------------------------------------------
class PracticeTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.r = Replay(FP3)

    def test_session_start_end_from_the_clock(self):
        tl = self.r.timeline
        self.assertEqual([p.id for p in tl.phases], ["FP3"])
        p = tl.phases[0]
        self.assertEqual(p.duration, 3600000)
        self.assertEqual(p.end, ms("03:30:00.011"))
        self.assertEqual(p.start, ms("02:30:00.011"))                          # SessionStatus Started 02:30:00.199
        self.assertEqual([m.label for m in tl.markers if m.sync], ["SESSION START", "SESSION END"])
        self.assertEqual(self.r.fresh("03:00:00")["session"]["phase_state"], "RUNNING")
        self.assertEqual(self.r.fresh("03:31:00")["session"]["phase_state"], "ENDED")
        self.assertEqual(self.r.fresh("03:00:00")["session"]["title"], "FP3 — BEST LAP")

    def test_best_lap_ranking(self):
        st = self.r.fresh("03:10:00")
        rows = board(st)
        timed = [d for d in rows if not d["no_time"]]
        times = [lap_ms(d["best_lap"]["value"]) for d in timed]
        self.assertEqual(times, sorted(times))
        self.assertEqual((rows[0]["tla"], rows[0]["best_lap"]["value"]), ("ANT", "1:29.929"))
        self.assertEqual((rows[1]["tla"], rows[1]["gap"]), ("LEC", "+0.607"))
        early = self.r.fresh("02:40:00")                                      # no final result early on
        self.assertEqual(board(early)[0]["best_lap"]["value"], "1:32.283")
        self.assertNotIn("1:29.929", [d["best_lap"]["value"] for d in early["drivers"].values()])

    def test_last_current_lap_and_sector(self):
        # SAI (55): line 03:03:58.579 (1:34.591), S1 PreviousValue 03:04:35.548 (its Value message is
        # missing: the sector is complete, its end time unknown), S2 Value 03:05:15.068, line 03:05:33.052
        d = tla(self.r.fresh("03:04:30"), "SAI")
        self.assertEqual((d["lap_now"], d["sector_now"], d["last_lap"]["value"]), (13, 1, "1:34.591"))
        self.assertEqual(d["lap_start_ms"], ms("03:03:58.579"))
        d = tla(self.r.fresh("03:04:40"), "SAI")
        self.assertEqual((d["sector_now"], d["sector_start_ms"]), (2, None))  # no invented sector start
        d = tla(self.r.fresh("03:05:20"), "SAI")
        self.assertEqual((d["sector_now"], d["sector_start_ms"]), (3, ms("03:05:15.068")))
        d = tla(self.r.fresh("03:05:34"), "SAI")
        self.assertEqual((d["lap_now"], d["sector_now"], d["last_lap"]["value"]), (14, 1, "1:34.481"))

    def test_deleted_lap_named_by_number(self):
        # 03:27:27 "CAR 63 (RUS) LAP DELETED - TRACK LIMITS AT TURN 16 LAP 16": the message names the
        # lap, not the time - lap 16 of RUS (2:07.816, completed 03:24:47.043) is deleted
        before = tla(self.r.fresh("03:27:20"), "RUS")
        after = tla(self.r.fresh("03:27:40"), "RUS")
        self.assertEqual((before["last_lap"]["value"], before["last_deleted"]), ("2:07.816", False))
        self.assertEqual(after["rc"]["deleted_lap_numbers"], [16])
        self.assertTrue(after["last_deleted"])
        self.assertNotEqual(after["last_lap"]["value"], "2:07.816")
        self.assertNotEqual(after["best_lap"]["value"], "2:07.816")

    def test_seek_backwards(self):
        at = self.r.vod()
        dump = lambda x: json.dumps(x, sort_keys=True)
        a = at("03:20:00")
        b = at("02:45:00")
        a2 = at("03:20:00")
        self.assertEqual(dump(a), dump(a2))
        self.assertEqual(dump(b), dump(self.r.fresh("02:45:00")))
        self.assertEqual(dump(a), dump(self.r.fresh("03:20:00")))


# ---------------------------------------------------------------------------
def sample(pb, paused=True, ts=0.0, asset="voyo-media:q1"):
    return parse_voyo_sample({"playback_time": pb, "paused": paused, "timestamp_local": ts, "ready_state": 4,
                              "asset": asset}, ts, ts)


class SessionSyncTest(unittest.TestCase):
    """Time remaining / elapsed and phase markers -> the same offset the video really has."""

    @classmethod
    def setUpClass(cls):
        cls.q = build_timeline(load_file(QUALI))
        cls.fp = build_timeline(load_file(FP3))

    def setUp(self):
        self.mono = 0.0
        self.K = ms("02:00:00") / 1000 - 7.25                  # video 0:00 (unknown to the system)

    def make(self, tl, name="Qualifying", store=None, key=11249):
        s = SyncManager({"enabled": True}, voyo_enabled=True, legacy_delay=0, source_speed=1.0,
                        store_path=store, vod=True)
        ref = RefEvents(source="none")
        ref.timeline = tl
        s.initialize({"session_key": key, "session_name": name, "session_type": name.split()[-1],
                      "date_start": "2026-03-28T06:00:00+00:00"}, ref)
        return s

    def at(self, s, f1_ms, paused=True):
        self.mono += 1.0
        pb = f1_ms / 1000 - self.K
        s.update(sample(pb, paused=paused, ts=self.mono), 0)
        s.target(self.mono, 0)
        return pb

    def remaining(self, s, phase, f1, secs, mode="remaining"):
        self.at(s, f1)
        s.capture(self.mono, 0)
        return s.add_clock_anchor(phase, mode, secs, self.mono, 0)

    def test_q1_q2_q3_time_remaining(self):
        for phase, end, rem in (("Q1", "06:18:00.011", 60), ("Q2", "06:40:00.011", 7 * 60 + 32),
                                ("Q3", "07:00:00.011", 12 * 60 + 59)):
            s = self.make(self.q)
            msg = self.remaining(s, phase, ms(end) - rem * 1000, rem)
            self.assertIn("MEDIUM", msg, phase)
            st = s.get_state(self.mono, 0, None)
            self.assertAlmostEqual(st["offsetSeconds"], self.K, places=3)
            self.assertEqual(st["method"], f"{phase} Time Remaining")
            self.assertEqual((st["health"], st["healthError"]), ("MEDIUM", "±0.5–1 s"))   # stated, not measured
            self.assertIsNone(st["errorSeconds"])

    def test_elapsed_gives_the_same_result(self):
        # Q3 was 13:00 long: 07:32 remaining == 05:28 elapsed
        s1, s2 = self.make(self.q), self.make(self.q)
        f1 = ms("07:00:00.011") - (7 * 60 + 32) * 1000
        self.remaining(s1, "Q3", f1, 7 * 60 + 32)
        self.remaining(s2, "Q3", f1, 5 * 60 + 28, mode="elapsed")
        self.assertEqual(s1.mapping.offset, s2.mapping.offset)
        self.assertEqual(s2.get_state(self.mono, 0, None)["method"], "Q3 Time Elapsed")

    def test_practice_time_remaining_and_elapsed(self):
        s = self.make(self.fp, "Practice 3")
        self.assertIn("MEDIUM", self.remaining(s, "FP3", ms("03:30:00.011") - (42 * 60 + 17) * 1000, 42 * 60 + 17))
        self.assertAlmostEqual(s.mapping.offset, self.K, places=3)
        s = self.make(self.fp, "Practice 3")
        self.remaining(s, "FP3", ms("02:30:00.011") + (17 * 60 + 43) * 1000, 17 * 60 + 43, mode="elapsed")
        self.assertAlmostEqual(s.mapping.offset, self.K, places=3)

    def test_impossible_clock_readings_are_refused(self):
        s = self.make(self.q)
        self.at(s, ms("06:24:30"))
        self.assertIn("stood still", s.add_clock_anchor("Q2", "remaining", 15 * 60, self.mono, 0))   # before the start
        self.assertIn("13:00", s.add_clock_anchor("Q3", "elapsed", 14 * 60, self.mono, 0))
        self.assertIn("never showed", s.add_clock_anchor("Q2", "remaining", 16 * 60, self.mono, 0))
        self.assertIn("END marker", s.add_clock_anchor("Q2", "remaining", 0, self.mono, 0))
        self.assertEqual(s.mapping.confidence, "UNSYNCED")                     # nothing invented

    def test_phase_end_marker_then_validation_is_high(self):
        s = self.make(self.q)
        self.at(s, ms("06:40:00.011"))                                        # paused on 0:00 of Q2
        self.assertIn("MEDIUM", s.add_marker_anchor("Q2_END", self.mono, 0))
        self.assertAlmostEqual(s.mapping.offset, self.K, places=3)
        st = s.get_state(self.mono, 0, None)
        self.assertEqual(st["method"], "Q2 END Marker")
        self.assertEqual(st["healthError"], "±0.1–0.3 s")                     # paused on the event
        # an independent check (Q3 time remaining) agrees -> HIGH
        msg = self.remaining(s, "Q3", ms("07:00:00.011") - 300_000, 300)
        self.assertIn("HIGH", msg)
        st = s.get_state(self.mono, 0, None)
        self.assertEqual(st["method"], "Q2 END Marker + Q3 Time Remaining")
        self.assertEqual(st["anchorsIndependent"], 2)

    def test_practice_end_marker(self):
        s = self.make(self.fp, "Practice 3")
        self.at(s, ms("03:30:00.011"))
        self.assertIn("MEDIUM", s.add_marker_anchor("FP3_END", self.mono, 0))
        self.assertAlmostEqual(s.mapping.offset, self.K, places=3)
        self.assertIn("not in this session", s.add_marker_anchor("Q2_END", self.mono, 0))

    def test_saved_sync_is_restored(self):
        with tempfile.TemporaryDirectory() as d:
            store = Path(d) / "sync.json"
            s = self.make(self.q, store=store)
            self.at(s, ms("06:40:00.011"))
            s.add_marker_anchor("Q2_END", self.mono, 0)
            s2 = self.make(self.q, store=store)
            self.at(s2, ms("06:50:00"))                                      # same video reopened
            st = s2.get_state(self.mono, 0, None)
            self.assertEqual(st["method"], "Q2 END Marker")
            self.assertTrue(st["anchors"][0]["restored"])
            self.assertAlmostEqual(s2.mapping.offset, self.K, places=3)

    def test_seek_after_sync_follows_the_video(self):
        s = self.make(self.q)
        self.at(s, ms("06:40:00.011"))
        s.add_marker_anchor("Q2_END", self.mono, 0)
        for hms, phase in (("06:10:00", "Q1"), ("06:55:00", "Q3"), ("06:30:00", "Q2")):
            pb = self.at(s, ms(hms))                                          # seek (paused there)
            t = s.target(self.mono, 0)
            self.assertAlmostEqual(t.ms, (pb + self.K) * 1000, delta=1)
            st = s.get_state(self.mono, 0, None)
            self.assertEqual(st["sessionTimeline"]["current"]["phase"], phase)
            self.assertEqual(st["sessionKind"], "qualifying")

    def test_menu_structure_has_no_spoilers(self):
        s = self.make(self.q)
        st = s.get_state(self.mono, 0, None)                                  # not synced
        kinds = {m["kind"] for m in st["sessionTimeline"]["markers"]}
        self.assertLessEqual(kinds, {"start", "end"})
        self.assertEqual([p["duration_ms"] for p in st["sessionTimeline"]["phases"]], [1080000, 900000, 780000])



class NamingAndLiveTest(unittest.TestCase):
    def test_session_types(self):
        from server.session_phases import phase_label, phase_prefix, session_kind
        self.assertEqual(session_kind("Race", "Race"), "race")
        self.assertEqual(session_kind("Race", "Sprint"), "race")
        self.assertEqual(session_kind("Qualifying", "Qualifying"), "qualifying")
        self.assertEqual(session_kind("Qualifying", "Sprint Qualifying"), "qualifying")
        self.assertEqual(session_kind("Qualifying", "Sprint Shootout"), "qualifying")
        self.assertEqual(session_kind("Practice", "Practice 2"), "practice")
        self.assertEqual(phase_label("qualifying", "Sprint Qualifying", 2), "SQ2")
        self.assertEqual(phase_label("qualifying", "Qualifying", 3), "Q3")
        self.assertEqual(phase_label("practice", "Practice 1", None), "FP1")
        self.assertEqual(phase_prefix("race", "Sprint"), "SPRINT")
        # a race keeps its board / sync options: no phases
        self.assertTrue(build_timeline([], "Race", "Race").empty())

    def test_live_feed_builds_the_same_structure(self):
        """Live / replay: the phases come from the messages as they arrive (never ahead)."""
        from server.sync import FeedRefCollector
        c = FeedRefCollector()
        cut = ms("06:30:00")
        for e in load_file(QUALI):
            t = e.t.timestamp() * 1000
            if t > cut:
                break
            c.observe(e.topic, e.data, t, e.snap)
        tl = c.timeline()
        self.assertEqual([p.id for p in tl.phases], ["Q1", "Q2"])            # Q3 not known yet
        self.assertEqual(tl.phase("Q2").duration, 900000)
        self.assertIsNone(tl.phase("Q2").end)                                  # still running
        self.assertEqual(tl.remaining_to_f1("Q2", 7 * 60000 + 32000)[0], ms("06:32:28.012"))


if __name__ == "__main__":
    unittest.main()
