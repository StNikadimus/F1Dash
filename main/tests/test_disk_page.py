"""The /disk page (server/disk/ + server/recordings_admin.py + server/activity.py): activity log
kept 48 h, what the recorder is doing now, retention settings changed on the page, deleting,
and the HTTP API.

Run (from main/):  python -m unittest tests.test_disk_page
"""
import json
import logging
import os
import shutil
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from server import recordings_admin as admin  # noqa: E402
from server.activity import ActivityHandler, ActivityLog  # noqa: E402
from server.config import REPO_ROOT, load_config  # noqa: E402
from server.voyo_recording import VoyoStreamRecorder  # noqa: E402


def recorder(root: Path, **rc) -> VoyoStreamRecorder:
    return VoyoStreamRecorder({"path": str(root), "min_free_bytes": 0, **rc}).start()


def fake_package(root: Path, iid: str, kind: str = "race", closed_days_ago: float = 1.0, video: int = 2) -> Path:
    pkg = root / iid
    (pkg / "capture").mkdir(parents=True)
    closed = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(time.time() - closed_days_ago * 86400)) + ".000Z"
    (pkg / "manifest.json").write_text(json.dumps({
        "stream_instance_id": iid, "status": "closed", "closed_at": closed, "detected_at": closed,
        "session": {"session_key": 1, "meeting": "Japanese Grand Prix", "session_name": "Race", "kind": kind},
        "capture": {"segments": [f"s{i}.mp4" for i in range(video)], "bytes": 10 * video}}))
    (pkg / "timeline.jsonl").write_text('{"t":1,"pb":0,"state":"playing","rate":1}\n')
    for i in range(video):
        (pkg / "capture" / f"s{i}.mp4").write_bytes(b"x" * 10)
    return pkg


class ActivityLogTest(unittest.TestCase):
    def setUp(self):
        self.d = Path(tempfile.mkdtemp())

    def tearDown(self):
        shutil.rmtree(self.d, ignore_errors=True)

    def test_entries_older_than_48_hours_are_deleted(self):
        now = [1_800_000_000.0]
        act = ActivityLog(self.d / "activity.jsonl", 48, clock=lambda: now[0])
        act.add("old")
        now[0] += 47 * 3600
        act.add("recent", "WARNING")
        now[0] += 2 * 3600                                                    # "old" is now 49 h old
        self.assertEqual([e["text"] for e in act.entries()], ["recent"])  # never shown
        self.assertEqual(act.prune(), 1)                                      # and removed from the file
        self.assertEqual(len((self.d / "activity.jsonl").read_text().splitlines()), 1)
        self.assertEqual(act.entries(min_level="WARNING")[0]["text"], "recent")

    def test_handler_takes_activity_info_and_all_warnings(self):
        act = ActivityLog(self.d / "a.jsonl")
        h = ActivityHandler(act)
        for name, lvl, msg in (("voyo-rec", logging.INFO, "started"), ("engine", logging.INFO, "noise"),
                               ("engine", logging.WARNING, "trouble"), ("uvicorn.access", logging.INFO, "GET /")):
            h.emit(logging.LogRecord(name, lvl, __file__, 1, msg, None, None))
        self.assertEqual([e["text"] for e in act.entries()], ["trouble", "started"])


class StateTest(unittest.TestCase):
    def setUp(self):
        self.d = Path(tempfile.mkdtemp())
        self.rec = recorder(self.d / "rec")

    def tearDown(self):
        shutil.rmtree(self.d, ignore_errors=True)

    def test_states(self):
        st = admin.current_state(self.rec, None, True, None, None)
        self.assertEqual(st["state"], "PLAYER OFF")
        hb = {"state": "idle", "next": {"meeting": "Japanese Grand Prix", "session_name": "Race"}}
        st = admin.current_state(self.rec, None, True, hb, 5)
        self.assertEqual((st["state"], st["level"]), ("REST", "idle"))
        self.assertIn("nothing to do", st["detail"])
        self.assertIn("Japanese Grand Prix Race", st["detail"])
        self.assertEqual(admin.current_state(self.rec, None, True, {"state": "open"}, 5)["state"], "OPENING")
        player = recorder(self.d / "rec")
        player.cur = {"stream_instance_id": "abc", "session": {"meeting": "GP", "session_name": "Race"},
                      "detected_at_epoch": time.time() - 600, "capture": {"segments": ["a"], "bytes": 5}}
        player.last_sample_wall = time.time()
        st = admin.current_state(self.rec, player, True, hb, 5)
        self.assertEqual((st["state"], st["segments"]), ("RECORDING", 1))
        self.assertGreaterEqual(st["elapsed_s"], 600)
        player.last_sample_wall = time.time() - 120                          # samples stopped: not recording
        self.assertNotEqual(admin.current_state(self.rec, player, True, hb, 5)["state"], "RECORDING")
        off = recorder(self.d / "nodisk", require_mount=str(self.d / "nodisk"))
        self.assertEqual(admin.current_state(off, None, True, hb, 5)["state"], "WAITING FOR DISK")

    def test_disk_info_and_sizes(self):
        fake_package(self.rec.root, "pkg00001")
        sizes = admin.Sizes().get(self.rec.root)
        self.assertEqual(sizes["pkg00001"]["video_bytes"], 20)
        self.assertEqual(sizes["pkg00001"]["segments"], 2)
        info = admin.disk_info(self.rec, sizes)
        self.assertTrue(info["ok"])
        self.assertGreater(info["total"], 0)
        self.assertEqual(info["video_bytes"], 20)


