"""Phone remote: /remote is served by the dashboard server and talks over the same /ws endpoint
(?client=remote). It sends the IR remote's commands and only receives the small remote state.

Runs the real app in TEST mode (simulator, no network).

Run:  python -m unittest tests.test_phone_remote
"""
import json
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from starlette.testclient import TestClient  # noqa: E402

from server.app import create_app  # noqa: E402
from server.config import load_config  # noqa: E402

SENSITIVE = ("f1tv", "token", "cookie", "auth")


def make(token=""):
    tmp = tempfile.mkdtemp()
    cfg = load_config(Path(tmp) / "none.toml")
    cfg["source"]["mode"] = "test"
    cfg["test"]["time_scale"] = 5
    cfg["voyo"]["check_reachability"] = False
    cfg["f1_tv"]["auth_file"] = str(Path(tmp) / "auth.json")
    cfg["f1_tv"]["open_browser"] = False
    cfg["remote"]["token"] = token
    cfg["remote"]["keymap"] = {"KEY_UP": "MOVE_UP", "KEY_DOWN": "MOVE_DOWN", "KEY_OK": "OPEN_TELEMETRY",
                               "KEY_BACK": "CLOSE_PANEL", "KEY_H": "TOGGLE_HELP"}
    return create_app(cfg)


def until(ws, pred, n=300):
    """Messages until pred(msg) - returns (msg, all types seen)."""
    seen = []
    for _ in range(n):
        m = ws.receive_json()
        seen.append(m["type"])
        if pred(m):
            return m, seen
    raise AssertionError(f"not received; got {sorted(set(seen))}")


class PhoneRemoteTest(unittest.TestCase):
    def test_page_info_and_lightweight_state(self):
        app = make()
        with TestClient(app, client=("192.168.1.50", 50000)) as c:
            page = c.get("/remote")
            self.assertEqual(page.status_code, 200)
            self.assertIn("client=remote", page.text)
            info = c.get("/api/remote/info").json()
            self.assertTrue(info["url"].endswith(":8080/remote"))
            self.assertIn("lan", info)
            with c.websocket_connect("/ws?client=remote") as phone:
                hello = phone.receive_json()
                self.assertEqual((hello["type"], hello["remote"]), ("hello", True))
                self.assertNotIn("keymap", hello)
                # the remote summary arrives; never positions, telemetry, the board or the track
                m, seen = until(phone, lambda m: m["type"] == "remote" and (m.get("session") or {}).get("session_name"))
                self.assertIn("flag", m)
                self.assertTrue(m["order"])
                t0 = time.time()
                while time.time() - t0 < 2.5:
                    seen.append(phone.receive_json()["type"])
                self.assertFalse(set(seen) & {"pos", "tel", "state", "track", "status", "clock", "video"}, seen)
                self.assertFalse(any(s in json.dumps(m).lower() for s in SENSITIVE))

    def test_commands_and_desktop_sync(self):
        app = make()
        with TestClient(app, client=("192.168.1.50", 50000)) as c:
            with c.websocket_connect("/ws") as desk, c.websocket_connect("/ws?client=remote") as phone, \
                    c.websocket_connect("/ws?client=remote") as phone2:
                # views (CHANGE_VIEW) - the desktop and the other phone follow
                phone.send_json({"type": "command", "command": "CHANGE_VIEW", "arg": "telemetry"})
                until(desk, lambda m: m["type"] == "ui" and m["view"] == "telemetry")
                until(phone2, lambda m: m["type"] == "ui" and m["view"] == "telemetry")
                # keys go through the same keymap as the IR remote
                phone.send_json({"type": "key", "key": "KEY_H"})
                until(desk, lambda m: m["type"] == "ui" and m["help"] is True)
                phone.send_json({"type": "key", "key": "KEY_H"})
                until(desk, lambda m: m["type"] == "ui" and m["help"] is False)
                # TV layout
                phone.send_json({"type": "command", "command": "SET_TV_MODE", "arg": "RACE_VIEW"})
                until(phone, lambda m: m["type"] == "ui" and m["tv_mode"] == "RACE_VIEW")
                # selected driver: the remote summary follows the selection
                m, _ = until(phone, lambda m: m["type"] == "remote" and m.get("order"))
                num = m["order"][3][0]
                phone.send_json({"type": "command", "command": "SELECT_DRIVER", "arg": num})
                until(phone2, lambda m: m["type"] == "remote" and (m.get("selected") or {}).get("num") == num)
                # weather report command -> its toast on every screen
                phone.send_json({"type": "command", "command": "WEATHER_REPORT"})
                until(desk, lambda m: m["type"] == "ui" and "eather" in str((m.get("toast") or {}).get("text")))
                # sync commands (the existing sync manager)
                for cmd in ("SYNC_PLUS", "SYNC_MINUS", "SYNC_MARK", "SYNC_RESYNC"):
                    phone.send_json({"type": "command", "command": cmd})
                phone.send_json({"type": "sync_action", "action": "capture", "value": None})
                r, _ = until(phone, lambda m: m["type"] == "sync_result")
                self.assertEqual(r["action"], "capture")
                # quick bursts are fine
                for _ in range(10):
                    phone.send_json({"type": "key", "key": "KEY_DOWN"})
                until(desk, lambda m: m["type"] == "ui" and m.get("selected"))

    def test_mode_both_ways(self):
        app = make()
        with TestClient(app, client=("192.168.1.50", 50000)) as c:
            with c.websocket_connect("/ws") as desk, c.websocket_connect("/ws?client=remote") as phone:
                phone.send_json({"type": "mode", "value": "VOD"})
                until(desk, lambda m: m["type"] == "mode" and m["selected_mode"] == "VOD" and not m["switching"])
                desk.send_json({"type": "mode", "value": "AUTO"})                       # the desktop's selector
                until(phone, lambda m: m["type"] == "mode" and m["selected_mode"] == "AUTO" and not m["switching"])

    def test_token(self):
        app = make(token="s3cret")
        with TestClient(app, client=("192.168.1.50", 50000)) as c:
            with self.assertRaises(Exception):
                with c.websocket_connect("/ws?client=remote") as phone:
                    phone.receive_json()
            with c.websocket_connect("/ws?client=remote&token=s3cret") as phone:
                self.assertEqual(phone.receive_json()["type"], "hello")
            info = c.get("/api/remote/info").json()                    # not loopback, no session: the token stays secret
            self.assertNotIn("s3cret", json.dumps(info))
            self.assertTrue(info["token_hidden"])
        with TestClient(app, client=("127.0.0.1", 50000)) as c:            # the dashboard PC itself still gets it for the QR
            self.assertIn("token=s3cret", c.get("/api/remote/info").json()["url"])


if __name__ == "__main__":
    unittest.main()
