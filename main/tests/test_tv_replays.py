"""VOYO -> disk -> /tv player: REPLAYS (server/replays.py + the /tv endpoints), the remote's PLAYER layer
(server/remote.py), the F1 schedule retry of the server player and the recording disk running full.

No network, no browser: synthetic fragmented-MP4 files in a temp recordings folder, the real app.

Run (from main/):  python -m unittest tests.test_tv_replays
"""
import asyncio
import json
import os
import shutil
import struct
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from server import replays  # noqa: E402


def box(typ: bytes, payload: bytes = b"") -> bytes:
    return struct.pack(">I4s", 8 + len(payload), typ) + payload


def fmp4(fragments: int = 2, moov_payload: bytes = b"\0" * 40) -> bytes:
    """ftyp + moov + (moof + mdat) x n - the layout ffmpeg's capture writes (empty_moov + frag_keyframe)."""
    out = box(b"ftyp", b"isom\0\0\x02\0isomiso6") + box(b"moov", moov_payload)
    for i in range(fragments):
        out += box(b"moof", b"\1" * 24) + box(b"mdat", bytes([i]) * 200)
    return out


def init_len() -> int:
    return len(box(b"ftyp", b"isom\0\0\x02\0isomiso6") + box(b"moov", b"\0" * 40))


def make_pkg(rec: Path, iid: str, status="closed", meeting="Singapore Grand Prix", session="Race", kind="race",
             start="2026-10-11T12:00:05Z", segs=3, seconds=10.0, channel="server_player", deleted=False, log=True):
    d = rec / iid
    (d / "capture").mkdir(parents=True)
    names, total, t0 = [], 0, 1791727200.0
    for i in range(segs):
        name = f"run20261011-140000_seg_{i:05d}.mp4"
        data = fmp4()
        (d / "capture" / name).write_bytes(data)
        names.append(name)
        total += len(data)
        if log:
            with open(d / "capture" / "capture.jsonl", "a") as fh:
                fh.write(json.dumps({"name": name, "bytes": len(data), "pc_start_epoch": t0 + i * seconds,
                                     "pc_end_epoch": t0 + (i + 1) * seconds}) + "\n")
    m = {"schema": 1, "stream_instance_id": iid, "status": status, "channel": channel, "detected_at": start,
         "stream_start_wall_time": start, "title": "VN Singapurja - VOYO", "live": True,
         "session": {"meeting": meeting, "session_name": session, "kind": kind},
         "position": {"min": 0, "max": 30}, "counts": {"timeline": 1, "observations": 0, "anchors": 0, "pairs": 0, "gaps": 0},
         "capture": {"enabled": True, "segments": names, "bytes": total,
                     "deleted_at": "2026-10-12T00:00:00Z" if deleted else None},
         "closed_at": start if status == "closed" else None}
    (d / "manifest.json").write_text(json.dumps(m))
    return d


