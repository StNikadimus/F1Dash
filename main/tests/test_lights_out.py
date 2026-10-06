"""LIGHTS OUT (L / START) as a sync anchor - found in the real F1 data, never the schedule.

Where lights out is in the F1 data (verified on the recorded 2026 Japanese GP race,
data/recordings/sample-2026-japan-race.json.gz):
* ``SessionData.StatusSeries`` {"SessionStatus": "Started", "Utc": "...05:14:02.078Z"} - the F1
  archive has NO ``SessionStatus`` topic at all, so this is the only exact source there;
* ``SessionStatus`` {"Status": "Started"} - live feed;
* OpenF1 race_control "SESSION STARTED" (same millisecond);
* ``ExtrapolatedClock`` starts running - fallback, whole seconds (±1 s).

Run:  python -m unittest tests.test_lights_out
"""
import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from server.config import DATA_DIR  # noqa: E402

from server.openf1 import merge_refs, ref_events_from_openf1  # noqa: E402
from server.sources.replay import META_TOPICS, load_file  # noqa: E402
from server.sources.vod import ref_events_from_archive  # noqa: E402
from server.sync import SyncManager, parse_voyo_sample  # noqa: E402
from server.telemetry import parse_utc  # noqa: E402
from tests.test_sync_start import MIN, SCHED, LiveRace, iso  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
SAMPLE = DATA_DIR / "recordings" / "sample-2026-japan-race.json.gz"
FIX = json.loads((ROOT / "tests" / "fixtures" / "openf1_2026_japan.json").read_text())
JP_START = parse_utc("2026-03-29T05:14:02.078Z").timestamp() * 1000
JP_SCHED = parse_utc("2026-03-29T05:00:00Z").timestamp() * 1000


