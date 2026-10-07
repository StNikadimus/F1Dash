"""/tv (live stream + dashboard), the VOYO account on /disk, HTTPS settings.

Run (from main/):  python -m unittest tests.test_tv_live
"""
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from server.config import PROJECT_ROOT, REPO_ROOT, load_config  # noqa: E402
from server.voyo_account import VoyoAccount, mask_email  # noqa: E402
from tools.voyo_capture import VoyoWindowCapture, ffmpeg_cmd  # noqa: E402


class AccountTest(unittest.TestCase):
    def setUp(self):
        self.d = Path(tempfile.mkdtemp())
        self.acc = VoyoAccount(self.d / "auth" / "voyo_credentials.json", self.d / "voyo_server_player.json",
                               "https://voyo.si/from-config")

    def tearDown(self):
        shutil.rmtree(self.d, ignore_errors=True)

    def test_saved_private_and_never_returned(self):
        self.acc.save("rok.test@gmail.com", "Skriv1!")
        f = self.d / "auth" / "voyo_credentials.json"
        self.assertEqual(stat.S_IMODE(f.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(f.parent.stat().st_mode), 0o700)
        pub = json.dumps(self.acc.public())
        self.assertNotIn("Skriv1!", pub)
        self.assertNotIn("rok.test", pub)
        self.assertEqual(self.acc.public()["email"], "r***@gmail.com")
        self.acc.save("other@gmail.com", None)                              # empty password keeps the saved one
        self.assertEqual(self.acc.credentials()["password"], "Skriv1!")
        self.acc.forget()
        self.assertFalse(self.acc.public()["password_set"])

    def test_validation_and_stream_page(self):
        with self.assertRaises(ValueError):
            self.acc.save("not-an-email", None)
        with self.assertRaises(ValueError):
            self.acc.save(None, None, "javascript:alert(1)")
        self.assertEqual(self.acc.stream_url(), ("https://voyo.si/from-config", "[voyo.server_player] stream_url"))
        self.acc.save(None, None, "https://voyo.si/live/sport")
        self.assertEqual(self.acc.stream_url()[1], "set on the /disk page")
        self.acc.save(None, None, "https://example.com/x")
        self.assertEqual(self.acc.public()["stream_url_warning"], "not a voyo.si page")
        self.assertEqual(mask_email("a@b.c"), "a***@b.c")

    def test_commands_for_the_player(self):
        self.acc.commands.append("login")
        self.assertEqual(self.acc.take_commands(), ["login"])
        self.assertEqual(self.acc.take_commands(), [])


class LiveCaptureTest(unittest.TestCase):
    def setUp(self):
        self.d = Path(tempfile.mkdtemp())

    def tearDown(self):
        shutil.rmtree(self.d, ignore_errors=True)

    def test_tee_command(self):
        cmd = ffmpeg_cmd("ffmpeg", {"window_id": "0", "display": ":90"}, self.d, "run1", 30, 23, 60,
                         audio={"format": "pulse", "device": "f1voyo.monitor"}, live_dir=self.d / "live")
        self.assertEqual(cmd[cmd.index("-f", cmd.index("-flags")) + 1], "tee")
        out = cmd[-1]
        rec, live = out.split("|")
        self.assertIn("f=segment:segment_time=60", rec)
        self.assertIn("program_date_time", live)
        self.assertTrue(live.endswith(str(self.d / "live" / "index.m3u8")))
        self.assertIn("expr:gte(t,n_forced*2)", cmd)                         # keyframe every 2 s (HLS pieces)
        self.assertEqual(cmd.count("-map"), 2)                                # video + sound
        plain = ffmpeg_cmd("ffmpeg", {"window_id": "0"}, self.d, "run1")
        self.assertNotIn("tee", plain)

    @unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("Xvfb") and sys.platform.startswith("linux"),
                         "needs ffmpeg + Xvfb")
    def test_real_recording_and_live_stream(self):
        xvfb = subprocess.Popen(["Xvfb", ":95", "-screen", "0", "320x240x24"], stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL)
        try:
            time.sleep(1)
            live = self.d / "live"
            live.mkdir()
            (live / "live_00001.ts").write_bytes(b"old")                      # a previous run's leftovers
            cap = VoyoWindowCapture("http://127.0.0.1:1", "", {}, self.d / "spool",
                                    lambda: {"window_id": "0", "display": ":95"}, log=lambda m: None, live_dir=live)
            cap.on_reply({"instance": "live1234", "capture": True, "segment_seconds": 4, "fps": 25})
            cap.step()
            for _ in range(80):
                if (live / "index.m3u8").exists() and "#EXTINF" in (live / "index.m3u8").read_text():
                    break
                time.sleep(0.1)
            text = (live / "index.m3u8").read_text()
            self.assertIn("#EXT-X-PROGRAM-DATE-TIME", text)
            self.assertFalse((live / "live_00001.ts").exists() and (live / "live_00001.ts").read_bytes() == b"old")
            time.sleep(5)
            cap.on_reply(None)
            cap.step()                                                        # stopped: off air
            self.assertFalse((live / "index.m3u8").exists())
            segs = list((self.d / "spool").glob("*/*.mp4"))                   # the recording still made (upload failed)
            self.assertTrue(segs)
        finally:
            xvfb.terminate()