class Mp4AndPlaylistTest(unittest.TestCase):
    def setUp(self):
        self.d = Path(tempfile.mkdtemp())

    def tearDown(self):
        shutil.rmtree(self.d, ignore_errors=True)

    def write(self, name, data):
        p = self.d / name
        p.write_bytes(data)
        return p

    def test_init_size_of_fragmented_and_other_files(self):
        self.assertEqual(replays.init_size(self.write("a.mp4", fmp4())), init_len())
        plain = box(b"ftyp", b"isom") + box(b"moov", b"\0" * 8) + box(b"mdat", b"x" * 50)
        self.assertIsNone(replays.init_size(self.write("plain.mp4", plain)))            # not fragmented
        self.assertIsNone(replays.init_size(self.write("trunc.mp4", fmp4()[:30])))       # still being written
        self.assertIsNone(replays.init_size(self.write("junk.mp4", b"\xff" * 100)))
        self.assertIsNone(replays.init_size(self.write("nomoov.mp4", box(b"ftyp") + box(b"moof") + box(b"mdat"))))
        self.assertIsNone(replays.init_size(self.d / "missing.mp4"))
        big = box(b"ftyp", b"isom") + struct.pack(">I4sQ", 1, b"moov", 16 + 10) + b"\0" * 10 + box(b"moof") + box(b"mdat")
        self.assertEqual(replays.init_size(self.write("big.mp4", big)), len(box(b"ftyp", b"isom")) + 26)   # 64-bit box size

    def test_segments_order_durations_and_filtering(self):
        pkg = make_pkg(self.d, "aaaa11112222", segs=3, seconds=12.5)
        cap = pkg / "capture"
        (cap / "run20261011-140000_seg_00003.mp4.part").write_bytes(fmp4())          # an unfinished upload
        (cap / "notes.txt").write_text("x")
        (cap / "run20261011-140000_seg_00004.mp4").write_bytes(b"\0" * 10)           # not an fMP4
        (cap / "bad name.mp4").write_bytes(fmp4())
        segs = replays.segments(pkg)
        self.assertEqual([s["name"] for s in segs], [f"run20261011-140000_seg_{i:05d}.mp4" for i in range(3)])
        self.assertEqual([s["duration"] for s in segs], [12.5] * 3)
        self.assertEqual(segs[0]["init"], init_len())
        (cap / "capture.jsonl").unlink()                                              # no log: the configured length
        self.assertEqual([s["duration"] for s in replays.segments(pkg, default_s=60)], [60.0] * 3)
        self.assertEqual(replays.segments(self.d / "nothing"), [])

    def test_playlist(self):
        segs = replays.segments(make_pkg(self.d, "aaaa11112222", segs=2, seconds=10.0))
        vod = replays.playlist(segs, complete=True)
        n = init_len()
        size = len(fmp4())
        self.assertTrue(vod.startswith("#EXTM3U\n#EXT-X-VERSION:7\n#EXT-X-TARGETDURATION:10\n"))
        self.assertIn("#EXT-X-PLAYLIST-TYPE:VOD", vod)
        self.assertTrue(vod.rstrip().endswith("#EXT-X-ENDLIST"))
        self.assertEqual(vod.count("#EXT-X-DISCONTINUITY"), 1)                        # between the 2 segments
        self.assertIn(f'#EXT-X-MAP:URI="run20261011-140000_seg_00000.mp4",BYTERANGE="{n}@0"', vod)
        self.assertIn(f"#EXT-X-BYTERANGE:{size - n}@{n}", vod)
        ev = replays.playlist(segs, complete=False, query="?p=abc")
        self.assertIn("#EXT-X-PLAYLIST-TYPE:EVENT", ev)
        self.assertNotIn("#EXT-X-ENDLIST", ev)                                         # still recording
        self.assertIn('URI="run20261011-140000_seg_00001.mp4?p=abc"', ev)
        self.assertIn("\nrun20261011-140000_seg_00001.mp4?p=abc\n", ev)

    def test_listing(self):
        make_pkg(self.d, "aaaa11112222", start="2026-10-11T12:00:05Z")
        make_pkg(self.d, "bbbb33334444", session="Qualifying", kind="qualifying", status="interrupted")
        make_pkg(self.d, "cccc55556666", session="Practice 1", kind="practice1", segs=0)             # no video
        make_pkg(self.d, "dddd77778888", deleted=True)                                               # video deleted
        summaries = [{"stream_instance_id": i, "capture_segments": 0 if i.startswith("c") else 3,
                      "capture_deleted_at": "x" if i.startswith("d") else None, "meeting": "Singapore Grand Prix",
                      "session_name": s, "session_kind": k, "status": st, "detected_at": "2026-10-11T12:00:05Z",
                      "channel": "server_player"}
                     for i, s, k, st in (("aaaa11112222", "Race", "race", "closed"),
                                         ("bbbb33334444", "Qualifying", "qualifying", "interrupted"),
                                         ("cccc55556666", "Practice 1", "practice1", "closed"),
                                         ("dddd77778888", "Race", "race", "closed"),
                                         ("../etc", "Race", "race", "closed"))]
        items = replays.listing(summaries, lambda i: self.d / i if (self.d / i / "manifest.json").exists() else None)
        self.assertEqual([x["id"] for x in items], ["aaaa11112222", "bbbb33334444"])
        race = items[0]
        self.assertEqual((race["meeting"], race["session_name"], race["session_kind"], race["status"], race["duration_s"],
                          race["segments"]), ("Singapore Grand Prix", "Race", "race", "closed", 30.0, 3))
        self.assertEqual(items[1]["status"], "interrupted")