class LiveLightsOutTest(unittest.TestCase):
    def l_press(self, r):
        with self.assertLogs("sync", "INFO") as logs:
            msg = r.sync.add_event_anchor("start", r.mono, r.now, None, "START")
        return msg, "\n".join(logs.output)

    def test_available_normal_race_successful_sync(self):
        r = LiveRace(self, stream_delay_s=0.5, start_ms=SCHED)
        r.to_video_showing(SCHED)
        msg, logs = self.l_press(r)
        self.assertIn("LIGHTS OUT ✓ F1 13:00:00", msg)
        self.assertIn("HIGH", msg)
        for line in ("[SYNC] Looking for Lights Out event", "[SYNC] Lights Out event found: 13:00:00.000 (F1 SignalR · SessionStatus)",
                     "[SYNC] Lights Out F1 timestamp: 13:00:00.000 UTC", "[SYNC] VOYO reference time: video",
                     "[SYNC] Calculated stream offset:", "[SYNC] Sync confidence: HIGH"):
            self.assertIn(line, logs)
        st = r.state()
        self.assertEqual(st["confidence"], "HIGH")
        self.assertTrue(st["lightsOut"]["available"])
        self.assertTrue(st["lightsOut"]["last"]["found"])
        self.assertAlmostEqual(r.shown(), SCHED, delta=50)

    def test_other_topics_status_series_and_session_clock(self):
        # the start only in SessionData.StatusSeries (no SessionStatus message)
        r = LiveRace(self, stream_delay_s=30, start_ms=SCHED + 90_000, start_via="StatusSeries")
        r.to_video_showing(SCHED + 90_000)
        msg, _ = self.l_press(r)
        self.assertIn("LIGHTS OUT ✓", msg)
        self.assertEqual(r.state()["lightsOut"]["last"]["source"], "F1 SignalR · SessionData.StatusSeries")
        self.assertAlmostEqual(r.shown(), SCHED + 90_000, delta=50)
        # only the session clock: ±1 s fallback, never presented as exact
        c = LiveRace(self, stream_delay_s=30, start_ms=SCHED, start_via="clock")
        c.to_video_showing(SCHED)
        msg, _ = self.l_press(c)
        self.assertIn("LIGHTS OUT ✓", msg)
        st = c.state()
        self.assertEqual(st["lightsOut"]["last"]["source"], "F1 SignalR · ExtrapolatedClock ±1 s")
        self.assertEqual(st["confidence"], "MEDIUM")
        self.assertEqual(st["anchors"][0]["kind"], "clock")
        self.assertAlmostEqual(c.shown(), SCHED, delta=1100)

    def test_delayed_race_uses_the_actual_lights_out(self):
        r = LiveRace(self, stream_delay_s=1, start_ms=SCHED + 4 * MIN + 37_000,
                     notices=[(SCHED - MIN, "RACE START DELAYED")])
        r.to_video_showing(SCHED + 4 * MIN + 37_000)
        msg, logs = self.l_press(r)
        self.assertIn("F1 13:04:37", msg)                          # not the scheduled 13:00
        self.assertIn("race delay +4:37", logs)
        self.assertAlmostEqual(r.shown(), SCHED + 4 * MIN + 37_000, delta=50)

    def test_delayed_voyo_stream_offset(self):
        r = LiveRace(self, stream_delay_s=4 * 60 + 15, start_ms=SCHED)
        r.to_video_showing(SCHED)
        msg, _ = self.l_press(r)
        self.assertIn("STREAM DELAY +4:15", msg)                   # the estimate assumed 5 s: still found
        last = r.state()["lightsOut"]["last"]
        self.assertAlmostEqual(last["streamDelaySeconds"], 255.0, delta=0.1)
        self.assertEqual(last["voyoUtc"][:8], "13:04:15")
        self.assertAlmostEqual(r.shown(), SCHED, delta=50)

    def test_both_delayed_not_combined(self):
        # the example: lights out 15:04:37 (CEST), the video shows it at 15:08:52
        lo = SCHED + 4 * MIN + 37_000
        r = LiveRace(self, stream_delay_s=4 * 60 + 15, start_ms=lo, notices=[(SCHED - MIN, "START DELAYED")])
        r.to_video_showing(lo)
        msg, _ = self.l_press(r)
        self.assertIn("LIGHTS OUT ✓ F1 13:04:37 · VOYO 13:08:52 · STREAM DELAY +4:15", msg)
        st = r.state()
        self.assertEqual(st["startInfo"]["f1DelaySeconds"], 277.0)
        self.assertAlmostEqual(st["streamDelaySeconds"], 255.0, delta=0.1)   # not 8:52

    def test_not_received_yet_is_explained(self):
        r = LiveRace(self, stream_delay_s=10, start_ms=None)
        r.step(25 * 60)                                            # 14:55, before the start
        with self.assertLogs("sync", "WARNING") as logs:
            msg = r.sync.add_event_anchor("start", r.mono, r.now, None, "START")
        self.assertIn("LIGHTS OUT NOT FOUND - not received yet", msg)
        self.assertIn("PRE-START", msg)
        text = "\n".join(logs.output)
        self.assertIn("[SYNC] Lights Out not found", text)
        self.assertIn("[SYNC] Checked topics: SessionData", text)
        self.assertIn("[SYNC] Session: Test Grand Prix Race", text)
        lo = r.state()["lightsOut"]
        self.assertFalse(lo["available"])
        self.assertEqual(lo["code"], "not_yet")
        self.assertEqual(lo["last"]["found"], False)

    def test_unknown_session_and_parse_error(self):
        s = SyncManager({"enabled": True}, voyo_enabled=True, legacy_delay=0, source_speed=1.0)
        s.update(parse_voyo_sample({"playback_time": 10.0, "paused": True, "asset": "x"}, 0.0, 1.0), SCHED)
        s.target(1.0, SCHED)
        self.assertIn("wrong or unknown session", s.add_event_anchor("start", 1.0, SCHED, None, "START"))
        r = LiveRace(self, stream_delay_s=10, start_ms=None)
        r.sync.observe_feed("SessionData", {"StatusSeries": {"2": {"Utc": "not a time", "SessionStatus": "Started"}}},
                            r.now, False)
        self.assertIn("could not be parsed", r.sync.add_event_anchor("start", r.mono, r.now, None, "START"))

    def test_multiple_points_lights_out_high_weight(self):
        r = LiveRace(self, stream_delay_s=20, start_ms=SCHED)
        r.to_video_showing(SCHED)
        self.l_press(r)
        k = r.sync.mapping.offset
        # a line crossing pressed 1.2 s late: lights out keeps the sync (counted twice in the median)
        cross = SCHED + 90_000
        r.to_video_showing(cross + 1200)
        r.sync.add_event_anchor("lap", r.mono, r.now, "1", "CAR 1")
        self.assertAlmostEqual(r.sync.mapping.offset, k, places=3)
        # two good crossings agreeing with lights out: HIGH, the late one is rejected as an outlier
        for lap in (2, 3):
            r.to_video_showing(SCHED + lap * 90_000)
            r.sync.add_event_anchor("lap", r.mono, r.now, "1", "CAR 1")
        st = r.state()
        self.assertEqual(st["confidence"], "HIGH")
        self.assertIn("outlier", st["reason"])
        self.assertAlmostEqual(r.sync.mapping.offset, k, delta=0.01)

    def test_red_flag_restart_picks_the_race_start_without_a_sync(self):
        r = LiveRace(self, stream_delay_s=60, start_ms=SCHED)
        r.step(45 * 60)                                            # race running; red flag; restart at 13:40
        restart = SCHED + 40 * MIN
        r.sync.observe_feed("SessionData", {"StatusSeries": {"5": {"Utc": iso(restart), "SessionStatus": "Started"}}},
                            restart, False)
        self.assertEqual(r.sync.start_info(None)["actual"], SCHED)   # lights out = before the first lap
        # without a reliable sync the video at the restart (estimate is ~55 s off) -> the restart
        r.to_video_showing(restart)
        msg, _ = self.l_press(r)
        self.assertIn("F1 13:40:00", msg)
        self.assertAlmostEqual(r.shown(), restart, delta=50)


