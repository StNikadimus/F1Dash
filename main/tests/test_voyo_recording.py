"""VOYO stream recordings (server/voyo_recording.py): path selection, one package per stream
instance, timeline / anchors / observations, session association, index, list / load API,
capture upload, retention, PC capture client.

Run (from main/):  python -m unittest tests.test_voyo_recording
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from server.config import DATA_DIR, load_config  # noqa: E402
from server.openf1 import ref_events_from_openf1  # noqa: E402
from server.sync import SyncManager, parse_voyo_sample  # noqa: E402
from server.voyo_recording import (VoyoStreamRecorder, load_package, recalibrate, resolve_root,  # noqa: E402
                                   session_kind)
from test_sync import FIX, START_MS, T0, _ms  # noqa: E402
from tools.voyo_capture import VoyoWindowCapture, ffmpeg_cmd  # noqa: E402


class Harness:
    """SyncManager (VOD, Japan 2026 fixture) + recorder, fed like engine.voyo_sample does."""

    def __init__(self, root: Path, **rc):
        self.rec = VoyoStreamRecorder({"path": str(root), "timeline_interval_seconds": 0, "min_free_bytes": 0,
                                       **rc}).start()
        self.session = next(x for x in FIX["sessions"] if x["session_key"] == 11253)
        self.ref = ref_events_from_openf1(FIX["laps"], FIX["race_control"])
        self.K = (_ms(self.session["date_start"]) - (28 * 60 + 50) * 1000) / 1000
        self.mono = 0.0
        self.rec.now = lambda: 1_780_000_000.0 + self.mono          # server wall clock follows the test clock
        self.s = self.new_sync()

    def new_sync(self, session=True):
        s = SyncManager({"enabled": True}, True, 0, 1.0, None, vod=True)
        s.on_record = lambda k, d: self.rec.sync_event(k, d, s)
        if session:
            s.initialize(self.session, self.ref)
        self.s = s
        return s

    def feed(self, pb, paused=False, media="m1", events=None, duration=9000.0, ready=4, dt=1.0):
        self.mono += dt
        smp = parse_voyo_sample({"playback_time": pb, "paused": paused, "asset": "x", "duration": duration,
                                 "ready_state": ready, "events": events or [],
                                 "page": {"media_id": media, "title": "VN Japonske"}}, 0, self.mono)
        self.s.update(smp, T0)
        self.s.target(self.mono, T0)
        with mock.patch("time.monotonic", return_value=self.mono):
            self.rec.observe(smp, self.s.tracker.current, self.s.last_instance_reason, self.s)
        return smp

    def at(self, f1_ms, **kw):
        return self.feed(f1_ms / 1000 - self.K, **kw)

    def load(self, pkg: Path) -> dict:
        if self.rec.cur is not None and self.rec.ok:
            with mock.patch("time.monotonic", return_value=self.mono):
                self.rec.write_manifest(force=True)             # flushes the open files
        return load_package(pkg)


class PathSelectionTest(unittest.TestCase):
    def setUp(self):
        self.d = Path(tempfile.mkdtemp())

    def tearDown(self):
        shutil.rmtree(self.d, ignore_errors=True)

    def test_absolute_path_created_and_writable(self):
        root, err = resolve_root({"path": str(self.d / "usb" / "voyo")})
        self.assertIsNone(err)
        self.assertTrue(root.is_dir())

    def test_missing_mount_is_not_created_when_disabled(self):
        root, err = resolve_root({"path": str(self.d / "notmounted"), "create_path_if_missing": False})
        self.assertIn("does not exist", err)
        self.assertFalse((self.d / "notmounted").exists())
        rec = VoyoStreamRecorder({"path": str(self.d / "notmounted"), "create_path_if_missing": False}).start()
        self.assertFalse(rec.ok)
        self.assertIn("does not exist", rec.status()["error"])

    @unittest.skipIf(os.geteuid() == 0 if hasattr(os, "geteuid") else True, "root can write anywhere")
    def test_not_writable_fails_without_fallback(self):
        ro = self.d / "ro"
        ro.mkdir()
        ro.chmod(0o500)
        try:
            root, err = resolve_root({"path": str(ro)})
            self.assertIn("not writable", err)
        finally:
            ro.chmod(0o700)

    def test_require_mount_waits_for_the_disk(self):
        mnt = self.d / "f1disk"
        mnt.mkdir()
        rc = {"path": str(mnt / "voyo_streams"), "require_mount": str(mnt), "min_free_bytes": 0}
        with mock.patch("os.path.ismount", return_value=False):
            rec = VoyoStreamRecorder(rc).start()
        self.assertIn("no disk mounted", rec.status()["error"])
        self.assertFalse((mnt / "voyo_streams").exists())                  # nothing on the system disk
        rec._last_recheck = -1e9
        with mock.patch("os.path.ismount", side_effect=lambda p: Path(p) == mnt.resolve()):
            rec._recheck()                                                   # disk mounted later
        self.assertTrue(rec.ok)
        self.assertTrue((mnt / "voyo_streams").is_dir())
        _, err = resolve_root({"path": str(self.d / "elsewhere"), "require_mount": str(mnt)})
        self.assertIn("not on require_mount", err)

    def test_write_test_failure_reported(self):
        with mock.patch("pathlib.Path.write_bytes", side_effect=PermissionError("read-only file system")):
            root, err = resolve_root({"path": str(self.d / "x")})
        self.assertIn("not writable", err)

    def test_relative_paths_and_f1_recordings_kept_apart(self):
        root, err = resolve_root({"path": "data/voyo_streams_test_tmp"})
        try:
            self.assertEqual(root, (DATA_DIR / "voyo_streams_test_tmp").resolve())
        finally:
            shutil.rmtree(root, ignore_errors=True)
        f1 = self.d / "recordings"
        f1.mkdir()
        _, err = resolve_root({"path": str(f1)}, f1)
        self.assertIn("F1 timing recordings", err)
        _, err = resolve_root({"path": str(f1 / "voyo")}, f1)
        self.assertIn("F1 timing recordings", err)

    def test_config_and_env_override(self):
        cfg = load_config()
        rc = cfg["voyo"]["recording"]
        self.assertEqual(rc["path"], "data/voyo_streams")
        self.assertFalse(rc["record_video_capture"])                       # never on by default
        with mock.patch.dict(os.environ, {"F1DASH_VOYO_RECORDING_PATH": "/mnt/usb/voyo",
                                          "F1DASH_VOYO_RECORDING_KEEP_RACE_DAYS": "90"}):
            rc = load_config()["voyo"]["recording"]
        self.assertEqual((rc["path"], rc["keep_race_days"]), ("/mnt/usb/voyo", 90))


class PackageTest(unittest.TestCase):
    def setUp(self):
        self.d = Path(tempfile.mkdtemp())
        self.h = Harness(self.d)

    def tearDown(self):
        shutil.rmtree(self.d, ignore_errors=True)

    def pkgs(self):
        return sorted(p for p in self.d.iterdir() if p.is_dir())

    def test_one_package_for_play_pause_buffer_seek_and_gap(self):
        h = self.h
        h.at(START_MS - 60_000)
        h.at(START_MS - 59_000, paused=True)
        h.at(START_MS - 59_000, ready=2)                                     # buffering
        h.at(START_MS - 59_000)
        h.at(START_MS - 300_000, events=[{"type": "seeking"}, {"type": "seeked"}])
        h.at(START_MS - 299_000, dt=20)                                      # clock lost 20 s
        self.assertEqual(len(self.pkgs()), 1)
        pk = self.h.load(self.pkgs()[0])
        states = [t.get("state") or t.get("type") for t in pk["timeline"]]
        self.assertEqual(states, ["playing", "paused", "buffering", "playing", "playing", "gap", "playing"])
        self.assertEqual(pk["timeline"][4]["events"], ["seeking", "seeked"])
        self.assertEqual(pk["manifest"]["counts"]["gaps"], 1)
        self.assertTrue(all("t" in r and "pb" in r for r in pk["timeline"] if "state" in r))

    def test_identity_meta_session_and_mode(self):
        h = self.h
        h.rec.mode_info = lambda: {"selected_mode": "AUTO", "effective_mode": "VOD"}
        h.at(START_MS)
        pk = self.h.load(self.pkgs()[0])
        m = pk["manifest"]
        self.assertEqual(m["stream_instance_id"], h.s.tracker.current.id)
        self.assertEqual((m["media_id"], m["title"], m["live"], m["duration"]), ("m1", "VN Japonske", False, 9000.0))
        self.assertEqual(m["mode"], {"selected": "AUTO", "effective": "VOD", "source": "VOD"})
        self.assertEqual(m["session"]["session_key"], 11253)
        self.assertEqual(m["session"]["kind"], "race")
        self.assertTrue(m["detected_at"].endswith("Z") and m["stream_start_wall_time"])
        self.assertEqual(pk["meta"]["voyo"]["media_id"], "m1")
        self.assertEqual(pk["meta"]["detection"]["reason"], "first stream seen")

    def test_new_instance_closes_previous_and_inherits_nothing(self):
        h = self.h
        h.at(START_MS)
        h.s.add_event_anchor("start", h.mono, T0, None, "L")
        first = h.s.tracker.current.id
        h.at(START_MS, media="m2")                                           # another recording
        second = h.s.tracker.current.id
        self.assertNotEqual(first, second)
        a = h.load(self.d / first)
        b = h.load(self.d / second)
        self.assertEqual(a["manifest"]["status"], "closed")
        self.assertIn("new stream instance", a["manifest"]["close_reason"])
        self.assertEqual(len(a["anchors"]["anchors"]), 1)
        self.assertEqual(b["anchors"], {})                                   # no anchors carried over
        self.assertEqual(b["manifest"]["counts"]["anchors"], 0)
        self.assertEqual(h.s.mapping.confidence, "UNSYNCED")

    def test_anchors_observations_pairs_and_recalibration(self):
        h = self.h
        h.at(START_MS, paused=True)
        h.s.add_event_anchor("start", h.mono, T0, None, "L")
        for i in range(1, 25):
            h.at(START_MS + i * 1000)
        pk = self.h.load(self.pkgs()[0])
        obs = pk["observations"]
        anchor = next(o for o in obs if o["type"] == "anchor")
        self.assertEqual((anchor["status"], anchor["instance_id"]), ("applied", h.s.tracker.current.id))
        pairs = [o for o in obs if o["type"] == "pair"]
        self.assertGreaterEqual(len(pairs), 2)
        p = pairs[-1]
        self.assertAlmostEqual(p["f1_ms"] / 1000 - p["pb"], h.K, places=2)   # (position, wall, F1 time)
        self.assertEqual(p["quality"], "measured")
        cal = recalibrate(pk)
        self.assertEqual((cal["state"], cal["n"]), ("LOCKED", 1))
        self.assertAlmostEqual(cal["offset"], h.K, places=2)

    def test_pending_rejected_observations(self):
        h = self.h
        h.at(START_MS, paused=True)
        h.s.add_event_anchor("start", h.mono, T0, None, "L")
        h.at(START_MS + 5000, paused=True)
        h.mono += 1
        h.s.add_event_anchor("start", h.mono, T0, None, "L")               # pressed 5 s late: drift warning
        h.s.keep_old()
        obs = self.h.load(self.pkgs()[0])["observations"]
        st = [o["status"] for o in obs if o["type"] == "anchor"]
        self.assertEqual(st, ["applied", "pending", "rejected"])

    def test_mark_stream_start_recorded_separately(self):
        h = self.h
        h.feed(0.0, paused=True)
        h.s.mark_stream_start(h.mono, T0)
        h.s.reset_stream_start()
        pk = self.h.load(self.pkgs()[0])
        marks = pk["anchors"]["stream_start_marks"]
        self.assertEqual([m["type"] for m in marks], ["stream_start", "stream_start_reset"])
        self.assertIsNone(pk["anchors"]["stream_start_mark"])
        self.assertTrue(pk["anchors"]["stream_start_wall_time"])            # the automatic base anchor stays
        self.assertEqual(pk["manifest"]["stream_start_marks"], 1)

    def test_resume_after_restart_appends_and_crash_marks_interrupted(self):
        h = self.h
        store = {}
        h.s.tracker.store = store
        h.at(START_MS)
        iid = h.s.tracker.current.id
        store.setdefault("instances", {})[iid] = h.s.tracker.current.to_json()
        h.rec.write_manifest(force=True)
        # crash: no finalize; a new server process starts
        rec2 = VoyoStreamRecorder({"path": str(self.d), "timeline_interval_seconds": 0, "min_free_bytes": 0}).start()
        self.assertEqual(json.loads((self.d / iid / "manifest.json").read_text())["status"], "interrupted")
        h.rec = rec2
        h.new_sync()
        h.s.tracker.store = store
        h.at(START_MS + 60_000)
        self.assertEqual(h.s.last_instance_reason, "resumed")
        self.assertEqual(len(self.pkgs()), 1)
        m = json.loads((self.d / iid / "manifest.json").read_text())
        self.assertEqual(m["status"], "recording")
        self.assertEqual(len(m["resumes"]), 1)

    def test_index_list_and_session_isolation(self):
        h = self.h
        h.at(START_MS)
        h.new_sync(session=False)                                            # other recording, session unknown
        h.at(START_MS, media="m2")
        h.rec.close()
        idx = json.loads((self.d / "index.json").read_text())
        self.assertEqual(len(idx["recordings"]), 2)
        lst = h.rec.list()
        keys = sorted(str(r["session_key"]) for r in lst)
        self.assertEqual(keys, ["11253", "None"])
        (self.d / "index.json").unlink()                                     # rebuilt when missing
        self.assertEqual(len(h.rec.list()), 2)

    def test_write_error_stops_and_recovers_without_other_disk(self):
        h = self.h
        h.at(START_MS)
        with mock.patch.object(h.rec, "_line", side_effect=OSError("No space left on device")):
            h.at(START_MS + 1000)
        self.assertFalse(h.rec.ok)
        self.assertIn("No space left", h.rec.status()["error"])
        h.at(START_MS + 2000)                                                # still failed: nothing written
        h.rec._last_recheck = -1e9
        h.at(START_MS + 3000)
        self.assertTrue(h.rec.ok)
        obs = self.h.load(self.pkgs()[0])["observations"]
        self.assertTrue(any("write error" in (o.get("text") or "") for o in obs))

    def test_disabled_writes_nothing(self):
        d = self.d / "off"
        rec = VoyoStreamRecorder({"enabled": False, "path": str(d)}).start()
        self.assertFalse(rec.ok)
        self.assertFalse(d.exists())


class CaptureAndRetentionTest(unittest.TestCase):
    def setUp(self):
        self.d = Path(tempfile.mkdtemp())

    def tearDown(self):
        shutil.rmtree(self.d, ignore_errors=True)

    def test_capture_target_rules(self):
        h = Harness(self.d)
        h.at(START_MS)
        iid = h.s.tracker.current.id
        self.assertIn("off", h.rec.capture_target(iid, "a.mp4", 10)[1])     # opt-in only
        self.assertFalse(h.rec.clock_reply()["capture"])
        h.rec.capture_enabled = True
        self.assertTrue(h.rec.clock_reply()["capture"])
        self.assertEqual(h.rec.capture_target("nope", "a.mp4", 10)[1], "unknown stream instance")
        self.assertEqual(h.rec.capture_target(iid, "../x.mp4", 10)[1], "bad file name")
        self.assertEqual(h.rec.capture_target(iid, "a.exe", 10)[1], "bad file name")
        path, err = h.rec.capture_target(iid, "run1_seg_00000.mp4", 10)
        self.assertIsNone(err)
        path.write_bytes(b"x" * 10)
        h.rec.capture_stored(iid, path, {"pc_start_epoch": 1.0})
        self.assertEqual(h.rec.cur["capture"]["segments"], ["run1_seg_00000.mp4"])
        self.assertEqual(load_package(self.d / iid)["capture"][0]["bytes"], 10)

    def test_retention_by_session_kind_keeps_metadata(self):
        h = Harness(self.d, keep_race_days=30, keep_practice1_days=0)
        h.at(START_MS)
        iid = h.s.tracker.current.id
        h.rec.capture_enabled = True
        p, _ = h.rec.capture_target(iid, "s.mp4", 1)
        p.write_bytes(b"1")
        h.rec.capture_stored(iid, p, {})
        h.rec.close()
        self.assertEqual(h.rec.apply_retention(), 0)                         # 30 days not over
        h.rec.now = lambda: time.time() + 31 * 86400
        self.assertEqual(h.rec.apply_retention(), 1)
        self.assertFalse(p.exists())
        m = json.loads((self.d / iid / "manifest.json").read_text())
        self.assertIn("race video kept 30 days", m["capture"]["deleted_reason"])
        self.assertTrue((self.d / iid / "timeline.jsonl").exists())

    def test_session_kinds(self):
        for name, kind in (("Practice 1", "practice1"), ("Practice 3", "practice3"), ("Qualifying", "qualifying"),
                           ("Sprint Qualifying", "sprint_qualifying"), ("Sprint Shootout", "sprint_qualifying"),
                           ("Sprint", "sprint"), ("Race", "race"), (None, "other"), ("Day 1", "other")):
            self.assertEqual(session_kind(name), kind, name)

    def test_ffmpeg_command_is_a_window_capture(self):
        win = ffmpeg_cmd("ffmpeg", {"title": "VN Japonske - VOYO"}, self.d, "run1", 30, 23, 60)
        self.assertIn("gdigrab", win)
        self.assertIn("title=VN Japonske - VOYO", win)
        x11 = ffmpeg_cmd("ffmpeg", {"window_id": "0x3a00007", "display": ":0"}, self.d, "run1")
        self.assertEqual(x11[x11.index("-window_id") + 1], str(0x3a00007))
        self.assertIn("segment", win)
        with self.assertRaises(ValueError):
            ffmpeg_cmd("ffmpeg", {}, self.d, "run1")

    def test_capture_client_follows_the_server(self):
        cap = VoyoWindowCapture("http://127.0.0.1:1", "", {}, self.d / "spool", lambda: None, log=lambda m: None)
        self.assertIsNone(cap.wanted())
        cap.on_reply({"instance": "abc", "capture": False})
        self.assertIsNone(cap.wanted())                                      # server says off: nothing runs
        cap.on_reply({"instance": "abc", "capture": True})
        self.assertEqual(cap.wanted()["instance"], "abc")

    @unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("Xvfb") and sys.platform.startswith("linux"),
                         "needs ffmpeg + Xvfb")
    def test_real_capture_segments_upload(self):
        """ffmpeg x11grab of an Xvfb screen -> finished segment -> PUT to a stub server."""
        import http.server
        import threading
        got = []

        class H(http.server.BaseHTTPRequestHandler):
            def do_PUT(self):
                n = int(self.headers["Content-Length"])
                got.append((self.path, len(self.rfile.read(n)), self.headers.get("X-Capture-Start")))
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b'{"ok":true}')

            def log_message(self, *a):
                pass
        srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        xvfb = subprocess.Popen(["Xvfb", ":97", "-screen", "0", "320x240x24"], stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL)
        try:
            time.sleep(1)
            cap = VoyoWindowCapture(f"http://127.0.0.1:{srv.server_port}", "", {},
                                    self.d / "spool", lambda: {"window_id": "0", "display": ":97"},
                                    log=lambda m: None)
            cap.on_reply({"instance": "abcd1234", "capture": True, "segment_seconds": 1, "fps": 25})
            cap.step()
            self.assertIsNotNone(cap.proc)
            time.sleep(4)                       # > the encoder's lookahead: segments exist
            cap.on_reply(None)                                               # server stops asking
            cap.step()
            self.assertIsNone(cap.proc)
            self.assertTrue(got)
            self.assertTrue(all(p.startswith("/api/voyo/recordings/abcd1234/capture/run") for p, _, _ in got))
            self.assertTrue(all(n > 0 and t0 for _, n, t0 in got))
            self.assertFalse(list((self.d / "spool").glob("*/*.mp4")))      # uploaded = removed locally
        finally:
            xvfb.terminate()
            srv.shutdown()


if __name__ == "__main__":
    unittest.main()