class DiscoveryTest(unittest.TestCase):
    """What counts as a playable recording: decided by the files on the disk, not by a folder or the index."""

    def setUp(self):
        self.d = Path(tempfile.mkdtemp())

    def tearDown(self):
        shutil.rmtree(self.d, ignore_errors=True)

    def summary(self, iid, **kw):
        return {"stream_instance_id": iid, "capture_segments": 3, "capture_deleted_at": None, "meeting": "Singapore Grand Prix",
                "session_name": "Race", "session_kind": "race", "status": "closed", "detected_at": "2026-10-11T12:00:05Z", **kw}

    def pkg_dir(self, iid):
        return self.d / iid if (self.d / iid / "manifest.json").exists() else None

    def test_empty_recordings_folder(self):
        hidden = {}
        self.assertEqual(replays.listing([], self.pkg_dir, hidden=hidden), [])
        self.assertEqual(hidden, {"no_video": 0, "unplayable": 0, "deleted": 0})

    def test_valid_invalid_and_incomplete_packages(self):
        make_pkg(self.d, "aaaa11112222")                                                     # valid
        make_pkg(self.d, "bbbb33334444", segs=0)                                             # capture/ but no video
        shutil.rmtree(make_pkg(self.d, "cccc55556666") / "capture")                         # no capture/ at all
        plain = make_pkg(self.d, "dddd77778888", segs=0)                                     # video, not fragmented
        (plain / "capture" / "run20261011-140000_seg_00000.mp4").write_bytes(
            box(b"ftyp", b"isom") + box(b"moov", b"\0" * 8) + box(b"mdat", b"x" * 50))
        mkv = make_pkg(self.d, "eeee99990000", segs=0)                                       # another format
        (mkv / "capture" / "run20261011-140000_seg_00000.mkv").write_bytes(b"\x1aE\xdf\xa3" + b"\0" * 64)
        part = make_pkg(self.d, "ffff11112222", segs=0)                                      # upload not finished
        (part / "capture" / "run20261011-140000_seg_00000.mp4.part").write_bytes(fmp4())
        make_pkg(self.d, "gggg33334444", deleted=True)                                       # video deleted (retention)
        (self.d / "hhhh55556666" / "capture").mkdir(parents=True)                           # a folder, no package
        (self.d / "hhhh55556666" / "capture" / "run20261011-140000_seg_00000.mp4").write_bytes(fmp4())
        idx0 = make_pkg(self.d, "iiii77778888")                                              # index says 0, disk has video
        ids = ["aaaa11112222", "bbbb33334444", "cccc55556666", "dddd77778888", "eeee99990000", "ffff11112222",
               "gggg33334444", "hhhh55556666", "iiii77778888"]
        summaries = [self.summary(i, capture_deleted_at="x" if i.startswith("g") else None,
                                  capture_segments=0 if i.startswith("i") else 3) for i in ids]
        hidden = {}
        items = replays.listing(summaries, self.pkg_dir, hidden=hidden)
        self.assertEqual([x["id"] for x in items], ["aaaa11112222", "iiii77778888"])
        self.assertEqual(hidden, {"no_video": 3, "unplayable": 2, "deleted": 1})
        self.assertTrue(idx0.exists())
        self.assertEqual(items[0]["skipped_segments"], 0)

    def test_a_segment_cut_short_plays_up_to_its_last_complete_fragment(self):
        pkg = make_pkg(self.d, "aaaa11112222", segs=2)
        last = pkg / "capture" / "run20261011-140000_seg_00001.mp4"
        whole = fmp4(fragments=2)
        cut = whole[:-50]                                                     # power loss inside the last mdat
        last.write_bytes(cut)
        one_fragment_end = init_len() + len(box(b"moof", b"\1" * 24) + box(b"mdat", b"\0" * 200))
        self.assertEqual(replays.layout(last), (init_len(), one_fragment_end))
        segs = replays.segments(pkg)
        self.assertEqual(segs[1]["end"], one_fragment_end)
        self.assertIn(f"#EXT-X-BYTERANGE:{one_fragment_end - init_len()}@{init_len()}", replays.playlist(segs, True))
        last.write_bytes(whole[:init_len() + 20])                             # not one complete fragment: left out
        st = {}
        self.assertEqual([s["name"] for s in replays.segments(pkg, stats=st)], ["run20261011-140000_seg_00000.mp4"])
        self.assertEqual(st, {"files": 2, "bad": 1})
        hidden = {}
        items = replays.listing([self.summary("aaaa11112222")], self.pkg_dir, hidden=hidden)
        self.assertEqual((items[0]["segments"], items[0]["skipped_segments"]), (1, 1))

    def test_trailing_index_box_is_not_played(self):
        pkg = make_pkg(self.d, "aaaa11112222", segs=1)
        f = pkg / "capture" / "run20261011-140000_seg_00000.mp4"
        f.write_bytes(fmp4() + box(b"mfra", b"\0" * 16))                    # ffmpeg ends a segment with mfra
        self.assertEqual(replays.layout(f), (init_len(), len(fmp4())))

    def test_check_command_reads_only(self):
        import contextlib
        import io
        make_pkg(self.d, "aaaa11112222")
        make_pkg(self.d, "bbbb33334444", segs=0)
        before = sorted((p, p.stat().st_mtime_ns) for p in self.d.rglob("*"))
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(replays.main([str(self.d)]), 0)
        text = out.getvalue()
        self.assertIn("aaaa11112222", text)
        self.assertIn("PLAYABLE", text)
        self.assertIn("no video files", text)
        self.assertIn("1 playable recording(s)", text)
        self.assertEqual(sorted((p, p.stat().st_mtime_ns) for p in self.d.rglob("*")), before)    # nothing written

    @unittest.skipUnless(shutil.which("ffmpeg"), "ffmpeg not installed")
    def test_real_ffmpeg_capture_segments(self):
        """Segments written exactly like tools/voyo_capture.py (segment muxer, fragmented MP4, H.264 + AAC) are
        listed, and the playlist built over them decodes from start to end."""
        import subprocess
        enc = subprocess.run(["ffmpeg", "-hide_banner", "-encoders"], capture_output=True, text=True).stdout
        if "libx264" not in enc:
            self.skipTest("ffmpeg without libx264")
        pkg = make_pkg(self.d, "aaaa11112222", segs=0)
        cap = pkg / "capture"
        r = subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "testsrc=size=320x180:rate=15", "-f", "lavfi",
                            "-i", "sine=frequency=440", "-t", "6", "-c:v", "libx264", "-preset", "veryfast",
                            "-pix_fmt", "yuv420p", "-force_key_frames", "expr:gte(t,n_forced*2)", "-c:a", "aac",
                            "-f", "segment", "-segment_time", "2", "-reset_timestamps", "1", "-segment_format", "mp4",
                            "-segment_format_options", "movflags=+frag_keyframe+empty_moov+default_base_moof",
                            str(cap / "run20261011-140000_seg_%05d.mp4")], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        segs = replays.segments(pkg, default_s=2.0)
        self.assertGreaterEqual(len(segs), 3)
        items = replays.listing([self.summary("aaaa11112222")], self.pkg_dir)
        self.assertEqual(items[0]["segments"], len(segs))
        (cap / "index.m3u8").write_text(replays.playlist(segs, True))
        r = subprocess.run(["ffmpeg", "-v", "error", "-allowed_extensions", "ALL", "-i", str(cap / "index.m3u8"),
                            "-f", "null", "-"], capture_output=True, text=True)
        self.assertEqual((r.returncode, r.stderr.strip()), (0, ""))


