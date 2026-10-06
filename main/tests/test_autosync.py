"""VOYO AUTO SYNC: stream instances, LIVE DATA DELAY, states, recommended sync, persistence,
plus the recording and the deployment layout (main/ + server/ + pc variant/).

Run (from main/):  python -m unittest tests.test_autosync
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from server.autosync import LiveDataDelay, StreamTracker, evaluate  # noqa: E402
from server.config import PROJECT_ROOT, load_config  # noqa: E402
from server.sync import SyncManager, parse_voyo_sample  # noqa: E402
from server.timeline import Timeline  # noqa: E402
from test_sync import FIX, START_MS, T0, _ms  # noqa: E402

MAIN = PROJECT_ROOT
REPO = MAIN.parent


def smp(pb, *, paused=False, ts=0.0, asset="voyo|live1", duration=None, events=None, page=None, ready=4):
    body = {"playback_time": pb, "paused": paused, "playback_rate": 1.0, "ready_state": ready, "asset": asset,
            "duration": duration, "events": events or [], "page": page or {}}
    return parse_voyo_sample(body, 0.0, ts)


# ---------------------------------------------------------------------------------------------
class LiveDataDelayTest(unittest.TestCase):
    def test_no_data_settling_stable(self):
        d = LiveDataDelay()
        self.assertEqual(d.snapshot(0)["state"], "NO DATA")
        for i in range(10):
            d.add(i * 0.5, 3.2)
        self.assertEqual(d.snapshot(5)["state"], "SETTLING")
        for i in range(10, 60):
            d.add(i * 0.5, 3.2 + (0.1 if i % 2 else -0.1))
        s = d.snapshot(30)
        self.assertEqual((s["state"], s["seconds"]), ("STABLE", 3.2))
        self.assertLessEqual(s["spread"], 0.35)
        self.assertTrue(s["history"])
        self.assertFalse(s["clockWarning"])

    def test_noisy_stale_and_window(self):
        d = LiveDataDelay()
        for i in range(60):
            d.add(i * 0.5, 2.0 + (i % 5) * 1.0)          # 2..6 s jumping around
        self.assertEqual(d.snapshot(30)["state"], "NOISY")
        self.assertIsNone(d.stable_seconds())
        self.assertEqual(d.snapshot(30 + 31)["state"], "STALE")
        d.add(200, 4.0)                                   # older than 60 s dropped
        self.assertEqual(len(d.samples), 1)

    def test_negative_delay_flags_the_server_clock_and_absurd_values_are_ignored(self):
        d = LiveDataDelay()
        d.add(0, float("nan"))
        d.add(0, 5000)
        self.assertEqual(len(d.samples), 0)
        for i in range(30):
            d.add(i, -2.0)
        self.assertTrue(d.snapshot(30)["clockWarning"])

    def test_timeline_feeds_the_live_data_delay(self):
        tl, got = Timeline(), []
        tl.on_latency = got.append
        tl.ingest("TrackStatus", {"Status": "1"}, T0, T0 + 2500)
        tl.ingest("TrackStatus", {"Status": "1"}, T0, T0 + 9000, snapshot=True)    # snapshot: not live latency
        self.assertEqual(got, [2500])


# ---------------------------------------------------------------------------------------------
class StreamTrackerTest(unittest.TestCase):
    def setUp(self):
        self.t = StreamTracker({})
        self.wall = 1_780_000_000.0

    def obs(self, s, dt=1.0):
        self.wall += dt
        return self.t.observe(s, {}, self.wall)

    def test_first_stream_and_base_anchor(self):
        self.assertEqual(self.obs(smp(3.0)), "first stream seen")
        inst = self.t.current
        self.assertTrue(inst.live)
        self.assertAlmostEqual(inst.origin_wall, self.wall - 3.0, places=3)     # position 0:00 seen
        self.assertIsNone(self.obs(smp(4.0)))
        t2 = StreamTracker({})
        t2.observe(smp(3600.0, asset="b"), {}, self.wall)
        self.assertIsNone(t2.current.origin_wall)                                 # joined late: unknown
        self.assertIn("joined at position", t2.current.origin_how)

    def test_seek_pause_buffering_never_start_an_instance(self):
        self.obs(smp(500.0))
        iid = self.t.current.id
        self.assertIsNone(self.obs(smp(400.0, events=[{"type": "seeking"}, {"type": "seeked"}])))
        self.assertIsNone(self.obs(smp(400.0, paused=True)))
        self.assertIsNone(self.obs(smp(400.0, ready=2, events=[{"type": "waiting"}])))
        self.assertIsNone(self.obs(smp(100.0)))           # DVR jump back without reload events
        self.assertEqual(self.t.current.id, iid)

    def test_new_video_media_id_and_shorter_recording(self):
        self.obs(smp(10.0, asset="a"))
        self.assertEqual(self.obs(smp(10.0, asset="b")), "another VOYO video / stream")
        self.obs(smp(10.0, asset="c", duration=7200, page={"media_id": "1"}))
        self.t.current.media_id = "1"
        self.t.current.asset = "c"
        s = smp(10.0, asset="c", duration=7200)
        s.page = {"media_id": "2"}
        self.assertEqual(self.obs(s), "another media id on the same page")
        self.obs(smp(10.0, asset="d", duration=7200))
        self.assertEqual(self.obs(smp(10.0, asset="d", duration=3600)),
                         "another recording (shorter) on the same page")

    def test_live_restart_by_page_reload_or_player_reload(self):
        self.obs(smp(900.0, page={"load_id": "L1"}))
        a = self.t.current.id
        self.assertEqual(self.obs(smp(905.0, page={"load_id": "L2"})), "live stream restarted (player / page reload)")
        b = self.t.current.id
        self.assertNotEqual(a, b)
        self.obs(smp(950.0, page={"load_id": "L2"}))
        self.assertEqual(self.obs(smp(2.0, page={"load_id": "L2"}, events=[{"type": "loadstart"}])),
                         "live stream restarted (player / page reload)")
        self.assertNotEqual(self.t.current.id, b)

    def test_length_arriving_late_or_growing(self):
        self.obs(smp(10.0, asset="r"))
        self.assertTrue(self.t.current.live)
        self.obs(smp(11.0, asset="r", duration=6000))
        self.assertFalse(self.t.current.live)                                    # a recording after all
        self.obs(smp(12.0, asset="r", duration=6100))
        self.assertTrue(self.t.current.live)                                     # DVR window keeps growing

    def test_resume_after_server_restart(self):
        store = {"instances": {}}
        t = StreamTracker(store)
        t.observe(smp(100.0, asset="v", duration=7200), {}, self.wall)
        store["instances"][t.current.id] = t.current.to_json()
        t2 = StreamTracker(store)                                                 # server restarted
        self.assertEqual(t2.observe(smp(3000.0, asset="v", duration=7201), {}, self.wall + 600), "resumed")
        self.assertEqual(t2.current.id, t.current.id)
        t3 = StreamTracker(store)
        self.assertEqual(t3.observe(smp(10.0, asset="v", duration=3600), {}, self.wall + 600), "first stream seen")
        # live: only when the position continued with the clock
        store = {"instances": {}}
        t = StreamTracker(store)
        t.observe(smp(1000.0), {}, self.wall)
        store["instances"][t.current.id] = t.current.to_json()
        self.assertEqual(StreamTracker(store).observe(smp(1060.0), {}, self.wall + 60), "resumed")
        self.assertEqual(StreamTracker(store).observe(smp(5.0), {}, self.wall + 60), "first stream seen")


# ---------------------------------------------------------------------------------------------
class EvaluateTest(unittest.TestCase):
    def ev(self, **kw):
        m = SimpleNamespace(offset=100.0, confidence="HIGH", source="anchors", method="Event Sync",
                            reason="2 points agree", deviation=0.1)
        args = dict(enabled=True, video=True, instance=object(), clock_state="PLAYING", session={"x": 1},
                    mapping=m, k_applied=100.0, observations=2, outliers=0, pending=None, latency_note=None)
        for k, v in kw.items():
            if k in ("offset", "confidence", "source", "deviation"):
                setattr(m, k, v)
            else:
                args[k] = v
        return evaluate(**args)

    def test_states(self):
        self.assertEqual(self.ev(enabled=False)["state"], "OFF")
        self.assertEqual(self.ev(instance=None)["state"], "SEARCHING")
        self.assertEqual(self.ev(session=None)["state"], "SEARCHING")
        self.assertEqual(self.ev(offset=None, observations=0)["state"], "SEARCHING")
        self.assertEqual(self.ev(observations=0, confidence="LOW", source="broadcast-delay-estimate")["state"],
                         "CALIBRATING")
        locked = self.ev()
        self.assertEqual(locked["state"], "LOCKED")
        self.assertGreater(locked["confidence"], 0.8)
        self.assertEqual(self.ev(k_applied=97.0)["state"], "CALIBRATING")       # slewing to the new offset
        self.assertEqual(self.ev(pending={"shift": 3.0})["state"], "UNSTABLE")
        self.assertEqual(self.ev(outliers=2, observations=1)["state"], "UNSTABLE")
        self.assertEqual(self.ev(confidence="LOW")["state"], "UNSTABLE")
        for cs in ("PAUSED", "BUFFERING", "SEEKING", "STALE"):
            r = self.ev(clock_state=cs)
            self.assertEqual((r["state"], r["confidence"]), ("HOLD", 0.0))
            self.assertIn("not drift", r["reason"])


# ---------------------------------------------------------------------------------------------
class VodAutoSyncTest(unittest.TestCase):
    """A recording: event points calibrate, session / video isolation, persistence."""

    def setUp(self):
        from server.openf1 import ref_events_from_openf1
        self.session = next(x for x in FIX["sessions"] if x["session_key"] == 11253)
        self.ref = ref_events_from_openf1(FIX["laps"], FIX["race_control"])
        self.K = (_ms(self.session["date_start"]) - (28 * 60 + 50) * 1000) / 1000
        self.mono = 0.0
        self.dir = tempfile.mkdtemp()
        self.path = Path(self.dir) / "cal.json"

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def make(self, session=None):
        s = SyncManager({"enabled": True}, True, 0, 1.0, self.path, vod=True)
        s.initialize(session or self.session, self.ref)
        return s

    def at(self, s, f1_ms, paused=True, media="m1", duration=9000.0):
        self.mono += 1.0
        s.update(smp(f1_ms / 1000 - self.K, paused=paused, ts=self.mono, asset="x", duration=duration,
                     page={"media_id": media}), T0)
        s.target(self.mono, T0)

    def test_calibrate_lock_points_carry_instance_and_persist(self):
        s = self.make()
        self.at(s, START_MS, paused=False)
        st = s.autosync_state(self.mono, T0)
        self.assertEqual(st["state"], "SEARCHING")                  # no F1 reference yet
        self.assertIsNone(st["videoZeroUtc"])
        s.add_event_anchor("start", self.mono, T0, None, "L")
        s.target(self.mono, T0)
        st = s.autosync_state(self.mono, T0)
        self.assertEqual(st["state"], "LOCKED")
        self.assertEqual(st["instance"]["id"], s.tracker.current.id)
        self.assertTrue(all(a.instance_id == s.tracker.current.id for a in s.anchors))
        saved = json.loads(self.path.read_text())
        inst = saved["autosync"]["instances"][s.tracker.current.id]
        self.assertEqual((inst["session_key"], inst["confidence"]), (11253, "HIGH"))
        self.assertAlmostEqual(inst["offset"], self.K, delta=0.3)       # L while playing: reaction time
        self.assertIn("instance_id", saved["assets"]["voyo-media:m1"]["anchors"][0])
        # VOD: the video-zero time is from the anchors, never from the server's wall clock
        self.assertEqual(st["videoZeroUtc"][:5], "04:31")                 # 04:31:10 UTC, the recording's 0:00
        self.assertIsNone(st["totalDelaySeconds"])
        # pause = HOLD, not drift
        self.at(s, START_MS + 5000, paused=True)
        self.assertEqual(s.autosync_state(self.mono, T0)["state"], "HOLD")

    def test_reopen_resumes_other_video_and_other_session_start_clean(self):
        s = self.make()
        self.at(s, START_MS)
        s.add_event_anchor("start", self.mono, T0, None, "L")
        iid = s.tracker.current.id
        r = self.make()                                                 # server restart, same recording
        self.at(r, START_MS + 60_000)
        self.assertEqual((r.tracker.current.id, r.tracker.current.resumed), (iid, True))
        self.assertEqual(r.mapping.confidence, "HIGH")
        o = self.make()                                                 # another recording, same session
        self.at(o, START_MS, media="m2")
        self.assertNotEqual(o.tracker.current.id, iid)
        self.assertEqual(o.mapping.confidence, "UNSYNCED")              # inherits nothing
        g = self.make({**self.session, "session_key": 11249})          # same video, other session
        self.at(g, START_MS)
        self.assertEqual(g.mapping.confidence, "UNSYNCED")

    def test_switching_video_in_one_server_does_not_inherit_the_offset(self):
        s = self.make()
        self.at(s, START_MS)
        s.add_event_anchor("start", self.mono, T0, None, "L")
        self.assertEqual(s.mapping.confidence, "HIGH")
        self.at(s, START_MS, media="m2")
        self.assertEqual((s.mapping.confidence, s.anchors), ("UNSYNCED", []))
        self.assertIsNone(s.target(self.mono, T0).ms)


# ---------------------------------------------------------------------------------------------
class LiveAutoSyncTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.path = Path(self.dir) / "cal.json"

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def make(self):
        return SyncManager({"enabled": True, "mode": "AUTO", "broadcast_delay_seconds": 5.0}, True, 0.0, 1.0,
                           self.path)

    def test_recommended_sync_basis_never_changes_the_setting(self):
        s = self.make()
        s.update(smp(100.0, ts=1.0), T0)
        self.assertEqual(s.recommended_latency(), (5.0, "configured broadcast_delay_seconds"))
        import time
        now = time.monotonic()
        for i in range(40):
            s.live_delay.add(now - 40 + i, 7.0)
        lat, how = s.recommended_latency()
        self.assertEqual(lat, 7.0)
        self.assertIn("LIVE DATA DELAY", how)
        s.store.data.setdefault("autosync", {})["live_latency"] = {"voyo|live1": [21.0, 22.0, 23.0]}
        self.assertEqual(s.recommended_latency()[0], 22.0)
        self.assertEqual(s.default_delay, 5.0)                          # configured value untouched
        st = s.autosync_state(1.0, T0)
        self.assertEqual(st["recommendedSync"]["configured"], 5.0)
        self.assertEqual(st["recommendedSync"]["seconds"], 22.0)

    def test_live_restart_starts_from_the_recommended_latency_not_the_old_offset(self):
        s = self.make()
        s.store.data.setdefault("autosync", {})["live_latency"] = {"voyo|live1": [30.0]}
        s.update(smp(500.0, ts=1.0, page={"load_id": "A"}), T0)
        k1, i1 = s.est_K, s.tracker.current.id
        self.assertAlmostEqual(k1, T0 / 1000 - 30.0 - 500.0, places=3)
        s.est_K = 12345.0                                               # whatever the first stream had
        s.update(smp(3.0, ts=2.0, page={"load_id": "B"}), T0 + 1000)
        self.assertNotEqual(s.tracker.current.id, i1)
        self.assertAlmostEqual(s.est_K, (T0 + 1000) / 1000 - 30.0 - 3.0, places=3)
        self.assertEqual(s.anchors, [])

    def test_total_delay_split_into_data_and_voyo_parts(self):
        s = self.make()
        s.initialize({"session_key": 1, "session_name": "Race"}, None)
        for i in range(40):
            s.live_delay.add(1.0 + i * 0.1, 3.0)
        s.update(smp(100.0, ts=5.0), T0)
        s.target(5.0, T0)
        st = s.autosync_state(5.0, T0)
        self.assertIsNotNone(st["totalDelaySeconds"])
        self.assertAlmostEqual(st["totalDelaySeconds"], 5.0, places=1)     # = broadcast_delay estimate
        self.assertAlmostEqual(st["voyoSyncSeconds"], st["totalDelaySeconds"] - 3.0, places=1)
        self.assertEqual(st["state"], "CALIBRATING")                         # estimate only, no F1 event yet
        self.assertIn("autoSync", s.get_state(5.0, T0, None))


# ---------------------------------------------------------------------------------------------
class RecordingTest(unittest.TestCase):
    def test_recorded_feed_replays(self):
        from server.recorder import Recorder
        from server.sources.replay import load_file
        with tempfile.TemporaryDirectory() as d:
            r = Recorder(Path(d))
            ts = datetime(2026, 3, 29, 5, 14, 2, tzinfo=timezone.utc)
            r.write("TrackStatus", {"Status": "1"}, ts, True)              # before the session is known
            r.set_session("2026/2026-03-29_Japanese_Grand_Prix/2026-03-29_Race/", "Race")
            r.write("RaceControlMessages", {"Messages": [{"Message": "GREEN LIGHT"}]}, ts, False)
            r.close()
            files = list(Path(d).glob("*.jsonl.gz"))
            self.assertEqual(len(files), 1)
            ev = load_file(files[0])
            self.assertEqual([e.topic for e in ev], ["TrackStatus", "RaceControlMessages"])
            self.assertTrue(ev[0].snap)


# ---------------------------------------------------------------------------------------------
class DeploymentLayoutTest(unittest.TestCase):
    def test_server_overlay(self):
        cfg = load_config(overlays=[str(REPO / "server" / "config" / "server.toml")])
        self.assertEqual(cfg["server"]["host"], "0.0.0.0")
        self.assertTrue(cfg["sync"]["allow_remote_clock"])
        self.assertFalse(cfg["f1_tv"]["open_browser"])
        base = load_config()
        self.assertFalse(base["sync"]["allow_remote_clock"])            # PC default unchanged
        self.assertEqual(base["voyo"]["capture_compat"], "no-gpu")       # AirParrot capture3 default

    def test_launch_scripts_point_at_main(self):
        bat = (REPO / "pc variant" / "launch.bat").read_text()
        for ref in (r"main\requirements.txt", r"main\tools\tv_launcher.py"):
            self.assertIn(ref, bat)
            self.assertTrue((REPO / ref.replace("\\", "/")).exists(), ref)
        self.assertIn('cd /d "%~dp0.."', bat)
        self.assertIn("capture3", bat)
        self.assertIn(b"\r\n", (REPO / "pc variant" / "launch.bat").read_bytes())
        self.assertIn(r'pc variant\launch.bat', (REPO / "launch.bat").read_text())
        sh = REPO / "server" / "launch.sh"
        self.assertTrue(os.access(sh, os.X_OK))
        self.assertEqual(subprocess.run(["bash", "-n", str(sh)]).returncode, 0)
        self.assertIn("main/requirements.txt", sh.read_text())
        unit = (REPO / "server" / "systemd" / "f1-dashboard.service").read_text()
        self.assertIn("ExecStart=/opt/f1-dashboard/server/launch.sh", unit)

    def test_tv_launcher_runs_from_main_and_keeps_the_voyo_profile(self):
        env = {k: v for k, v in os.environ.items() if k != "F1DASH_DATA_DIR"}
        out = subprocess.run([sys.executable, str(MAIN / "tools" / "tv_launcher.py"), "--print", "--browser",
                              "/bin/true"], capture_output=True, text=True, timeout=60, env=env).stdout
        self.assertIn("--disable-gpu", out)                              # capture3 / no-gpu
        self.assertIn(str(REPO / "data" / "browser-profiles" / "voyo"), out)


if __name__ == "__main__":
    unittest.main()