class HttpTest(unittest.TestCase):
    def test_tv_and_account_api(self):
        from starlette.testclient import TestClient
        import server.app as appmod
        d = Path(tempfile.mkdtemp())
        try:
            env = {"F1DASH_VOYO_SERVER_PLAYER_ENABLED": "true", "F1DASH_VOYO_RECORDING_PATH": str(d / "rec"),
                   "F1DASH_VOYO_RECORDING_REQUIRE_MOUNT": "", "F1DASH_VOYO_RECORDING_MOUNT_MARKER": "",
                   "F1DASH_VOYO_RECORDING_MIN_FREE_BYTES": "0", "F1DASH_REMOTE_TOKEN": "tok"}
            with mock.patch.dict(os.environ, env), \
                    mock.patch.object(appmod, "LOOPBACK", appmod.LOOPBACK | {"testclient"}), \
                    mock.patch.object(appmod, "DATA_DIR", d):
                cfg = load_config()
                cfg["source"]["mode"] = "test"
                c = TestClient(appmod.create_app(cfg))
                H = {"X-Remote-Token": "tok"}
                self.assertEqual(c.get("/tv").status_code, 200)
                self.assertEqual(c.get("/tv-static/vendor/hls.light.min.js").status_code, 200)
                st = c.get("/api/tv/status").json()
                self.assertFalse(st["live"]["on_air"])
                self.assertEqual(c.get("/tv/live/index.m3u8").status_code, 401)
                self.assertEqual(c.get("/tv/live/index.m3u8", headers=H).status_code, 404)    # off air
                self.assertEqual(c.get("/tv/live/..%2Fauth%2Fx", headers=H).status_code, 404)
                live = d / "live"
                live.mkdir()
                pdt = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(time.time() - 3)) + ".000+0000"
                (live / "index.m3u8").write_text("#EXTM3U\n#EXTINF:2.0,\n#EXT-X-PROGRAM-DATE-TIME:" + pdt +
                                                 "\nlive_00001.ts\n")
                (live / "live_00001.ts").write_bytes(b"\x47" * 188)
                r = c.get("/tv/live/index.m3u8", headers=H)
                self.assertEqual(r.headers["content-type"].split(";")[0], "application/vnd.apple.mpegurl")
                self.assertEqual(c.get("/tv/live/live_00001.ts?token=tok").status_code, 200)
                lv = c.get("/api/tv/status").json()["live"]
                self.assertTrue(lv["on_air"])
                self.assertEqual(lv["viewers"], 1)
                self.assertAlmostEqual(lv["lag_s"], 3, delta=2)
                # the VOYO account on /disk
                self.assertEqual(c.post("/api/disk/voyo", content='{"email":"a@b.si"}').status_code, 401)
                r = c.post("/api/disk/voyo", headers=H,
                           content=json.dumps({"email": "a@b.si", "password": "pw", "stream_url": "https://voyo.si/x"}))
                self.assertEqual(r.json()["changed"], ["e-mail", "password", "stream page"])
                self.assertNotIn('"pw"', c.get("/api/disk/voyo").text)
                self.assertEqual(c.post("/api/disk/voyo/login", headers=H).status_code, 409)  # player not running
                c.post("/api/voyo/player/status", content=json.dumps({"state": "idle"}))
                self.assertEqual(c.post("/api/disk/voyo/login", headers=H).status_code, 200)
                ans = c.post("/api/voyo/player/status", content=json.dumps(
                    {"state": "idle", "login": {"ok": True, "result": "signed in", "at": time.time()}})).json()
                self.assertEqual(ans["commands"], ["login"])                    # the player picks it up once
                self.assertEqual(c.get("/api/disk/voyo").json()["last_login"]["result"], "signed in")
                self.assertTrue(c.get("/api/disk/status").json()["live"]["on_air"])
        finally:
            shutil.rmtree(d, ignore_errors=True)

    def test_files_and_settings(self):
        for f in ("index.html", "tv.css", "tv.js", "vendor/hls.light.min.js", "vendor/hls.js-LICENSE"):
            self.assertTrue((REPO_ROOT / "server" / "tv" / f).is_file(), f)
        app_js = (PROJECT_ROOT / "dashboard" / "app.js").read_text()
        self.assertIn("FORCED_TV", app_js)                                    # ?layout= for the /tv iframe
        self.assertNotIn("S.ui.tv_mode_effective ||", app_js)
        cfg = load_config()
        self.assertEqual(cfg["server"]["https_port"], 0)                      # PC default: http only
        over = load_config(overlays=[str(REPO_ROOT / "server" / "config" / "server.toml")])
        self.assertEqual(over["server"]["https_port"], 443)
        self.assertTrue(over["voyo"]["server_player"]["live_stream"])
        unit = (REPO_ROOT / "server" / "systemd" / "f1-dashboard.service").read_text()
        self.assertIn("AmbientCapabilities=CAP_NET_BIND_SERVICE", unit)


if __name__ == "__main__":
    unittest.main()