class TvReplayEndpointTest(unittest.TestCase):
    """The /tv endpoints on the real app: an approved /tv page only, listed segments only, ranges."""

    def setUp(self):
        self.d = Path(tempfile.mkdtemp())
        rec = self.d / "rec"
        self.pkg = make_pkg(rec, "aaaa11112222")
        make_pkg(rec, "bbbb33334444", session="Qualifying", kind="qualifying")
        (rec / "outside.mp4").write_bytes(fmp4())
        os.symlink(rec / "outside.mp4", self.pkg / "capture" / "run20261011-140000_seg_00009.mp4")    # escapes capture/
        from test_security import Server
        self.s = Server(self.d)
        admin = self.s.browser()
        csrf = self.s.setup_password(admin)
        phone = self.s.browser()
        did, _ = self.s.remote_device(phone)
        self.assertEqual(admin.post("/api/disk/security/trust", json={"device": did},
                                    headers={"X-F1-CSRF": csrf}).status_code, 200)
        self.admin, self.did = admin, did
        self.tv = self.s.browser()
        self.assertEqual(self.tv.get("/tv").status_code, 200)
        b = self.tv.post("/api/tv/auth/request").json()
        req = next(x for x in self.s.sec.requests.values() if x["code"] == b["code"])
        self.s.sec.decide(req["id"], True, by_device=did)
        st = self.tv.post("/api/tv/auth/status", headers={"X-F1-TV-Challenge": b["challenge"]}).json()
        self.page = st["page"]
        self.H = {"X-F1-TV-Page": self.page}

    def tearDown(self):
        self.s.close()
        shutil.rmtree(self.d, ignore_errors=True)

    def test_list_playlist_and_segments(self):
        r = self.tv.get("/api/tv/replays", headers=self.H)
        self.assertEqual(r.status_code, 200, r.text)
        items = r.json()["recordings"]
        self.assertEqual({x["id"] for x in items}, {"aaaa11112222", "bbbb33334444"})
        r = self.tv.get("/tv/replay/aaaa11112222/index.m3u8", headers=self.H)
        self.assertEqual(r.status_code, 200)
        self.assertIn("mpegurl", r.headers["content-type"])
        self.assertIn("#EXT-X-ENDLIST", r.text)
        self.assertNotIn("seg_00009", r.text)                                  # the symlink out of capture/ is not listed
        n, size = init_len(), len(fmp4())
        seg = "/tv/replay/aaaa11112222/run20261011-140000_seg_00001.mp4"
        r = self.tv.get(seg, headers={**self.H, "Range": f"bytes={n}-{size - 1}"})
        self.assertEqual(r.status_code, 206)
        self.assertEqual(r.content, fmp4()[n:])
        r = self.tv.get(seg, headers={**self.H, "Range": f"bytes=0-{n - 1}"})
        self.assertEqual((r.status_code, r.content), (206, fmp4()[:n]))
        self.assertEqual(self.tv.get(seg, headers=self.H).content, fmp4())

    def test_response_shape(self):
        b = self.tv.get("/api/tv/replays", headers=self.H).json()
        self.assertEqual(set(b), {"ok", "recordings", "hidden", "recorder"})
        self.assertEqual((b["ok"], b["recorder"]), (True, {"ok": True}))
        self.assertEqual(b["hidden"], {"no_video": 0, "unplayable": 0, "deleted": 0})
        x = b["recordings"][0]
        self.assertEqual(set(x), {"id", "meeting", "session_name", "session_kind", "title", "start", "status", "channel",
                                  "duration_s", "segments", "skipped_segments", "check", "sound"})
        self.assertNotIn(str(self.d), json.dumps(b))                             # no disk paths to the TV

    def test_missing_segments_and_a_recording_that_went_away(self):
        seg = "/tv/replay/aaaa11112222/run20261011-140000_seg_00002.mp4"
        (self.pkg / "capture" / "run20261011-140000_seg_00002.mp4").unlink()        # gone after it was listed
        self.assertEqual(self.tv.get(seg, headers=self.H).status_code, 404)
        self.assertNotIn("seg_00002", self.tv.get("/tv/replay/aaaa11112222/index.m3u8", headers=self.H).text)
        shutil.rmtree(self.pkg / "capture")                                             # all video gone
        self.assertEqual(self.tv.get("/tv/replay/aaaa11112222/index.m3u8", headers=self.H).status_code, 404)
        b = self.tv.get("/api/tv/replays", headers=self.H).json()
        self.assertEqual([x["id"] for x in b["recordings"]], ["bbbb33334444"])
        self.assertEqual(b["hidden"]["no_video"], 1)

    def test_live_stream_still_works_and_still_needs_the_page(self):
        live = self.d / "live"
        live.mkdir(exist_ok=True)
        (live / "index.m3u8").write_text("#EXTM3U\n#EXT-X-TARGETDURATION:2\n#EXTINF:2.0,\nlive_00001.ts\n")
        (live / "live_00001.ts").write_bytes(b"\x47" * 188)
        self.assertEqual(self.tv.get("/tv/live/index.m3u8", headers=self.H).status_code, 200)
        self.assertEqual(self.tv.get("/tv/live/live_00001.ts", headers=self.H).status_code, 200)
        self.assertEqual(self.tv.get("/tv/live/index.m3u8").status_code, 401)
        self.assertEqual(self.s.browser().get("/tv/live/live_00001.ts", headers=self.H).status_code, 401)

    def test_safari_page_secret_in_the_urls(self):
        r = self.tv.get("/tv/replay/aaaa11112222/index.m3u8", params={"p": self.page})
        self.assertEqual(r.status_code, 200)
        self.assertIn("seg_00000.mp4?p=", r.text)
        uri = next(ln for ln in r.text.splitlines() if ln and not ln.startswith("#"))
        self.assertEqual(self.tv.get("/tv/replay/aaaa11112222/" + uri).status_code, 200)

    def test_no_access_without_the_approved_page(self):
        anon = self.s.browser()
        for path in ("/api/tv/replays", "/tv/replay/aaaa11112222/index.m3u8",
                     "/tv/replay/aaaa11112222/run20261011-140000_seg_00000.mp4"):
            self.assertEqual(anon.get(path).status_code, 401, path)
            self.assertEqual(self.admin.get(path).status_code, 401, path)             # a /disk login is not a TV page
            self.assertEqual(self.tv.get(path).status_code, 401, path)                # cookie without the page secret
            self.assertEqual(anon.get(path, headers=self.H).status_code, 401, path)   # page secret without the cookie

    def test_only_listed_files_of_valid_packages(self):
        bad = ["/tv/replay/aaaa11112222/manifest.json", "/tv/replay/aaaa11112222/capture.jsonl",
               "/tv/replay/aaaa11112222/run20261011-140000_seg_00009.mp4",          # the symlink
               "/tv/replay/aaaa11112222/run20261011-140000_seg_00042.mp4",          # not there
               "/tv/replay/aaaa11112222/..%2Fmanifest.json", "/tv/replay/..%2F..%2Fetc/index.m3u8",
               "/tv/replay/zz/index.m3u8", "/tv/replay/aaaa11112222!/index.m3u8",
               "/tv/replay/ffffffffffff/index.m3u8"]
        for path in bad:
            r = self.tv.get(path, headers=self.H)
            self.assertIn(r.status_code, (404,), path)
            self.assertNotIn(b"ftyp", r.content[:64], path)

    def test_recording_in_progress_is_an_event_playlist(self):
        rec = self.s.app.state.runtime.stream_recorder
        rec.cur = {"stream_instance_id": "bbbb33334444"}
        try:
            r = self.tv.get("/tv/replay/bbbb33334444/index.m3u8", headers=self.H)
        finally:
            rec.cur = None
        self.assertIn("#EXT-X-PLAYLIST-TYPE:EVENT", r.text)
        self.assertNotIn("#EXT-X-ENDLIST", r.text)


