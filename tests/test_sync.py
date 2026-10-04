"""Tests for the timestamped timeline and the VOYO sync engine.

Run:  python -m unittest discover tests
"""
import json
import math
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from server.sources.replay import load_file  # noqa: E402
from server.sync import SyncManager, parse_voyo_sample  # noqa: E402
from server.telemetry import encode_z  # noqa: E402
from server.timeline import INF, Timeline  # noqa: E402

SAMPLE = Path(__file__).resolve().parent.parent / "data" / "recordings" / "sample-2026-japan-race.json.gz"
T0 = 1_780_000_000_000.0          # arbitrary epoch ms


def dump(tl: Timeline) -> str:
    return json.dumps(tl.feed.topics, sort_keys=True, default=str)


def pos_msg(t_ms: float, x: int) -> dict:
    from datetime import datetime, timezone
    iso = datetime.fromtimestamp(t_ms / 1000, timezone.utc).isoformat().replace("+00:00", "Z")
    return {"Position": [{"Timestamp": iso, "Entries": {"1": {"Status": "OnTrack", "X": x, "Y": 1, "Z": 1}}}]}


class TimelineTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.events = load_file(SAMPLE)

    def _fill(self, tl: Timeline, upto: int):
        for e in self.events[:upto]:
            ms = e.t.timestamp() * 1000
            tl.ingest(e.topic, e.data, ms, ms, e.snap)

    def test_state_at_time_x_is_reproducible(self):
        n = min(len(self.events), 6000)
        mid = self.events[n // 2].t.timestamp() * 1000
        end = self.events[n - 1].t.timestamp() * 1000
        # A: walk forward in 250 ms steps to "mid"
        a = Timeline(buffer_seconds=3600)
        self._fill(a, n)
        t = self.events[0].t.timestamp() * 1000
        while t < mid:
            t = min(mid, t + 250)
            a.advance(t)
        forward = dump(a)
        # B: one jump straight to "mid"
        b = Timeline(buffer_seconds=3600)
        self._fill(b, n)
        b.advance(mid)
        self.assertEqual(forward, dump(b))
        # A again: to the end and back (checkpoint rebuild) must give the same state
        a.advance(end)
        self.assertNotEqual(forward, dump(a))
        res = a.advance(mid)
        self.assertTrue(res.discontinuity)
        self.assertEqual(forward, dump(a))

    def test_events_after_target_are_not_shown(self):
        tl = Timeline()
        tl.ingest("TrackStatus", {"Status": "1", "Message": "AllClear"}, T0, T0)
        tl.ingest("TrackStatus", {"Status": "6", "Message": "VSCDeployed"}, T0 + 10_000, T0 + 10_000)
        tl.advance(T0 + 5_000)
        self.assertEqual(tl.feed.get("TrackStatus")["Status"], "1")      # VSC not yet on the video
        tl.advance(T0 + 10_000)
        self.assertEqual(tl.feed.get("TrackStatus")["Status"], "6")
        tl.advance(T0 + 2_000)                                             # seek back
        self.assertEqual(tl.feed.get("TrackStatus")["Status"], "1")

    def test_positions_use_their_own_timestamps_and_late_samples_are_kept(self):
        tl = Timeline()
        tl.ingest("TrackStatus", {"Status": "1"}, T0 + 5000, T0 + 5000)
        tl.ingest("Position.z", encode_z(pos_msg(T0 + 1000, 10)), T0 + 6000, T0 + 6000)
        res = tl.advance(T0 + 500)
        self.assertEqual(res.released_positions, [])
        res = tl.advance(T0 + 1200)
        self.assertEqual(len(res.released_positions), 1)
        self.assertEqual(res.released_positions[0][0], T0 + 1000)
        tl.advance(T0 + 7000)
        # an archive sample arriving 5 s late is applied and still streamed
        tl.ingest("Position.z", encode_z(pos_msg(T0 + 2000, 20)), None, T0 + 7000, origin="archive")
        res = tl.advance(T0 + 7100)
        self.assertEqual([r[1]["cars"][0][1] for r in res.released_positions], [20])

    def test_buffer_trim_keeps_unapplied_events(self):
        tl = Timeline(buffer_seconds=10)
        for i in range(120):
            tl.ingest("LapCount", {"CurrentLap": i}, T0 + i * 1000, T0 + i * 1000)
        tl.advance(T0 + 30_000)                         # "paused video": far behind the data
        tl.advance(T0 + 30_000)
        self.assertEqual(tl.feed.get("LapCount")["CurrentLap"], 30)
        self.assertTrue(all(e.event_ms > T0 + 30_000 for e in tl.events[tl.idx:]))
        self.assertEqual(len(tl.events) - tl.idx, 89)   # nothing newer than the target was dropped
        tl.advance(T0 + 119_000)
        self.assertEqual(tl.feed.get("LapCount")["CurrentLap"], 119)

    def test_position_lookahead_streams_each_sample_once(self):
        tl = Timeline()
        for i in range(10):
            tl.ingest("Position.z", encode_z(pos_msg(T0 + i * 500, i)), T0 + i * 500, T0 + i * 500)
        tl.advance(T0, pos_lookahead_ms=0)                              # first checkpoint (buffer start)
        res = tl.advance(T0 + 1000, pos_lookahead_ms=2000)          # samples up to T0+3000 streamed early
        self.assertEqual([r[1]["cars"][0][1] for r in res.released_positions], [1, 2, 3, 4, 5, 6])
        self.assertEqual(tl.positions.latest["1"][1], 2)               # ...but the state is at the target
        res = tl.advance(T0 + 2000, pos_lookahead_ms=2000)
        self.assertEqual([r[1]["cars"][0][1] for r in res.released_positions], [7, 8])
        res = tl.advance(T0 + 500, pos_lookahead_ms=2000)            # seek back: refill around the target
        self.assertTrue(res.discontinuity)
        refill = [r[1]["cars"][0][1] for r in res.replay_positions + res.released_positions]
        self.assertEqual(refill[:2], [0, 1])
        self.assertIn(5, refill)

    def test_live_mode_applies_everything(self):
        tl = Timeline()
        tl.ingest("LapCount", {"CurrentLap": 3}, T0, T0)
        tl.advance(INF)
        self.assertEqual(tl.feed.get("LapCount")["CurrentLap"], 3)
        tl.ingest("LapCount", {"CurrentLap": 4}, T0 - 50, T0)          # out of order: still applied
        tl.advance(INF)
        self.assertEqual(tl.feed.get("LapCount")["CurrentLap"], 4)


def sample(pb, paused=False, ts=None, asset="voyo|9184", **kw):
    body = {"playback_time": pb, "paused": paused, "playback_rate": 1.0, "ready_state": 4, "asset": asset,
            "seekable_end": kw.pop("seekable_end", None), **kw}
    return parse_voyo_sample(body, 0.0, ts)


FIX = json.loads((Path(__file__).resolve().parent / "fixtures" / "openf1_2026_japan.json").read_text())


def _ms(iso):
    from server.telemetry import parse_utc
    return parse_utc(iso).timestamp() * 1000


START_MS = _ms("2026-03-29T05:14:02.078Z")      # SESSION STARTED = lights out (OpenF1 race_control)


class LiveSyncTest(unittest.TestCase):
    """VOYO live stream (not VOD)."""

    def make(self, **cfg):
        base = {"enabled": True, "mode": "AUTO", "broadcast_delay_seconds": 5.0, "auto_drift_correction": True}
        base.update(cfg)
        return SyncManager(base, voyo_enabled=True, legacy_delay=0.0, source_speed=1.0, store_path=None)

    def test_estimate_is_low_and_follows_the_video_clock(self):
        s = self.make()
        s.update(sample(2534.4, ts=100.0), T0)
        t = s.target(100.0, T0)
        self.assertEqual(t.mode, "VOYO")
        self.assertAlmostEqual((T0 - t.ms) / 1000, 5.0, places=3)
        st = s.get_state(100.0, T0, Timeline())
        self.assertEqual(st["confidence"], "LOW")
        self.assertIsNone(st["absoluteTime"])                  # never an exact-looking time when LOW
        s.update(sample(2535.4, ts=101.0), T0 + 1000)
        self.assertAlmostEqual(s.target(101.0, T0 + 1000).ms - t.ms, 1000, delta=1)

    def test_pause_freezes_and_seek_jumps(self):
        s = self.make()
        s.update(sample(100.0, ts=10.0), T0)
        s.target(10.0, T0)
        s.update(sample(101.0, paused=True, ts=11.0), T0 + 1000)
        a = s.target(11.0, T0 + 1000)
        s.update(sample(101.0, paused=True, ts=12.5), T0 + 2500)
        b = s.target(12.9, T0 + 2900)
        self.assertEqual((a.ms, b.rate), (b.ms, 0.0))
        s.update(sample(71.0, ts=13.0, events=[{"type": "seeking", "pb": 71.0}]), T0 + 3000)
        c = s.target(13.0, T0 + 3000)
        self.assertTrue(c.jump)
        self.assertAlmostEqual(b.ms - c.ms, 30_000, delta=5)

    def test_clock_loss_holds_the_delay(self):
        s = self.make()
        s.update(sample(10.0, ts=0.0), T0)
        a = s.target(0.0, T0)
        b = s.target(5.0, T0 + 5000)
        self.assertEqual(b.mode, "DELAY")
        self.assertAlmostEqual(b.ms - a.ms, 5000, delta=1)

    def test_delay_mode_without_voyo(self):
        s = SyncManager({"mode": "AUTO"}, voyo_enabled=False, legacy_delay=0.0, source_speed=1.0)
        self.assertEqual(s.target(0.0, T0).mode, "LIVE")
        s2 = SyncManager({"mode": "DELAY", "broadcast_delay_seconds": 3}, voyo_enabled=False,
                         legacy_delay=0.0, source_speed=1.0)
        t = s2.target(0.0, T0)
        self.assertEqual((t.mode, t.ms), ("DELAY", T0 - 3000))

    def test_sync_plus_minus_is_manual(self):
        s = self.make()
        s.update(sample(10.0, ts=0.0), T0)
        s.target(0.0, T0)
        self.assertIn("5.25s", s.adjust(0.25, 0.0, T0))
        self.assertIn("5.50s", s.adjust(0.25, 0.0, T0))
        self.assertEqual(s.mapping.confidence, "MANUAL")


class VodSyncTest(unittest.TestCase):
    """VOYO recording + OpenF1 (real 2026 Japanese GP data in tests/fixtures)."""

    def setUp(self):
        from server.openf1 import ref_events_from_openf1
        self.session = next(x for x in FIX["sessions"] if x["session_key"] == 11253)
        self.ref = ref_events_from_openf1(FIX["laps"], FIX["race_control"])
        self.sched = _ms(self.session["date_start"])
        # the recording: video 0:00 = 04:31:10 UTC (lead 28:50 before the 05:00 schedule - unknown to the system)
        self.K = (self.sched - (28 * 60 + 50) * 1000) / 1000
        self.mono = 0.0

    def make(self, **cfg):
        s = SyncManager({"enabled": True, **cfg}, voyo_enabled=True, legacy_delay=0, source_speed=1.0,
                        store_path=None, vod=True)
        s.initialize(self.session, self.ref)
        return s

    def at(self, s, f1_ms, paused=True):
        """Move the video to the frame that shows F1 time f1_ms (paused on it)."""
        self.mono += 1.0
        pb = f1_ms / 1000 - self.K
        s.update(sample(pb, paused=paused, ts=self.mono), T0)
        s.target(self.mono, T0)
        return pb

    def test_openf1_reference_events(self):
        # lap 6 date_start of #12 = the crossing that completed lap 5 (same ms as the F1 archive message)
        self.assertIn((_ms("2026-03-29T05:22:02.092Z"), 5), self.ref.crossings["12"])
        self.assertEqual(self.ref.starts, [START_MS])
        self.assertEqual(self.ref.finishes, [_ms("2026-03-29T06:42:05.699Z")])
        self.assertEqual(len(self.ref.crossings["12"]), 8)          # laps 1..7 completed + end of lap 8

    def test_unsynced_without_anchor_gives_no_timestamp(self):
        s = self.make()
        self.at(s, START_MS)
        t = s.target(self.mono, T0)
        self.assertIsNone(t.ms)
        st = s.get_state(self.mono, T0, None)
        self.assertEqual((st["confidence"], st["synced"], st["absoluteTime"]), ("UNSYNCED", False, None))
        self.assertIn("Countdown", st["reason"])
        self.assertIn("not synchronised", s.add_event_anchor("lap", self.mono, T0, "12", "ANT"))

    def test_countdown_is_medium_and_matches_openf1_start(self):
        s = self.make()
        pb = self.at(s, self.sched - (23 * 60 + 47) * 1000)          # VOYO shows 23:47 to the start
        s.capture(self.mono, T0)
        msg = s.add_countdown_anchor(23 * 60 + 47, self.mono, T0, "23:47", target="scheduled")
        self.assertIn("MEDIUM", msg)
        s.target(self.mono, T0)
        st = s.get_state(self.mono, T0, None)
        self.assertEqual(st["method"], "VOYO Countdown")
        self.assertIn("23:47 before session start", st["anchor"])
        self.assertAlmostEqual(st["offsetSeconds"], self.K, places=2)
        self.assertEqual(st["leadSeconds"], 28 * 60 + 50)
        self.assertIsNone(st["errorSeconds"])                        # nothing measured yet ...
        self.assertEqual((st["health"], st["healthError"]), ("MEDIUM", "±1–2 s"))   # ... only the stated range
        self.assertTrue(st["synced"])

    def test_countdown_plus_line_crossings_is_high(self):
        s = self.make()
        self.at(s, self.sched - 600_000)
        s.add_countdown_anchor(600, self.mono, T0, "10:00", target="scheduled")
        for t, lap in self.ref.crossings["12"][2:4]:
            self.at(s, t, paused=True)                               # paused exactly on the line
            self.assertIn("LAP", s.add_event_anchor("lap", self.mono, T0, "12", "ANT"))
        st = s.get_state(self.mono, T0, None)
        self.assertEqual(st["confidence"], "HIGH")
        self.assertIn("independent anchors agree", st["reason"])
        self.assertAlmostEqual(st["offsetSeconds"], self.K, places=2)
        self.assertLess(st["errorSeconds"], 0.5)

    def test_lights_out_alone_is_medium_then_s_makes_high(self):
        s = self.make()
        self.at(s, START_MS)
        self.assertIn("MEDIUM", s.add_event_anchor("start", self.mono, T0, None, "START"))
        t, _ = self.ref.crossings["12"][5]
        self.at(s, t)
        self.assertIn("HIGH", s.add_event_anchor("lap", self.mono, T0, "12", "ANT"))

    def test_wrong_estimate_makes_ambiguous_lap_low_until_start_anchor(self):
        s = self.make(broadcast_lead_seconds=1800)                   # user estimate 30:00 (really 28:50)
        t, lap = self.ref.crossings["12"][4]
        self.at(s, t)
        self.assertEqual(s.mapping.confidence, "LOW")
        msg = s.add_event_anchor("lap", self.mono, T0, "12", "ANT")
        self.assertIn("not confirmed", msg)
        self.assertEqual(s.mapping.confidence, "LOW")                # several laps within ±20 min
        self.at(s, START_MS)
        s.add_event_anchor("start", self.mono, T0, None, "START")      # unique -> grounds + re-matches the lap
        self.assertEqual(s.mapping.confidence, "HIGH")
        self.assertAlmostEqual(s.mapping.offset, self.K, places=2)

    def test_big_difference_is_not_applied_silently(self):
        s = self.make()
        self.at(s, self.sched - 600_000)
        s.add_countdown_anchor(600, self.mono, T0, target="scheduled")
        k_before = s.K
        self.at(s, START_MS + 5000)                                    # L pressed 5 s after lights out
        msg = s.add_event_anchor("start", self.mono, T0, None, "L")
        self.assertIn("POSSIBLE SYNC DRIFT", msg)
        self.assertEqual(s.K, k_before)                                 # nothing moved
        st = s.get_state(self.mono, T0, None)
        self.assertAlmostEqual(st["drift"]["shift"], -5.0, places=1)   # shown time would move 5 s back
        self.assertAlmostEqual(st["drift"]["current"], 28 * 60 + 50, places=1)
        self.assertEqual(len(s.anchors), 1)
        self.assertIn("KEPT", s.keep_old())
        self.assertIsNone(s.pending)
        self.assertEqual([a.status for a in s.history], ["rejected"])
        s.add_event_anchor("start", self.mono, T0, None, "L")          # again - and use it this time
        self.assertIn("USING THE NEW SYNC", s.use_new())
        self.assertAlmostEqual(s.K - k_before, -5.0, places=1)
        self.assertEqual([a.status for a in s.history], ["rejected", "replaced"])

    def test_typed_time_replaces_a_different_sync_and_says_so(self):
        s = self.make()
        self.at(s, self.sched - 600_000)
        s.add_countdown_anchor(600, self.mono, T0, target="scheduled")
        self.at(s, START_MS)
        msg = s.add_manual_anchor(START_MS + 9000, self.mono, T0, text="07:14:11")   # you typed another time
        self.assertIn("previous sync replaced", msg)
        self.assertEqual([a.kind for a in s.anchors], ["exact"])
        self.assertEqual(s.mapping.confidence, "MANUAL")
        self.assertEqual([a.status for a in s.history], ["replaced"])

    def test_typed_time_far_from_the_session_is_refused(self):
        s = self.make()
        self.at(s, START_MS)
        for wrong in (START_MS - 12 * 3600_000, START_MS + 7 * 3600_000):   # 12-hour clock / wrong zone
            msg = s.add_manual_anchor(wrong, self.mono, T0, text="x")
            self.assertIn("no data there", msg)
        self.assertEqual(s.mapping.confidence, "UNSYNCED")

    def test_consistent_anchor_is_confirmed_minor_one_moves_slowly(self):
        s = self.make()
        self.at(s, self.sched - 600_000)
        s.add_countdown_anchor(600, self.mono, T0, target="scheduled")
        self.at(s, START_MS)
        s.add_event_anchor("start", self.mono, T0, None, "L")          # exact: consistent
        st = s.get_state(self.mono, T0, None)
        self.assertEqual((st["confidence"], st["health"]), ("HIGH", "HIGH"))
        self.assertFalse(st["errorMeasured"])                          # one precise anchor: stated range
        self.assertEqual(st["healthError"], "±0.1–0.3 s")              # paused on the event
        t, _ = self.ref.crossings["12"][5]
        self.at(s, t + 1200)                                            # S pressed 1.2 s late (paused)
        k_before = s.K
        msg = s.add_event_anchor("lap", self.mono, T0, "12", "ANT")
        self.assertIn("minor deviation", msg)
        self.assertLess(abs(s.K - k_before), 1e-9)                      # no jump ...
        s.target(self.mono + 1, T0)
        s.target(self.mono + 2, T0)
        self.assertGreater(abs(s.K - k_before), 0)                      # ... it moves gradually
        self.assertLess(abs(s.K - k_before), 0.2)
        st = s.get_state(self.mono, T0, None)
        self.assertEqual(st["confidence"], "MEDIUM")                    # two events differ by > 0.5 s
        self.assertTrue(st["errorMeasured"])
        self.assertIn("minor deviation", st["reason"])

    def test_several_anchors_median_outlier_and_measured_error(self):
        s = self.make()
        self.at(s, self.sched - 600_000)
        s.add_countdown_anchor(600, self.mono, T0, target="scheduled")
        self.at(s, START_MS + 100)
        s.add_event_anchor("start", self.mono, T0, None, "L")          # +0.10 s
        for i, err in ((4, -0.05), (5, 0.08), (6, 1.4)):                # lap 7 pressed 1.4 s late
            t, _ = self.ref.crossings["12"][i]
            self.at(s, t + err * 1000)
            s.add_event_anchor("lap", self.mono, T0, "12", "ANT")
        st = s.get_state(self.mono, T0, None)
        rows = {r["label"]: r for r in st["anchors"]}
        self.assertEqual(rows["S lap 7"]["state"], "outlier")
        self.assertEqual(rows["L"]["state"], "valid")
        self.assertEqual((st["anchorsValid"], st["anchorsOutliers"]), (4, 1))
        self.assertEqual((st["confidence"], st["health"]), ("HIGH", "HIGH"))
        self.assertTrue(st["errorMeasured"])
        self.assertAlmostEqual(float(st["healthError"].strip("±s ")), 0.13, places=2)   # measured: max deviation
        self.assertAlmostEqual(self.K - s.mapping.offset, 0.08, delta=0.005)  # median of 0.10/-0.05/0.08;
        #                                                                  the mean with lap 7 would be 0.38
        self.assertIn("1 outlier", st["reason"])

    def test_same_event_twice_is_not_independent(self):
        s = self.make()
        self.at(s, START_MS)
        s.add_event_anchor("start", self.mono, T0, None, "L")
        s.add_event_anchor("start", self.mono, T0, None, "L")
        self.assertEqual(s.mapping.independent, 1)
        self.assertEqual(s.mapping.confidence, "MEDIUM")

    def test_health_levels_and_no_invented_error(self):
        s = self.make()
        self.at(s, START_MS)
        st = s.get_state(self.mono, T0, None)
        self.assertEqual((st["health"], st["healthError"]), ("UNSYNCED", None))
        s.add_countdown_anchor((self.sched - START_MS) / 1000, self.mono, T0, target="scheduled")
        s.target(self.mono, T0)
        st = s.get_state(self.mono, T0, None)
        self.assertEqual((st["health"], st["healthError"], st["errorMeasured"]), ("MEDIUM", "±1–2 s", False))
        s.clear_anchor()
        s.add_manual_anchor(START_MS, self.mono, T0, text="14:14:02")
        s.target(self.mono, T0)
        st = s.get_state(self.mono, T0, None)
        self.assertEqual((st["health"], st["healthError"]), ("MANUAL", None))

    def test_manual_exact_time_is_manual(self):
        s = self.make()
        self.at(s, START_MS + 60_000)
        msg = s.add_manual_anchor(START_MS + 60_000, self.mono, T0, text="14:15:02")
        self.assertIn("MANUAL", msg)
        st = s.get_state(self.mono, T0, None)
        self.assertEqual((st["method"], st["confidence"]), ("Manual Exact Time", "MANUAL"))
        # the video plays on: absoluteTime = anchorF1 + (currentTime - anchorVideoTime)
        self.mono += 10
        s.update(sample((START_MS + 70_000) / 1000 - self.K, ts=self.mono), T0)
        self.assertAlmostEqual(s.target(self.mono, T0).ms, START_MS + 70_000, delta=1)

    def test_captured_moment_is_used_for_typed_values(self):
        s = self.make()
        self.at(s, self.sched - 600_000, paused=False)
        s.capture(self.mono, T0)
        pb_capture = s.captured[0]
        self.mono += 8                                                # typing takes 8 s, video plays on
        s.update(sample(pb_capture + 8, ts=self.mono), T0)
        s.add_countdown_anchor(600, self.mono, T0, target="scheduled")
        self.assertAlmostEqual(s.mapping.offset, self.K, places=2)

    def test_estimate_is_labelled_and_learned_lead_not_applied_silently(self):
        s = self.make()
        self.at(s, START_MS)
        s.store.data["leads"]["Race"] = [1730.0]
        self.assertEqual(s.mapping.confidence, "UNSYNCED")
        res = s.auto_sync(self.mono)
        self.assertFalse(res["applied"])
        self.assertIn("not reliable enough", res["message"])
        self.assertEqual(res["suggested_lead"], 1730.0)
        s.set_estimate(1730.0)
        s.target(self.mono, T0)
        st = s.get_state(self.mono, T0, None)
        self.assertEqual((st["confidence"], st["method"], st["absoluteTime"]), ("LOW", "Session Start Estimate", None))
        self.assertIsNotNone(st["approxTime"])

    def test_sync_is_saved_per_video_and_session(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "cal.json"
            s = SyncManager({}, True, 0, 1.0, path, vod=True)
            s.initialize(self.session, self.ref)
            self.at(s, START_MS)
            s.add_event_anchor("start", self.mono, T0, None, "START")
            s2 = SyncManager({}, True, 0, 1.0, path, vod=True)     # dashboard restarted
            s2.initialize(self.session, self.ref)
            self.at(s2, START_MS)
            self.assertEqual(s2.mapping.confidence, "MEDIUM")
            self.assertIn("+restored", s2.mapping.source)
            s3 = SyncManager({}, True, 0, 1.0, path, vod=True)     # other session, same video
            s3.initialize({**self.session, "session_key": 11249}, self.ref)
            self.at(s3, START_MS)
            self.assertEqual(s3.mapping.confidence, "UNSYNCED")
            s4 = SyncManager({}, True, 0, 1.0, path, vod=True)     # other video
            s4.initialize(self.session, self.ref)
            self.mono += 1
            s4.update(sample(10.0, ts=self.mono, asset="voyo|other"), T0)
            self.assertEqual(s4.mapping.confidence, "UNSYNCED")

    def test_same_session_other_media_id_is_a_new_vod_and_reopen_restores(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "cal.json"

            def video(media_id):
                s = SyncManager({}, True, 0, 1.0, path, vod=True)
                s.initialize(self.session, self.ref)
                self.mono += 1
                s.update(sample(START_MS / 1000 - self.K, paused=True, ts=self.mono, asset="x",
                                page={"media_id": media_id}), T0)
                s.target(self.mono, T0)
                return s
            a = video("111")
            a.add_event_anchor("start", self.mono, T0, None, "L")
            self.assertEqual(a.mapping.confidence, "MEDIUM")
            b = video("222")                                              # same GP + session, other recording
            self.assertEqual(b.mapping.confidence, "UNSYNCED")
            c = video("111")                                              # the first recording again
            self.assertEqual(c.mapping.confidence, "MEDIUM")
            self.assertIn("restored", c.mapping.source)

    def test_clock_lost_holds_and_is_not_synced(self):
        s = self.make()
        self.at(s, START_MS)
        s.add_event_anchor("start", self.mono, T0, None, "START")
        t = s.target(self.mono + 10, T0)
        self.assertEqual((t.mode, t.rate), ("HOLD", 0.0))
        self.assertFalse(s.get_state(self.mono + 10, T0, None)["synced"])

    def test_countdown_parser(self):
        from server.sync import parse_countdown
        for txt, v in [("23:47", 1427), ("00:23:47", 1427), ("23m 47s", 1427), ("1:02:03", 3723),
                       ("47s", 47), ("23m", 1380), ("1h 2m", 3720)]:
            self.assertEqual(parse_countdown(txt), v, txt)
        for bad in ("", "abc", "23:77", "99:00:00", "-5"):
            self.assertIsNone(parse_countdown(bad), bad)


class DetectionTest(unittest.TestCase):
    def test_title_to_session(self):
        from datetime import datetime, timezone
        from server.openf1 import match_session, parse_title
        now = datetime(2026, 9, 29, tzinfo=timezone.utc)
        cases = [("Formula 1: Velika nagrada Japonske – dirka", 11253),
                 ("VN Japonske, kvalifikacije", 11249),
                 ("1. prosti trening - VN Japonske", 11246),
                 ("VN Kitajske: sprint kvalifikacije", 11236),
                 ("Sprint, VN Kitajske", 11240),
                 ("VN Španije 2026 - 1. prosti trening", 11362),
                 ("VN Barcelone – dirka", 11307)]
        for title, key in cases:
            det = match_session(parse_title(title), FIX["sessions"], FIX["meetings"], now)
            self.assertEqual((det.session or {}).get("session_key"), key, (title, det.reason))

    def test_never_guesses(self):
        from datetime import datetime, timezone
        from server.openf1 import match_session, parse_title
        now = datetime(2026, 9, 29, tzinfo=timezone.utc)
        det = match_session(parse_title("VN Japonske"), FIX["sessions"], FIX["meetings"], now)
        self.assertIsNone(det.session)                               # no session type in the title
        self.assertIn("session type", det.reason)
        det = match_session(parse_title("Formula 1 posnetek"), FIX["sessions"], FIX["meetings"], now)
        self.assertIsNone(det.session)
        det = match_session(parse_title("Bahrajn dirka"), FIX["sessions"], FIX["meetings"], now)
        self.assertIsNone(det.session)                               # the Oct race is in the future


class VodSourceTest(unittest.TestCase):
    """Seekable whole-session source: state after jumps == state replayed from the start."""

    def test_seek_anywhere_gives_the_same_state(self):
        import asyncio
        from server.openf1 import OpenF1Client
        from server.sources.vod import VodSource, _ms as ems
        events = load_file(SAMPLE)
        src = VodSource({"session_key": 1}, OpenF1Client(Path("/nonexistent")), Path("/nonexistent"))
        src.events = events
        src._times = [ems(e) for e in events]
        src.ckpts, src.ref = VodSource._prepare(events)
        src.state = "ready"
        self.assertGreater(src.ref.count(), 50)                       # crossings from the archive fallback
        tl = Timeline(buffer_seconds=120)

        def ingest(topic, data, ms, snap):
            tl.ingest(topic, data, ms, ms, snap)

        t0, t1 = src._times[0], src._times[-1]
        for frac in (0.6, 0.62, 0.3, 0.9, 0.31):                       # forward, backward, long jumps
            target = t0 + (t1 - t0) * frac
            src.ensure(target, ingest, tl)
            tl.advance(target)
            ref_tl = Timeline(buffer_seconds=1e6)
            for e, ms in zip(events, src._times):
                if ms > target + 10_000:
                    break
                ref_tl.ingest(e.topic, e.data, ms, ms, e.snap)
            ref_tl.advance(target)
            for topic in ("TimingData", "TrackStatus", "LapCount", "RaceControlMessages", "TimingAppData"):
                self.assertEqual(json.dumps(tl.feed.get(topic), sort_keys=True),
                                 json.dumps(ref_tl.feed.get(topic), sort_keys=True), (frac, topic))


class ArchiveDownloadTest(unittest.TestCase):
    """Streamed archive download: progress is reported, no truncated cache file is ever left."""

    def test_progress_and_no_partial_file(self):
        import asyncio
        import tempfile
        from unittest import mock
        import httpx
        from server.sources import replay
        body = "\n".join(
            ['00:00:00.100{"Utc":"2026-09-26T09:00:00.1Z"}', '00:00:01.000{"Utc":"2026-09-26T09:00:01Z"}', ""]).encode()
        calls = {"n": 0}

        def handler(req):
            calls["n"] += 1
            if req.url.path.endswith("Heartbeat.jsonStream"):
                return httpx.Response(200, content=body * 20000)
            if req.url.path.endswith("TimingData.jsonStream"):
                raise httpx.ReadError("connection reset")        # interrupted download
            return httpx.Response(404)

        real = httpx.AsyncClient
        seen = []
        with tempfile.TemporaryDirectory() as d, \
                mock.patch.object(replay.httpx, "AsyncClient",
                                  lambda **kw: real(transport=httpx.MockTransport(handler), **kw)):
            with self.assertRaises(httpx.ReadError):
                asyncio.run(replay.load_archive("2026/x/", Path(d), ["Heartbeat", "TimingData"], progress=seen.append))
            files = sorted(p.name for p in (Path(d) / "2026/x").iterdir())
            self.assertIn("Heartbeat.jsonStream", files)             # complete file kept
            self.assertNotIn("TimingData.jsonStream", files)        # never a truncated cache file
            ev = asyncio.run(replay.load_archive("2026/x/", Path(d), ["Heartbeat"], progress=seen.append))
            self.assertEqual(len(ev), 40000)
        self.assertTrue(any("Heartbeat" in x and "MB" in x for x in seen), seen)


class VodLoadAfterSyncTest(unittest.TestCase):
    """[vod] preload_data = false: the session details come first, the big download only after SYNC."""

    def test_download_waits_for_sync(self):
        import asyncio
        from unittest import mock
        from server.openf1 import OpenF1Client, OpenF1Error
        from server.sources import vod as vodmod
        events = load_file(SAMPLE)
        downloads = []

        async def fake_path(year, key, cache):
            return "2026/x/", {"session_key": key, "meeting_name": "Japanese Grand Prix", "session_name": "Race",
                               "date_start": "2026-03-29T05:00:00+00:00", "gmt_offset": "09:00:00"}

        small = []

        async def fake_load(path, cache, topics=None, progress=None):
            if topics is not None:                                # the small session-structure topics
                small.append(list(topics))
                return [e for e in events if e.topic in topics]
            downloads.append(path)
            return events

        class Sink:
            def set_status(self, **kw):
                pass

        async def scenario():
            client = OpenF1Client(Path("/nonexistent"))

            async def offline(*a, **k):
                raise OpenF1Error("offline (test)")
            client.session = client.laps = client.race_control = offline
            src = vodmod.VodSource({"session_key": 11253}, client, Path("/nonexistent"))
            metas, loaded = [], []
            src.on_meta = lambda sess, ref: metas.append(sess["session_key"])
            src.on_loaded = lambda sess, ref: loaded.append(sess["session_key"])
            task = asyncio.create_task(src.run(Sink()))
            for _ in range(50):
                await asyncio.sleep(0.05)
                if src.waiting_for_sync:
                    break
            self.assertTrue(src.waiting_for_sync)
            self.assertEqual(metas, [11253])                      # start time / time zone known
            self.assertEqual(downloads, [])                      # nothing big downloaded before SYNC
            from server.sources.replay import META_TOPICS
            self.assertEqual(small, [META_TOPICS])               # only the clock / status topics (KBs)
            self.assertNotIn("TimingData", META_TOPICS)
            self.assertNotIn("Position.z", META_TOPICS)
            src.allow_load()
            for _ in range(100):
                await asyncio.sleep(0.05)
                if loaded:
                    break
            task.cancel()
            self.assertEqual(downloads, ["2026/x/"])
            self.assertEqual(src.state, "ready")

        with mock.patch.object(vodmod, "archive_session_path", fake_path), \
                mock.patch.object(vodmod, "load_archive", fake_load):
            asyncio.run(scenario())


class LiveModeRecordingTest(unittest.TestCase):
    """A VOYO recording followed while the server runs in LIVE mode is reported, not shown as an empty board."""

    def test_recording_in_live_mode_is_flagged(self):
        import tempfile
        import time as _t
        from server.config import load_config
        from server.engine import Engine
        from server.hub import Hub
        from server.sources.base import Source
        from server.track import TrackProvider

        class LiveStub(Source):
            mode = "live"

            async def run(self, sink):
                pass

        with tempfile.TemporaryDirectory() as d:
            cfg = load_config(Path(d) / "none.toml")
            eng = Engine(cfg, LiveStub(), TrackProvider(Path(d), cfg["tracks"]), Hub())
            now = _t.time()
            eng.sync.update(parse_voyo_sample({"playback_time": 1761.8, "paused": True, "timestamp_local": now},
                                              now, _t.monotonic()), now * 1000)
            eng._target_ms = eng._src_now_ms() - 3 * 86400 * 1000          # the video shows 3 days ago
            st = eng.sync_status()
            self.assertIn("RECORDING_IN_LIVE_MODE", st["flags"])
            self.assertIn("launch.bat vod", st["modeNote"])
            eng._target_ms = eng._src_now_ms() - 30_000                    # watching live: no warning
            self.assertNotIn("modeNote", eng.sync_status())

    def test_launcher_picks_vod_between_sessions(self):
        from datetime import datetime, timezone
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
        import tv_launcher
        idx = {"Meetings": [{"Name": "Azerbaijan Grand Prix", "Sessions": [
            {"Name": "Race", "StartDate": "2026-09-26T15:00:00", "EndDate": "2026-09-26T17:00:00", "GmtOffset": "04:00:00"}]}]}
        at = lambda iso: datetime.fromisoformat(iso).replace(tzinfo=timezone.utc).timestamp()
        self.assertEqual(tv_launcher.choose_mode(at("2026-09-29T13:46:00"), idx)[0], "vod")
        self.assertEqual(tv_launcher.choose_mode(at("2026-09-26T11:30:00"), idx)[0], "live")


class RaceControlCutTest(unittest.TestCase):
    """Rewinding: race control messages and the flags / investigations / penalties they cause are
    cut at the shown moment, even if the state holds later messages (real 2026 Japan messages)."""

    def test_messages_after_the_shown_time_are_hidden(self):
        from datetime import datetime
        from server.feedstate import FeedState
        from server.race_control import process_messages
        fs = FeedState()
        for e in load_file(SAMPLE):
            if e.topic == "RaceControlMessages":
                fs.apply(e.topic, e.data, e.snap)
        raw = fs.get("RaceControlMessages")
        ms = lambda iso: datetime.fromisoformat(iso + "+00:00").timestamp() * 1000
        full = process_messages(raw)
        at_0630 = process_messages(raw, ms("2026-03-29T06:30:00"))
        at_0515 = process_messages(raw, ms("2026-03-29T05:15:00"))
        self.assertEqual(at_0630.driver_flags["44"].investigation, "NOTED")         # 06:29:27 noted
        self.assertTrue(all(m.utc <= "2026-03-29T06:30:01" for m in at_0630.messages))
        self.assertLess(len(at_0515.messages), len(at_0630.messages))
        self.assertLess(len(at_0630.messages), len(full.messages))
        self.assertFalse(("44" in at_0515.driver_flags) and at_0515.driver_flags["44"].investigation)
        self.assertTrue(all(m.utc <= "2026-03-29T05:15:01" for m in at_0515.messages))


class SyncWhileLoadingTest(unittest.TestCase):
    """The session is known before its (large) data finishes downloading: a typed time works
    then, and the anchor survives when the data arrives."""

    def test_exact_time_before_data_loaded(self):
        import tempfile
        import time
        from datetime import datetime
        from server.openf1 import RefEvents
        sess = next(x for x in FIX["sessions"] if x["session_key"] == 11377)
        with tempfile.TemporaryDirectory() as d:
            sm = SyncManager({}, True, 0.0, 1.0, Path(d) / "cal.json", vod=True)
            sm.initialize(dict(sess), None)                     # metadata only (engine._vod_meta)
            t = time.time()
            mono = time.monotonic()
            sm.update(parse_voyo_sample({"playback_time": 3600.0, "paused": True, "timestamp_local": t,
                                         "page": {"media_id": "77"}}, t, mono), t * 1000)
            f1 = datetime.fromisoformat("2026-09-26T11:00:00+00:00").timestamp() * 1000
            res = sm.add_manual_anchor(f1, mono, t * 1000, text="13:00:00")
            self.assertFalse(res.startswith("SYNC:"), res)
            self.assertEqual(sm.mapping.confidence, "MANUAL")
            sm.initialize(dict(sess), RefEvents())              # data loaded (engine._vod_loaded)
            self.assertEqual(sm.mapping.confidence, "MANUAL")
            self.assertAlmostEqual(sm.mapping.offset, f1 / 1000 - 3600.0, places=3)


class AutoMediaSyncTest(unittest.TestCase):
    """Which session is this VOYO video? (real OpenF1 data, no timestamps involved)."""
    NOW = None

    def det(self, title, video_s=9184, **kw):
        from datetime import datetime, timezone
        from server.openf1 import match_session, parse_title
        now = datetime(2026, 9, 29, 12, tzinfo=timezone.utc)
        return match_session(parse_title(title), FIX["sessions"], FIX["meetings"], now, video_seconds=video_s, **kw)

    def key(self, title, **kw):
        d = self.det(title, **kw)
        return (d.session or {}).get("session_key"), d.status

    def test_slovenian_and_english_titles(self):
        cases = {
            "VN Azerbajdžana - Glej dirke online": 11377,         # bare GP title + long video = race
            "Velika nagrada Azerbajdžana": 11377,
            "Azerbaijan Grand Prix": 11377,
            "Azerbaijan GP – Race": 11377,
            "VN Azerbajdžana: dirka - Glej dirke online": 11377,
            "VN Azerbajdžana - kvalifikacije - Glej dirke online": 11373,
            "Azerbaijan GP Qualifying": 11373,
            "VN Azerbajdžana, 1. prosti trening": 11370,
            "Azerbaijan GP FP2": 11371,
            "vn-azerbajdzana-trening-3": 11372,
            "VN Kitajske: sprint kvalifikacije": 11236,
            "Chinese GP - Sprint Qualifying": 11236,
            "VN Kitajske – sprint": 11240,
            "Formula 1: Velika nagrada Japonske – dirka": 11253,
        }
        for title, key in cases.items():
            self.assertEqual(self.key(title), (key, "detected"), title)

    def test_season_from_title_and_recent_assumption_is_labelled(self):
        self.assertEqual(self.key("Azerbaijan GP 2025 - Qualifying"), (9900, "detected"))
        self.assertEqual(self.key("VN Azerbajdžana 2025 - Glej dirke online"), (9904, "detected"))
        d = self.det("VN Azerbajdžana - Glej dirke online")
        self.assertIn("ASSUMED", d.how)                                   # 2026 race 3 days ago, 2025 exists too
        d = self.det("VN Azerbajdžana - Glej dirke online", assume_recent_days=0)
        self.assertEqual(d.status, "ambiguous")                           # never assume -> choose
        self.assertEqual({c["session_key"] for c in d.candidates}, {9904, 11377})

    def test_wrong_or_inconsistent_year(self):
        d = self.det("VN Japonske 2024 - dirka")
        self.assertEqual((d.session, d.status), (None, "failed"))
        self.assertIn("2024", d.reason)
        d = self.det("VN Japonske - dirka 29. 3. 2025")                  # date says 2025, only 2026 exists
        self.assertIsNone(d.session)

    def test_failed_and_ambiguous_are_never_guessed(self):
        self.assertEqual(self.key("Formula 1 - Glej dirke online"), (None, "failed"))     # no GP
        self.assertEqual(self.key("Kvalifikacije in dirka VN Azerbajdžana"), (None, "ambiguous"))
        self.assertEqual(self.key("VN Italije in VN Madžarske"), (None, "ambiguous"))
        self.assertEqual(self.key("VN Azerbajdžana - Glej dirke online", video_s=3600), (None, "ambiguous"))
        self.assertEqual(self.key("VN Azerbajdžana - Glej dirke online", video_s=None), (None, "ambiguous"))
        self.assertEqual(self.key("VN Azerbajdžana", bare_title_is_race=False), (None, "ambiguous"))
        self.assertEqual(self.key("VN Japonske - sprint"), (None, "failed"))             # Japan had no sprint
        self.assertEqual(self.key("Bahrajn dirka"), (None, "failed"))    # only a future Bahrain race in the data


class MediaFlowTest(unittest.TestCase):
    """Engine: detection -> session request; manual fallback; per-video memory; new video unloads."""

    def setUp(self):
        import tempfile
        from server.config import load_config
        from server.hub import Hub
        from server.openf1 import OpenF1Client
        from server.sources.vod import VodSource
        from server.sync import CalibrationStore
        from server.track import TrackProvider
        from server.engine import Engine
        self.tmp = tempfile.TemporaryDirectory()
        d = Path(self.tmp.name)

        class FakeOpenF1(OpenF1Client):
            async def sessions(self, year):
                return [x for x in FIX["sessions"] if str(x["date_start"])[:4] == str(year)]

            async def meetings(self, year):
                return FIX["meetings"]

        def make():
            cfg = load_config(Path(self.tmp.name) / "none.toml")
            cfg["source"]["mode"] = "vod"
            src = VodSource(cfg["vod"], FakeOpenF1(d / "of1"), d / "arch")
            eng = Engine(cfg, src, TrackProvider(d, cfg["tracks"]), Hub())
            eng.sync.store = CalibrationStore(d / "cal.json")
            return eng, src
        self.make = make

    def tearDown(self):
        self.tmp.cleanup()

    def feed(self, eng, title, media_id, length=9184):
        import asyncio

        async def go():
            eng.voyo_sample(parse_voyo_sample({"playback_time": 100, "ready_state": 4, "asset": "x",
                                               "meta": {"length": length},
                                               "page": {"media_title": title, "media_id": media_id}}, 0, 1.0))
            if eng._detect_task:
                await eng._detect_task
        asyncio.run(go())

    def test_auto_detection_requests_the_session_but_not_a_sync(self):
        eng, src = self.make()
        self.feed(eng, "VN Azerbajdžana - Glej dirke online", "m1")
        m = eng.media_state()
        self.assertEqual((m["state"], m["session_key"], src.requested), ("detected", 11377, 11377))
        st = eng.sync_status()
        self.assertEqual(st["confidence"], "UNSYNCED")                    # detection is not a time sync
        self.assertIsNone(st["absoluteTime"])

    def test_failed_detection_then_manual_selection_is_remembered_per_video(self):
        eng, src = self.make()
        self.feed(eng, "Formula 1 - Glej dirke online", "m2")
        self.assertEqual((eng.media["state"], src.requested), ("failed", None))
        eng.select_session(11249)
        self.assertEqual((eng.media["state"], src.requested), ("manual", 11249))
        eng2, src2 = self.make()                                          # dashboard restarted, same video
        self.feed(eng2, "Formula 1 - Glej dirke online", "m2")
        self.assertEqual((eng2.media["state"], src2.requested), ("manual", 11249))
        eng3, src3 = self.make()                                          # same title, OTHER video
        self.feed(eng3, "Formula 1 - Glej dirke online", "m3")
        self.assertEqual((eng3.media["state"], src3.requested), ("failed", None))

    def test_another_video_unloads_the_previous_session(self):
        eng, src = self.make()
        self.feed(eng, "VN Azerbajdžana - Glej dirke online", "m1")
        src.loaded_key, src.state = 11377, "ready"                         # as if loaded
        self.feed(eng, "VN Japonske - kvalifikacije", "m9")
        self.assertIsNone(src.loaded_key)
        self.assertEqual((eng.media["media_id"], src.requested), ("m9", 11249))


class LauncherWindowTest(unittest.TestCase):
    def test_voyo_window_found_by_recording_title(self):
        """A recording's window is titled like the page ("VN Azerbajdžana - Glej dirke online"),
        without "VOYO": the agent must still find it (title from the clock bridge) so T works."""
        import tools.tv_launcher as tl

        class Ops(tl.NullOps):
            name, dpi_scale = "fake", 1.0
            titles = {7: "VN Azerbajdžana - Glej dirke online"}

            def screen_size(self): return 1920, 1080
            def find_voyo(self, pid, hint): return next((h for h, t in self.titles.items() if hint.lower() in t.lower()), None)
            def is_valid(self, w): return w in self.titles
            def place(self, w, rect, topmost): self.placed = w
            def foreground_is(self, w): return True
            def poll_hotkeys(self, active): return ["T"] if active else []

        sent = []
        agent = tl.TvAgent(Ops(), "http://x", "", 0, "VOYO", None, http_get=lambda p: {}, http_key=sent.append)
        agent.step({"tv_mode_effective": "RACE_VIEW"})
        self.assertIsNone(agent.win)                                  # "VOYO" is not in the title
        agent.title_hints = lambda: ["VN Azerbajdžana - Glej dirke online"]
        agent.step({"tv_mode_effective": "RACE_VIEW"})
        agent.step({"tv_mode_effective": "RACE_VIEW"})
        self.assertEqual(agent.win, 7)
        self.assertIn("KEY_T", sent)


class SyncMiscTest(unittest.TestCase):
    def test_remote_sync_commands(self):
        import asyncio
        from server.remote import RemoteController
        calls = []

        async def pub(_):
            pass
        rc = RemoteController({"keymap": {"KEY_S": "SYNC_MARK", "KEY_EQUAL": "SYNC_PLUS"}}, lambda: ["1"], pub)
        rc.sync_hook = lambda name, arg: calls.append((name, arg)) or f"{name} ok"
        asyncio.run(rc.handle_key("KEY_EQUAL", "t"))
        asyncio.run(rc.handle_key("KEY_S", "t"))
        asyncio.run(rc.handle_command("SYNC_ADJUST", "-10", "t"))
        asyncio.run(rc.handle_command("SYNC_ADJUST", "rm -rf", "t"))
        rc.ui.tv_mode = "VIDEO_FOCUS"
        asyncio.run(rc.handle_command("SYNC_MENU", None, "t"))
        self.assertEqual(calls, [("SYNC_PLUS", None), ("SYNC_MARK", None), ("SYNC_ADJUST", "-10")])
        self.assertTrue(rc.ui.sync_menu)
        self.assertEqual(rc.ui.tv_mode, "RACE_VIEW")                  # the video window would cover the menu
        self.assertEqual(rc.ui.toast["text"], "SYNC_ADJUST ok")

    def test_sample_validation(self):
        with self.assertRaises(ValueError):
            parse_voyo_sample({"playback_time": "x"}, 0, 0)
        with self.assertRaises(ValueError):
            parse_voyo_sample({"playback_time": math.inf}, 0, 0)
        s = parse_voyo_sample({"playback_time": 5, "asset": "a\x00b<script>", "events": [{"type": "evil"}],
                               "meta": {"length": 9184, "drmProtected": True, "x": "y"},
                               "page": {"title": "VN Japonske", "media_id": "ab/../12", "token": "secret"}}, 0, 0)
        self.assertEqual(s.events, [])
        self.assertEqual(s.meta, {"length": 9184.0, "drmProtected": True})
        self.assertNotIn("token", s.page)
        self.assertEqual(s.asset, "voyo-media:ab12")


if __name__ == "__main__":
    unittest.main()