class VodLightsOutTest(unittest.TestCase):
    """Older GP / VOD: the real recorded 2026 Japanese GP race (F1 archive)."""

    @classmethod
    def setUpClass(cls):
        cls.events = load_file(SAMPLE)

    def vod(self, ref):
        sess = {**next(x for x in FIX["sessions"] if x["session_key"] == 11253), "gmt_offset": "09:00:00"}
        s = SyncManager({"enabled": True}, voyo_enabled=True, legacy_delay=0, source_speed=1.0, vod=True)
        s.initialize(sess, ref)
        return s

    def press_at(self, s, f1_ms, k):
        s.update(parse_voyo_sample({"playback_time": f1_ms / 1000 - k, "paused": True, "asset": "voyo-media:jp"},
                                   0.0, 5.0), 0.0)
        s.target(5.0, 0.0)
        return s.add_event_anchor("start", 5.0, 0.0, None, "START")

    def test_archive_has_lights_out_in_status_series(self):
        topics = {e.topic for e in self.events}
        self.assertNotIn("SessionStatus", topics)                  # why it said "no data" before
        ref = ref_events_from_archive(self.events)
        self.assertEqual(ref.starts, [JP_START])
        self.assertEqual(ref.start_src[JP_START], "F1 archive · SessionData.StatusSeries")
        self.assertEqual(len(ref.finishes), 1)
        # before the big download: the small META topics already have it
        meta = ref_events_from_archive([e for e in self.events if e.topic in META_TOPICS])
        self.assertEqual(meta.starts, [JP_START])

    def test_historical_lights_out_syncs_a_recording(self):
        k = (JP_SCHED - (28 * 60 + 50) * 1000) / 1000                # video 0:00 = 04:31:10 UTC
        s = self.vod(ref_events_from_archive([e for e in self.events if e.topic in META_TOPICS]))
        msg = self.press_at(s, JP_START, k)
        self.assertIn("LIGHTS OUT ✓ F1 05:14:02 · VIDEO 42:52", msg)
        self.assertIn("HIGH", msg)
        self.assertAlmostEqual(s.mapping.offset, k, places=2)        # the delayed start, not the 05:00 schedule
        st = s.get_state(5.0, 0.0, None)
        self.assertEqual(st["lightsOut"]["source"], "F1 archive · SessionData.StatusSeries")
        self.assertIsNone(st["lightsOut"]["last"]["streamDelaySeconds"])
        self.assertAlmostEqual(st["startInfo"]["f1DelaySeconds"], 842.1, places=1)

    def test_openf1_without_race_control_is_merged_with_the_archive(self):
        of1 = ref_events_from_openf1(FIX["laps"], [])               # laps only (race_control failed / empty)
        self.assertEqual(of1.starts, [])
        merged = merge_refs(of1, ref_events_from_archive([e for e in self.events if e.topic in META_TOPICS]))
        self.assertEqual(merged.starts, [JP_START])
        self.assertTrue(merged.crossings)                          # OpenF1 lap crossings kept
        both = merge_refs(ref_events_from_openf1(FIX["laps"], FIX["race_control"]), ref_events_from_archive(self.events))
        self.assertEqual(both.starts, [JP_START])                  # same start from two sources = one start

    def test_vod_without_any_timing_data_is_explained(self):
        from server.openf1 import RefEvents
        s = self.vod(RefEvents(source="none"))
        msg = self.press_at(s, JP_START, (JP_SCHED - 1_800_000) / 1000)
        self.assertIn("VOD timing data unavailable", msg)
        self.assertEqual(s.get_state(5.0, 0.0, None)["lightsOut"]["code"], "vod_unavailable")

    def test_wrong_session_with_a_good_sync(self):
        k = (JP_SCHED - (28 * 60 + 50) * 1000) / 1000
        s = self.vod(ref_events_from_archive(self.events))
        s.update(parse_voyo_sample({"playback_time": JP_SCHED / 1000 - 600 - k, "paused": True,
                                    "asset": "voyo-media:jp"}, 0.0, 5.0), 0.0)
        s.target(5.0, 0.0)
        s.add_countdown_anchor(600, 5.0, 0.0, "10:00", target="scheduled")
        s.ref.starts.append(JP_START + 3600_000)                  # a second start (restart) far away
        # L pressed 30 min before lights out on the video: nothing in the window of the good sync
        self.assertIn("none within", self.press_at(s, JP_START - 1800_000, k))


if __name__ == "__main__":
    unittest.main()