class HealthDiskFullTest(unittest.TestCase):
    """DISK FULL during an open session: /api/health still says busy (server/update-f1dash.sh waits)."""

    def test_busy(self):
        import server.app as appmod
        from test_security import Server
        d = Path(tempfile.mkdtemp())
        s = Server(d)
        try:
            rec = s.app.state.runtime.stream_recorder
            c = s.browser()
            with mock.patch.object(appmod, "LOOPBACK", appmod.LOOPBACK | {"testclient"}), \
                    mock.patch.object(rec, "free_bytes", return_value=0), mock.patch.object(rec, "min_free", 1):
                self.assertEqual(c.get("/api/health").json()["recorder"], {"state": "DISK FULL", "busy": False})
                rec.cur = {"stream_instance_id": "aaaa11112222"}
                try:
                    self.assertEqual(c.get("/api/health").json()["recorder"], {"state": "DISK FULL", "busy": True})
                finally:
                    rec.cur = None
        finally:
            s.close()
            shutil.rmtree(d, ignore_errors=True)


class GatewayReplayTest(unittest.TestCase):
    """Through Tailscale Funnel: the replay routes are allowlisted for the approved /tv page only."""

    def test_public(self):
        import test_public_gateway as g
        d = Path(tempfile.mkdtemp())
        make_pkg(d / "rec", "aaaa11112222")
        try:
            t = g.GatewayTest(methodName="test_00_off_by_default_and_fail_closed")
            t.d, t.e = d, g.Env(d)
            try:
                t.admin = t.e.lan()
                code = (d / "auth" / "disk-setup-code").read_text().strip()
                t.admin.post("/api/disk/auth/setup", json={"code": code, "password": g.PW, "confirm": g.PW})
                t.H = {"X-F1-CSRF": t.admin.get("/api/disk/auth/state").json()["csrf"]}
                anon = t.e.internet()
                self.assertEqual(anon.get("/api/tv/replays").status_code, 401)
                self.assertEqual(anon.get("/tv/replay/aaaa11112222/index.m3u8").status_code, 401)
                tv, page, _ch, _req = t.approved_public_tv()
                r = tv.get("/api/tv/replays", headers=page)
                self.assertEqual(r.status_code, 200, r.text)
                self.assertEqual([x["id"] for x in r.json()["recordings"]], ["aaaa11112222"])
                r = tv.get("/tv/replay/aaaa11112222/index.m3u8", headers=page)
                self.assertEqual(r.status_code, 200)
                seg = next(ln for ln in r.text.splitlines() if ln and not ln.startswith("#"))
                r = tv.get("/tv/replay/aaaa11112222/" + seg, headers={**page, "Range": "bytes=0-7"})
                self.assertEqual((r.status_code, len(r.content)), (206, 8))
                self.assertEqual(tv.get("/tv/replay/aaaa11112222/index.m3u8", params={"x": "1"}, headers=page).status_code,
                                 400)                                      # an unknown query parameter: refused
                self.assertEqual(tv.get("/tv/replay/aaaa11112222/manifest.json", headers=page).status_code, 404)
                self.assertEqual(tv.post("/api/tv/replays", headers=page).status_code, 404)
                # why the /tv page opens its panel itself: its dashboard socket through Funnel only listens -
                # a key sent there (B = LIVE / REPLAYS) never reaches the remote, by design
                from server.remote import RemoteController
                calls = []
                orig_key, orig_cmd = RemoteController.handle_key, RemoteController.handle_command

                async def spy_key(self_, key, origin):
                    calls.append(key)
                    return await orig_key(self_, key, origin)

                async def spy_cmd(self_, name, arg=None, origin="?", log_it=True):
                    calls.append(name)
                    return await orig_cmd(self_, name, arg, origin, log_it)
                with mock.patch.object(RemoteController, "handle_key", spy_key), \
                        mock.patch.object(RemoteController, "handle_command", spy_cmd):
                    with tv.websocket_connect("/ws") as ws:
                        self.assertEqual(json.loads(ws.receive_text())["type"], "hello")
                        ws.send_text(json.dumps({"type": "key", "key": "KEY_B"}))
                        ws.send_text(json.dumps({"type": "command", "command": "PLAYER_MENU", "arg": "open"}))
                        time.sleep(0.3)
                    self.assertEqual(calls, [])
                    lan = t.e.lan()                                # the same command on the LAN does arrive
                    with lan.websocket_connect("/ws") as ws:
                        ws.receive_text()
                        ws.send_text(json.dumps({"type": "command", "command": "PLAYER_MENU", "arg": "open"}))
                        time.sleep(0.3)
                    self.assertEqual(calls, ["PLAYER_MENU"])
            finally:
                t.e.close()
        finally:
            shutil.rmtree(d, ignore_errors=True)


