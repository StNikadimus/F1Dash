"""The screen recorder under the session recorder (tools/voyo_capture.py), the one-player lock and a dead
virtual screen (tools/voyo_server_player.py):

* supervised: it keeps recording while the dashboard server is away (that used to stop it 15 s after the
  server's last answer - a restart / update of the dashboard cut every recording), and stops only when the
  server answers that it takes no video (disk full) or the session recorder says so;
* its health (running, file growing, uploads), a restart of a hung ffmpeg, ffmpeg's errors in a FILE (a pipe
  nobody reads blocks ffmpeg after 64 KB), a full spool disk;
* a finished segment's check: picture, sound track, silence (real ffmpeg);
* only one server VOYO player at a time; a dead X server's socket is not taken for a screen.

Run (from main/):  python -m unittest tests.test_voyo_capture_supervised
"""
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools import voyo_capture as vc  # noqa: E402

KEY = "sprint-63661233-20260321"


class WantedTest(unittest.TestCase):
    def cap(self, override=None):
        return vc.VoyoWindowCapture("http://127.0.0.1:1", "", {"ffmpeg": "ffmpeg"}, Path(tempfile.mkdtemp()),
                                    lambda: {"window_id": "0"}, log=lambda s: None, override=override)

    def test_pc_capture_follows_the_server_only(self):
        c = self.cap()
        self.assertIsNone(c.wanted())
        c.on_reply({"instance": "abc12345", "capture": True})
        self.assertEqual(c.wanted()["instance"], "abc12345")
        c._reply = (c._reply[0], time.monotonic() - 20)              # no answer for 20 s: stops (as before)
        self.assertIsNone(c.wanted())

    def test_supervised_keeps_recording_without_the_server(self):
        state = {"on": True}
        c = self.cap(override=lambda: {"instance": KEY, "segment_seconds": 60} if state["on"] else None)
        self.assertEqual(c.wanted()["instance"], KEY)                 # no server answer at all yet
        c.on_reply({"instance": KEY, "capture": True, "blocked": None, "fps": 25})
        self.assertEqual((c.wanted()["instance"], c.wanted()["fps"]), (KEY, 25))
        c._reply = (c._reply[0], time.monotonic() - 300)             # the server away for 5 min
        self.assertEqual(c.wanted()["instance"], KEY)
        c.on_reply({"instance": KEY, "capture": False, "blocked": "disk full (min_free_bytes)"})
        self.assertIsNone(c.wanted())                                 # a fresh "no" stops it
        c.on_reply({"instance": KEY, "capture": True, "blocked": None})
        self.assertIsNotNone(c.wanted())
        state["on"] = False
        self.assertIsNone(c.wanted())                                 # the session recorder ended it
        self.assertEqual(c.retry_s, 5.0)

    def test_full_spool_disk_stops_the_recording(self):
        c = self.cap(override=lambda: {"instance": KEY})
        c.spool_min_free = 10 ** 18
        with mock.patch.object(c, "_start_ffmpeg") as start:
            c.step()
        start.assert_not_called()
        self.assertEqual(c.last_error, "spool disk full")


@unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "ffmpeg / ffprobe not installed")
class RealFfmpegTest(unittest.TestCase):
    def setUp(self):
        self.d = Path(tempfile.mkdtemp())

    def tearDown(self):
        shutil.rmtree(self.d, ignore_errors=True)

    def make(self, name, audio="sine=frequency=440", seconds=3, frag=False):
        cmd = ["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", f"testsrc=size=160x90:rate=10:duration={seconds}"]
        if audio:
            cmd += ["-f", "lavfi", "-i", f"{audio}:duration={seconds}" if audio.startswith("sine") else f"{audio}:d={seconds}",
                    "-c:a", "aac"]
        cmd += ["-c:v", "libx264", "-pix_fmt", "yuv420p"]
        if frag:                                   # like the capture: fragmented MP4, a keyframe every second
            cmd += ["-g", "10", "-movflags", "+frag_keyframe+empty_moov+default_base_moof"]
        cmd += [str(self.d / name)]
        subprocess.run(cmd, check=True)
        return self.d / name

    def test_segment_check(self):
        ok = vc.check_segment(self.make("ok.mp4"), "ffmpeg")
        self.assertEqual((ok["video"], ok["audio"], ok["silent"]), (True, True, False))
        self.assertAlmostEqual(ok["duration"], 3, delta=0.3)
        mute = vc.check_segment(self.make("mute.mp4", audio="anullsrc=r=48000:cl=stereo"), "ffmpeg")
        self.assertEqual((mute["audio"], mute["silent"]), (True, True))
        nosound = vc.check_segment(self.make("nosound.mp4", audio=None), "ffmpeg")
        self.assertEqual((nosound["video"], nosound["audio"], nosound["silent"]), (True, False, None))
        bad = self.d / "bad.mp4"
        bad.write_bytes(b"\0" * 100)
        self.assertEqual(vc.check_segment(bad, "ffmpeg")["duration"], None)

    def test_the_unfinished_segment_of_a_killed_recorder_is_kept(self):
        spool = self.d / "spool"
        d = spool / "abc12345"
        d.mkdir(parents=True)
        run = "run20261010-124539"
        t0 = vc.run_epoch(run)
        shutil.copy(self.make("a.mp4", seconds=4, frag=True), d / f"{run}_seg_00000.mp4")
        (d / f"{run}_list.csv").write_text(f"{run}_seg_00000.mp4,0.000000,4.000000\n")
        # the first one was uploaded already (and deleted): its end in the list still places the next one
        uploaded = self.d / "uploaded.mp4"
        full = self.make("b.mp4", seconds=6, frag=True).read_bytes()
        (d / f"{run}_seg_00001.mp4").write_bytes(full[: int(len(full) * 0.8)])        # ffmpeg killed mid-segment
        (d / "run20261010-130000_seg_00000.mp4").write_bytes(b"")                          # killed at once: empty
        c = vc.VoyoWindowCapture("http://127.0.0.1:1", "", {"ffmpeg": "ffmpeg"}, spool, lambda: None, log=lambda s: None)
        got = {p.name: (round(a - t0, 1), round(b - t0, 1)) for p, a, b in c._finished(d)}
        self.assertEqual(got[f"{run}_seg_00000.mp4"], (0.0, 4.0))
        shutil.move(d / f"{run}_seg_00000.mp4", uploaded)
        got2 = {p.name: (round(a - t0, 1), round(b - t0, 1)) for p, a, b in c._finished(d)}
        self.assertEqual(list(got2), [f"{run}_seg_00001.mp4"])
        self.assertEqual(got2[f"{run}_seg_00001.mp4"][0], 4.0)
        a, b = got[f"{run}_seg_00001.mp4"]
        self.assertEqual(a, 4.0)                                       # right after the last finished one
        self.assertGreater(b - a, 1.0)                                 # its measured length
        self.assertFalse((d / "run20261010-130000_seg_00000.mp4").exists())

    def test_health_restart_and_errors_in_a_file(self):
        c = vc.VoyoWindowCapture("http://127.0.0.1:1", "", {"ffmpeg": "ffmpeg"}, self.d / "spool",
                                 lambda: {"window_id": "0"}, log=lambda s: None, override=lambda: {"instance": KEY})
        c.spool_min_free = 0
        (self.d / "spool" / KEY).mkdir(parents=True)

        def fake_cmd(*a, **k):                    # a "recorder" that writes a growing segment and errors to stderr
            out, run = a[2], a[3]
            return ["sh", "-c", f"i=0; while true; do echo x >> {out}/{run}_seg_00000.mp4; "
                                f"echo 'some ffmpeg error line' >&2; i=$((i+1)); sleep 0.2; done"]
        with mock.patch.object(vc, "ffmpeg_cmd", fake_cmd), mock.patch.object(c, "upload_pending"):
            c.ffmpeg = "ffmpeg"
            c.step()
            self.assertTrue(c.health()["alive"])
            time.sleep(1.2)
            c.step()
            h = c.health()
            self.assertTrue(h["alive"])
            self.assertLess(h["grew_age_s"], 1.5)
            self.assertGreater(h["file_bytes"], 0)
            self.assertTrue(c.err_log.exists())
            self.assertIn("some ffmpeg error line", c.err_log.read_text())
            pid = c.proc.pid
            c.restart("test")
            self.assertIsNone(c.proc)
            c.step()                                                  # restarted at once, a new run
            self.assertNotEqual(c.proc.pid, pid)
            c._stop_ffmpeg()


class LockAndDisplayTest(unittest.TestCase):
    def test_only_one_player(self):
        from tools.voyo_server_player import Lock
        path = Path(tempfile.mkdtemp()) / "player.lock"
        with Lock(path):
            with self.assertRaises(SystemExit) as cm:
                with Lock(path):
                    pass
            self.assertIn("another server VOYO player is running", str(cm.exception))
        with Lock(path):                                              # free again afterwards
            pass

    def test_dead_x_server_socket_is_not_a_screen(self):
        from tools.voyo_server_player import display_alive
        d = Path(tempfile.mkdtemp())
        sock = d / "X93"
        srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        srv.bind(str(sock))
        srv.listen(1)
        self.assertTrue(display_alive(sock))
        srv.close()                                                    # the server died, its socket file stays
        self.assertTrue(sock.exists())
        self.assertFalse(display_alive(sock))
        self.assertFalse(display_alive(d / "X94"))

    def test_start_display_replaces_a_dead_screen(self):
        from tools import voyo_server_player as vsp
        p = vsp.Player.__new__(vsp.Player)
        p.display, p.sp = ":93", {}
        p.xvfb = mock.Mock()
        p.xvfb.alive.return_value = False
        lines = []
        removed = []
        with mock.patch.object(vsp, "display_alive", return_value=False), mock.patch.object(vsp, "log", lines.append), \
                mock.patch.object(Path, "exists", lambda self: str(self).endswith("X93")), \
                mock.patch.object(Path, "unlink", lambda self: removed.append(str(self))), \
                mock.patch.object(vsp.shutil, "which", return_value="/usr/bin/Xvfb"), \
                mock.patch.object(vsp.time, "sleep"):
            p.start_display()
        self.assertIn("/tmp/.X11-unix/X93", removed)
        self.assertIn("/tmp/.X93-lock", removed)
        p.xvfb.start.assert_called_once()
        self.assertTrue(any("dead one's socket" in ln for ln in lines))


if __name__ == "__main__":
    unittest.main()