class SettingsAndDeleteTest(unittest.TestCase):
    def setUp(self):
        self.d = Path(tempfile.mkdtemp())
        self.rec = recorder(self.d / "rec", keep_race_days=30)

    def tearDown(self):
        shutil.rmtree(self.d, ignore_errors=True)

    def test_settings_validate_persist_and_apply(self):
        s = admin.Settings(self.d / "settings.json")
        with self.assertRaises(ValueError):
            s.update({"keep_race_days": -1}, (self.rec,))
        with self.assertRaises(ValueError):
            s.update({"path": "/"}, (self.rec,))                              # only retention can change
        s.update({"keep_race_days": 2}, (self.rec,))
        self.assertEqual(self.rec.keep_days("race"), 2)
        again = admin.Settings(self.d / "settings.json")                     # after a restart
        self.assertEqual(again.apply({"keep_race_days": 30})["keep_race_days"], 2)
        pkg = fake_package(self.rec.root, "pkg00002", closed_days_ago=3)
        self.assertEqual(self.rec.apply_retention(), 2)                       # 3 days > 2 days: video gone
        self.assertFalse(list((pkg / "capture").glob("*.mp4")))
        self.assertTrue((pkg / "timeline.jsonl").exists())
        rows = admin.retention_table(self.rec.rc, again.values)
        race = next(r for r in rows if r["kind"] == "race")
        self.assertEqual((race["days"], race["changed_on_page"]), (2, True))

    def test_delete_video_then_all_and_refuse_open(self):
        pkg = fake_package(self.rec.root, "pkg00003")
        self.assertIn("2 segment", admin.delete_package(self.rec, (), "pkg00003", "video"))
        self.assertTrue((pkg / "manifest.json").exists())
        self.assertFalse(list((pkg / "capture").glob("*.mp4")))
        self.rec.cur = {"stream_instance_id": "pkg00003"}
        with self.assertRaises(PermissionError):
            admin.delete_package(self.rec, (), "pkg00003", "all")
        self.rec.cur = None
        admin.delete_package(self.rec, (), "pkg00003", "all")
        self.assertFalse(pkg.exists())
        with self.assertRaises(KeyError):
            admin.delete_package(self.rec, (), "pkg00003", "all")
        with self.assertRaises(KeyError):
            admin.delete_package(self.rec, (), "../etc", "all")


class HttpTest(unittest.TestCase):
    def test_api_and_page(self):
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
                self.assertEqual(c.get("/disk").status_code, 200)
                self.assertIn("RECORDER", c.get("/disk").text)
                self.assertEqual(c.get("/disk-static/disk.js").status_code, 200)
                st = c.get("/api/disk/status").json()
                self.assertEqual(st["state"]["state"], "PLAYER OFF")
                self.assertTrue(st["token_required"])
                c.post("/api/voyo/player/status", content=json.dumps({"state": "idle", "next": None}))
                self.assertEqual(c.get("/api/disk/status").json()["state"]["state"], "REST")
                c.post("/api/voyo/player/log", content=json.dumps({"text": "OPEN for X", "level": "INFO"}))
                log_texts = [e["text"] for e in c.get("/api/disk/log").json()["entries"]]
                self.assertIn("OPEN for X", log_texts)
                self.assertEqual(c.post("/api/disk/settings", content='{"keep_race_days": 5}').status_code, 401)
                r = c.post("/api/disk/settings", content='{"keep_race_days": 5}', headers={"X-Remote-Token": "tok"})
                self.assertEqual(r.status_code, 200)
                self.assertTrue(json.loads((d / "voyo_recording_settings.json").read_text())["keep_race_days"] == 5)
                fake_package(d / "rec", "pkg00004")
                recs = c.get("/api/disk/recordings?fresh=1").json()["recordings"]
                self.assertEqual(recs[0]["video_bytes"], 20)
                self.assertEqual(recs[0]["keep_days"], 5)
                self.assertEqual(c.post("/api/disk/recordings/pkg00004/delete?what=all").status_code, 401)
                r = c.post("/api/disk/recordings/pkg00004/delete?what=all", headers={"X-Remote-Token": "tok"})
                self.assertEqual(r.json()["message"], "recording deleted")
        finally:
            shutil.rmtree(d, ignore_errors=True)

    def test_page_files_are_in_the_server_folder(self):
        for f in ("index.html", "disk.css", "disk.js"):
            self.assertTrue((REPO_ROOT / "server" / "disk" / f).is_file(), f)


if __name__ == "__main__":
    unittest.main()