class RemotePlayerLayerTest(unittest.TestCase):
    """The PLAYER layer of the remote: while the /tv player's panel is open the arrows / OK / BACK drive it."""

    def make(self):
        from server.config import load_config
        from server.remote import RemoteController
        sent = []

        async def publish(m):
            sent.append(m)
        rc = RemoteController(load_config()["remote"], lambda: ["1", "16", "44"], publish)
        return rc, sent

    def run_keys(self, rc, *keys):
        async def go():
            return [await rc.handle_key(k, "test") for k in keys]
        return asyncio.run(go())

    def test_open_navigate_close(self):
        rc, sent = self.make()
        self.assertEqual(self.run_keys(rc, "KEY_DOWN"), [True])
        self.assertEqual(rc.ui.selected, "1")                           # closed: the dashboard's arrows
        self.run_keys(rc, "KEY_B")
        self.assertTrue(rc.ui.player_menu)
        self.assertEqual(rc.ui.player_cmd["action"], "menu")
        n = rc.ui.player_cmd["n"]
        for k, action, arg in (("KEY_DOWN", "nav", "down"), ("KEY_UP", "nav", "up"), ("KEY_LEFT", "nav", "left"),
                               ("KEY_RIGHT", "nav", "right"), ("KEY_OK", "ok", None), ("KEY_ENTER", "ok", None),
                               ("KEY_PLAYPAUSE", "play_pause", None), ("KEY_DOWN", "nav", "down")):
            self.run_keys(rc, k)
            n += 1
            self.assertEqual(rc.ui.player_cmd, {"n": n, "action": action, "arg": arg}, k)
        self.assertEqual(rc.ui.selected, "1")                           # the dashboard did not move meanwhile
        self.assertEqual(rc.ui.view, "overview")                        # OK did not open telemetry
        self.run_keys(rc, "KEY_BACK")
        self.assertFalse(rc.ui.player_menu)
        self.run_keys(rc, "KEY_DOWN")
        self.assertEqual(rc.ui.selected, "16")                          # closed again: arrows are the dashboard's
        self.assertTrue(sent and sent[-1]["player_menu"] is False)

    def test_repeated_presses_each_count(self):
        rc, _ = self.make()
        self.run_keys(rc, "KEY_B")
        n = rc.ui.player_cmd["n"]
        self.run_keys(rc, *(["KEY_DOWN"] * 20))
        self.assertEqual(rc.ui.player_cmd["n"], n + 20)

    def test_closed_panel_rejects_navigation_and_menus_exclude_each_other(self):
        rc, _ = self.make()

        async def c(name, arg=None):
            return await rc.handle_command(name, arg, "test")
        asyncio.run(c("PLAYER_NAV", "down"))                            # panel closed: nothing happens
        self.assertEqual(rc.ui.player_cmd["n"], 0)
        asyncio.run(c("PLAYER_NAV", "sideways"))
        self.assertEqual(rc.ui.player_cmd["n"], 0)
        asyncio.run(c("PLAYER_MENU", "open"))
        asyncio.run(c("PLAYER_NAV", "sideways"))
        self.assertNotEqual(rc.ui.player_cmd["action"], "nav")
        asyncio.run(c("SYNC_MENU", "open"))
        self.assertFalse(rc.ui.player_menu)                             # the SYNC menu takes over
        asyncio.run(c("PLAYER_MENU", "open"))
        self.assertFalse(rc.ui.sync_menu)
        asyncio.run(c("PLAYER_SEEK", "-30"))
        self.assertEqual(rc.ui.player_cmd["arg"], "-30")
        n = rc.ui.player_cmd["n"]
        asyncio.run(c("PLAYER_SEEK", "rm -rf"))
        self.assertEqual(rc.ui.player_cmd["n"], n)                      # a bad argument does nothing

    def test_config_keys(self):
        from server.config import load_config
        km = load_config()["remote"]["keymap"]
        self.assertEqual({k: km.get(k) for k in ("KEY_B", "KEY_LIST", "KEY_EPG")},
                         {"KEY_B": "PLAYER_MENU", "KEY_LIST": "PLAYER_MENU", "KEY_EPG": "PLAYER_MENU"})
        self.assertEqual(km["KEY_UP"], "MOVE_UP")                       # unchanged


