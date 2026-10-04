"""VOYO playback integration: nothing on the dashboard side may stop the official VOYO player.

* the TV agent never minimizes the VOYO window (a minimized window is a hidden page: the
  browser pauses muted / video-only playback there and some players pause themselves);
  FULL_DASHBOARD sends it behind the dashboard instead;
* while a text field of the VOYO page has the focus (login, PIN, search) the agent takes no
  key away from the page;
* the server's reachability check never turns the video layer off in window mode (a request
  from the server, without the browser's login / cookies, may be refused while the VOYO window
  plays fine);
* the VOYO window is launched without any flag that weakens the browser's security, DRM or
  autoplay rules.

Run:  python -m unittest tests.test_voyo_playback
"""
import asyncio
import contextlib
import io
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import httpx  # noqa: E402

from server import video as V  # noqa: E402
from tools.tv_launcher import NullOps, TvAgent  # noqa: E402


class FakeOps(NullOps):
    name = "fake"

    def __init__(self):
        self.calls, self.hotkeys_active = [], []

    def find_voyo(self, pid, title_hint): return "w1"
    def is_valid(self, win): return win == "w1"
    def find_dashboard(self, pid): return None
    def foreground_is(self, win): return True
    def place(self, win, rect, topmost): self.calls.append(("place", topmost))
    def minimize(self, win): self.calls.append(("minimize",))
    def send_back(self, win): self.calls.append(("send_back",))

    def poll_hotkeys(self, active):
        self.hotkeys_active.append(active)
        return []


def agent(ops):
    a = TvAgent(ops, "http://127.0.0.1:1", "", 32, "VOYO", None, http_get=lambda p: {}, http_key=lambda k: None)
    return a


class AgentTest(unittest.TestCase):
    def test_full_dashboard_never_minimizes(self):
        ops = FakeOps()
        a = agent(ops)
        with contextlib.redirect_stdout(io.StringIO()):
            for mode in ("RACE_VIEW", "FULL_DASHBOARD", "VIDEO_FOCUS", "FULL_DASHBOARD"):
                a.step({"tv_mode_effective": mode})
            a.step({"tv_mode_effective": None})                  # no video layer -> FULL_DASHBOARD
        self.assertNotIn(("minimize",), ops.calls)
        self.assertEqual(ops.calls, [("place", True), ("send_back",), ("place", True), ("send_back",)])

    def test_typing_in_voyo_releases_the_hotkeys(self):
        ops = FakeOps()
        a = agent(ops)
        typing = [False]
        a.typing = lambda: typing[0]
        with contextlib.redirect_stdout(io.StringIO()):
            a.step({"tv_mode_effective": "RACE_VIEW"})
            typing[0] = True                                     # login / PIN / search field focused
            a.step({"tv_mode_effective": "RACE_VIEW"})
            typing[0] = False
            a.step({"tv_mode_effective": "RACE_VIEW"})
        self.assertEqual(ops.hotkeys_active, [True, False, True])

    def test_default_hotkeys_keep_e_for_voyo(self):
        src = (ROOT / "tools" / "tv_launcher.py").read_text(encoding="utf-8")
        default = src.split('"--hotkeys", default="', 1)[1].split('"', 1)[0].split(",")
        self.assertNotIn("E", default)                           # E = LIVE/VOD switch: not taken from VOYO


class LaunchCommandTest(unittest.TestCase):
    def test_voyo_window_flags(self):
        out = subprocess.run([sys.executable, str(ROOT / "tools" / "tv_launcher.py"), "--print",
                              "--browser", "browser.exe", "--voyo-url", "https://voyo.si/"],
                             capture_output=True, text=True, timeout=60, cwd=str(ROOT)).stdout
        voyo = next(line for line in out.splitlines() if "voyo.si" in line)
        self.assertIn("--app=https://voyo.si/", voyo)
        self.assertIn("--disable-backgrounding-occluded-windows", voyo)
        self.assertIn("browser-profiles", voyo)                    # its own normal profile (login stays)
        for bad in ("--disable-web-security", "--no-sandbox", "--ignore-certificate-errors", "--user-agent",
                    "--disable-features", "--enable-automation", "--headless", "--autoplay-policy",
                    "--allow-running-insecure-content", "--disable-site-isolation", "widevine", "--proxy"):
            self.assertNotIn(bad.lower(), voyo.lower(), bad)


class ReachabilityTest(unittest.TestCase):
    def run_monitor(self, mode, status=None):
        def handler(request):
            if status is None:
                raise httpx.ConnectError("refused")
            return httpx.Response(status, headers={"X-Frame-Options": "DENY"})

        real = httpx.AsyncClient

        def client(**kw):
            return real(transport=httpx.MockTransport(handler), **kw)

        calls = []

        class Remote:
            async def set_video_available(self, available, notice):
                calls.append((available, notice))

        class Hub:
            def set_video(self, info):
                pass

        mon = V.VideoMonitor({"enabled": True, "mode": mode, "url": "https://voyo.si/"}, Remote(), Hub())

        async def go():
            with mock.patch.object(V.httpx, "AsyncClient", client):
                task = asyncio.create_task(mon.run())
                for _ in range(100):
                    await asyncio.sleep(0.01)
                    if calls:
                        break
                task.cancel()
        asyncio.run(go())
        return calls[0]

    def test_window_mode_is_informational(self):
        for status in (None, 503):                               # connection refused / bot-protection 5xx
            available, notice = self.run_monitor("window", status)
            self.assertTrue(available, notice)
            self.assertIn("informational", notice)
        self.assertEqual(self.run_monitor("window", 200), (True, None))

    def test_embed_mode_still_respects_the_site(self):
        available, notice = self.run_monitor("embed", 200)       # X-Frame-Options: DENY is never bypassed
        self.assertFalse(available)
        self.assertIn("does not allow embedding", notice)


if __name__ == "__main__":
    unittest.main()