class ScheduleRetryTest(unittest.TestCase):
    """The server player's F1 schedule: a failed read is retried within a minute (a session may be due)."""

    def test_retry(self):
        import server.mode as mode
        import tools.voyo_server_player as vsp
        calls = []
        ok = {"now": False}
        idx = {"Meetings": [{"Name": "Singapore Grand Prix", "Sessions": [
            {"Name": "Race", "StartDate": "2026-10-11T20:00:00", "EndDate": "2026-10-11T22:00:00", "GmtOffset": "08:00:00"}]}]}

        async def fetch(y):
            calls.append(y)
            if y == 2026 and ok["now"]:
                return idx
            raise RuntimeError("network down")
        clock = {"t": 1000.0}
        at = 1791720000.0 + 3600                                         # 2026-10-11 13:00 UTC: inside the race window
        sp = {"record_sessions": ["race"], "lead_minutes": 15, "trail_minutes": 30}
        with mock.patch.object(mode, "fetch_index", fetch), mock.patch.object(vsp, "log", lambda m: None), \
                mock.patch.object(vsp.time, "monotonic", lambda: clock["t"]):
            s = vsp.Schedule(sp)
            self.assertIsNone(s.current(at))                             # network down: nothing known
            first = len(calls)
            clock["t"] += 30
            s.current(at)
            self.assertEqual(len(calls), first)                         # not hammered
            ok["now"] = True
            clock["t"] += 31
            w = s.current(at)
            self.assertEqual(w and w["session_name"], "Race")           # read again after a minute, not 30
            self.assertEqual(calls.count(2027), 1)                      # next season: no fast retries
            ok["now"] = False
            clock["t"] += 1801
            self.assertEqual((s.current(at) or {}).get("session_name"), "Race")     # a later failure keeps the last one


class DiskFullTest(unittest.TestCase):
    """The recording disk runs below min_free_bytes while recording: the capture stops (it would otherwise pile
    its segments up in the spool on the system disk), the state says DISK FULL."""

    def test_capture_stops_and_state(self):
        from server import recordings_admin as admin
        from server.voyo_recording import VoyoStreamRecorder
        d = Path(tempfile.mkdtemp())
        try:
            rec = VoyoStreamRecorder({"path": str(d / "rec"), "min_free_bytes": 1, "record_video_capture": True},
                                     channel="server_player").start()
            self.assertTrue(rec.ok)
            rec.cur = {"stream_instance_id": "aaaa11112222"}
            self.assertTrue(rec.clock_reply()["capture"])
            with mock.patch.object(rec, "free_bytes", return_value=0), self.assertLogs("voyo-rec", "WARNING") as logs:
                self.assertFalse(rec.clock_reply()["capture"])
                self.assertFalse(rec.clock_reply()["capture"])
                st = admin.current_state(rec, rec, True, None, None)
            self.assertEqual(sum("video capture stopped" in m for m in logs.output), 1)     # said once
            self.assertEqual(st["state"], "DISK FULL")
            self.assertEqual(st["level"], "bad")
            self.assertIs(rec.status()["space_ok"], True)
            self.assertTrue(rec.clock_reply()["capture"])               # room again: capture on
        finally:
            shutil.rmtree(d, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
