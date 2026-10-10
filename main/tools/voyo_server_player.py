#!/usr/bin/env python3
"""SERVER VOYO PLAYER - the server opens VOYO itself and records it (no PC needed).

    python tools/voyo_server_player.py login     # once: sign in to VOYO (VNC through an SSH tunnel);
                                                 # Ctrl+C saves the open page if it has a usable player
                                                 # (--save-unverified: save it even without one)
    python tools/voyo_server_player.py run       # the service: open + record around every F1 session
    python tools/voyo_server_player.py test [--minutes 5] [--url URL]   # open + record now
    python tools/voyo_server_player.py status    # what is installed / configured, next sessions

(server/voyo-player.sh runs this with the server's environment; server/systemd/f1-voyo-player.service
keeps "run" going.)

How it works - all on the Linux server, next to the dashboard backend:

* a virtual screen (Xvfb ``[voyo.server_player] display``) and, for the sound, a PulseAudio
  null sink ``f1voyo`` - nothing is shown on a monitor;
* Google Chrome on that screen with its own profile (``profile``), signed in to YOUR VOYO account
  (``login``: you sign in yourself over VNC; the password is never stored by this tool - Chrome keeps
  its normal session cookie in the profile), opening ``stream_url`` (the F1 live page);
* before each F1 session of ``record_sessions`` (the official schedule, livetiming.formula1.com
  Index.json) minus ``lead_minutes`` it opens the stream, presses play / unmute / the player's own
  fullscreen (like you would - script in the page through Chrome's local DevTools port), and keeps
  it until ``trail_minutes`` after the scheduled end (longer while the live feed still runs);
* the read-only clock bridge (tools/voyo_clock.py, channel "server_player") posts the video
  position to the dashboard server -> its own stream instances -> VOYO stream recordings
  ([voyo.recording] path, channel "server_player"), F1 session + LIVE DATA DELAY attached;
* ffmpeg records that virtual screen + the f1voyo sound (tools/voyo_capture.py) into the package's
  capture/ folder. It is a recording of what Chrome displays - this tool never reads VOYO's stream,
  its keys or buffers, and does not work around DRM: if Chrome shows no picture, nothing is recorded.

For your own use - check VOYO's terms (e.g. how many streams your account may play at the same time:
the server's stream counts as one while it records).
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import secrets
import shutil
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parent.parent                       # main/
sys.path.insert(0, str(ROOT))
from server.config import DATA_DIR, load_config, resolve_path  # noqa: E402
from server.voyo_recording import session_kind  # noqa: E402

STATE = DATA_DIR / "voyo_server_player.json"
SINK = "f1voyo"
CHROMES = ("google-chrome", "google-chrome-stable", "chrome", "chromium", "chromium-browser")

# the user's actions in the official player: play, sound on, the player's own fullscreen
PLAY_JS = r"""(() => {
  const vs = [...document.querySelectorAll('video')];
  if (!vs.length) return {video: false, url: location.href, title: document.title};
  const v = vs.sort((a, b) => b.clientWidth * b.clientHeight - a.clientWidth * a.clientHeight)[0];
  const did = [];
  if (v.paused && v.readyState >= 2 && !v.ended) { v.play().catch(() => {}); did.push('play'); }
  if (v.muted) { v.muted = false; did.push('unmute'); }
  if (FULLSCREEN && !document.fullscreenElement && v.readyState >= 2 && v.requestFullscreen) {
    v.requestFullscreen().catch(() => {}); did.push('fullscreen');
  }
  return {video: true, paused: v.paused, t: v.currentTime, ready: v.readyState, url: location.href,
          title: document.title, did};
})()"""

# login / setup: what the open page offers - read only (no play, no unmute). The page address and the
# player's state; never the media address (the stream itself stays the player's business).
INSPECT_JS = r"""(() => {
  const all = [...document.querySelectorAll('video')];
  const area = (v) => v.clientWidth * v.clientHeight;
  const base = {url: location.href, title: document.title, videos: all.length};
  if (!all.length) return Object.assign(base, {video: false});
  const v = all.sort((a, b) => area(b) - area(a))[0];
  return Object.assign(base, {video: true, visible: area(v) > 0, w: v.clientWidth, h: v.clientHeight,
    paused: v.paused, ended: v.ended, ready: v.readyState, t: v.currentTime,
    duration: isFinite(v.duration) ? v.duration : null, live: v.duration === Infinity, muted: v.muted,
    error: v.error ? v.error.code : null});
})()"""
READY_STATES = {0: "HAVE_NOTHING", 1: "HAVE_METADATA", 2: "HAVE_CURRENT_DATA", 3: "HAVE_FUTURE_DATA",
                4: "HAVE_ENOUGH_DATA"}


def player_check(st: Optional[dict]) -> tuple[bool, str]:
    """INSPECT_JS's answer -> (a usable player?, what it is in words). Usable = a visible video element
    that can play now (readyState >= 2 = HAVE_CURRENT_DATA), paused or playing, without a media error.
    It is what the recording needs: the player shows a picture that the screen capture can record."""
    if not st:
        return False, "page not reachable (loading / navigating?)"
    if not st.get("video"):
        return False, "no video element on the page"
    ready = int(st.get("ready") or 0)
    rs = f"readyState {ready} {READY_STATES.get(ready, '')}".strip()
    if st.get("error"):
        return False, f"video element with a media error (code {st['error']}, {rs})"
    if not st.get("visible"):
        return False, f"video element not visible on the screen ({rs})"
    if ready < 2:
        return False, f"video element not ready to play yet ({rs})"
    pos = float(st.get("t") or 0)
    what = "ended" if st.get("ended") else "paused" if st.get("paused") else "playing"
    return True, f"video player ready - {what} at {pos:.0f} s ({rs}, {st.get('w')}x{st.get('h')}" \
                 f"{', live' if st.get('live') else ''}{', muted' if st.get('muted') else ''})"


def _same_page(a: Optional[str], b: Optional[str]) -> bool:
    """The same page address (the #fragment does not count)."""
    return bool(a and b) and a.split("#", 1)[0] == b.split("#", 1)[0]


_FORWARD = {"server": None, "token": "", "off_until": 0.0}


def _post(path: str, body: dict, timeout: float = 1.5) -> Optional[dict]:
    """POST to the dashboard server (local); quiet and short when it is not running. -> its JSON answer"""
    srv = _FORWARD["server"]
    if not srv or time.monotonic() < _FORWARD["off_until"]:
        return None
    try:
        req = urllib.request.Request(srv + path, method="POST", data=json.dumps(body, default=str).encode(),
                                     headers={"Content-Type": "application/json",
                                              **({"X-Remote-Token": _FORWARD["token"]} if _FORWARD["token"] else {})})
        raw = urllib.request.urlopen(req, timeout=timeout).read()
        try:
            return json.loads(raw or b"{}")
        except ValueError:
            return {}
    except OSError:
        _FORWARD["off_until"] = time.monotonic() + 30            # server down: don't slow the player
        return None


CREDS = DATA_DIR / "auth" / "voyo_credentials.json"

# VOYO's own sign-in form: find it (or the link that opens it). Reads the page only - the e-mail and
# password are typed with Chrome's Input.insertText, never put into a script.
LOGIN_FIND_JS = r"""(() => {
  const vis = (e) => !!(e && e.offsetParent !== null && e.getBoundingClientRect().width > 0);
  const pw = [...document.querySelectorAll('input[type=password]')].find(vis);
  if (pw) {
    const form = pw.form || document;
    const cands = [...form.querySelectorAll('input')].filter((i) => vis(i) && i !== pw &&
      /^(email|text|tel|)$/.test((i.getAttribute('type') || '').toLowerCase()));
    const em = cands.find((i) => /mail|user|uporab|login/i.test((i.name || '') + (i.id || '') +
      (i.autocomplete || '') + (i.placeholder || '') + (i.type || ''))) || cands[0];
    return {form: true, email: !!em, url: location.href, title: document.title};
  }
  const words = /^(prijava|prijavi se|vpis|vpiši se|sign in|log ?in|login|moj račun)$/i;
  const link = [...document.querySelectorAll('a,button,[role=button]')].find((e) => vis(e) &&
    words.test((e.innerText || e.getAttribute('aria-label') || '').trim()));
  return {form: false, link: link ? (link.innerText || link.getAttribute('aria-label') || '').trim() : null,
          url: location.href, title: document.title};
})()"""
LOGIN_CLICK_JS = r"""(() => {
  const vis = (e) => !!(e && e.offsetParent !== null);
  const words = /^(prijava|prijavi se|vpis|vpiši se|sign in|log ?in|login|moj račun)$/i;
  const link = [...document.querySelectorAll('a,button,[role=button]')].find((e) => vis(e) &&
    words.test((e.innerText || e.getAttribute('aria-label') || '').trim()));
  if (link) { link.click(); return true; } return false;
})()"""
LOGIN_FOCUS_JS = r"""((which) => {
  const vis = (e) => !!(e && e.offsetParent !== null && e.getBoundingClientRect().width > 0);
  const pw = [...document.querySelectorAll('input[type=password]')].find(vis);
  if (!pw) return false;
  let el = pw;
  if (which === 'email') {
    const form = pw.form || document;
    const cands = [...form.querySelectorAll('input')].filter((i) => vis(i) && i !== pw &&
      /^(email|text|tel|)$/.test((i.getAttribute('type') || '').toLowerCase()));
    el = cands.find((i) => /mail|user|uporab|login/i.test((i.name || '') + (i.id || '') + (i.autocomplete || '') +
      (i.placeholder || '') + (i.type || ''))) || cands[0];
    if (!el) return false;
  }
  el.focus(); el.select && el.select(); return true;
})"""


def log(msg: str) -> None:
    print(time.strftime("%Y-%m-%d %H:%M:%S ") + msg, flush=True)
    text = msg.strip()
    low = text.lower()
    level = "WARNING" if any(w in low for w in ("warning", "failed", "not reachable", "no video", "lost", "error",
                                                 "missing", "not installed", "not found", "not set")) else "INFO"
    _post("/api/voyo/player/log", {"text": text, "level": level})      # -> the /disk page's activity log


def load_state() -> dict:
    try:
        return json.loads(STATE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save_state(d: dict) -> None:
    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps(d, indent=1), encoding="utf-8")


def find_browser(sp: dict) -> Optional[str]:
    if sp.get("browser"):
        p = shutil.which(str(sp["browser"])) or (str(sp["browser"]) if Path(str(sp["browser"])).is_file() else None)
        return p
    for name in CHROMES:
        p = shutil.which(name)
        if p:
            return p
    return None


def has_widevine(browser: Optional[str]) -> bool:
    """Google Chrome ships the Widevine CDM VOYO's player needs (most distro Chromium builds do not)."""
    if not browser:
        return False
    real = Path(os.path.realpath(browser))
    return any((d / "WidevineCdm").exists() for d in (real.parent, Path("/opt/google/chrome")))


# ---------------------------------------------------------------------------------------------
# processes
# ---------------------------------------------------------------------------------------------
class Proc:
    def __init__(self, name: str) -> None:
        self.name = name
        self.p: Optional[subprocess.Popen] = None

    def alive(self) -> bool:
        return self.p is not None and self.p.poll() is None

    def start(self, cmd: list[str], env: Optional[dict] = None) -> None:
        self.p = subprocess.Popen(cmd, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                  stderr=subprocess.DEVNULL, start_new_session=True)

    def stop(self, timeout: float = 8) -> None:
        p, self.p = self.p, None
        if p is None or p.poll() is not None:
            return
        try:
            os.killpg(p.pid, signal.SIGTERM)
            p.wait(timeout)
        except (OSError, subprocess.TimeoutExpired):
            try:
                os.killpg(p.pid, signal.SIGKILL)
            except OSError:
                pass


class Player:
    def __init__(self, cfg: dict) -> None:
        self.cfg = cfg
        self.sp = (cfg.get("voyo") or {}).get("server_player") or {}
        self.rc = (cfg.get("voyo") or {}).get("recording") or {}
        self.server = f"http://127.0.0.1:{int(cfg['server'].get('port', 8080))}"
        self.token = str((cfg.get("remote") or {}).get("token") or "")
        _FORWARD.update(server=self.server, token=self.token)
        self.note = ""                                         # last thing the page said (for the heartbeat)
        self.last_login_try = -1e9
        self.last_reload = -1e9
        self.login_result: Optional[dict] = None
        self.display = str(self.sp.get("display") or ":90")
        self.cdp_port = int(self.sp.get("cdp_port", 9224))
        self.profile = resolve_path(str(self.sp.get("profile") or "data/browser-profiles/voyo-server"))
        self.xvfb, self.pulse, self.chrome, self.vnc = Proc("Xvfb"), Proc("pulseaudio"), Proc("chrome"), Proc("x11vnc")
        self.pulse_dir = DATA_DIR / "voyo-server-pulse"
        self.bridge = None
        self.capture = None
        self.hint: dict = {}
        self.opened_at = 0.0
        self.last_play = 0.0

    # ------------------------------------------------------------------ environment
    def env(self) -> dict:
        e = dict(os.environ, DISPLAY=self.display)
        if self.pulse.alive():
            e.update(PULSE_SERVER=f"unix:{self.pulse_dir / 'native'}", PULSE_SINK=SINK)
        return e

    def start_display(self) -> None:
        if self.xvfb.alive():
            return
        n = self.display.lstrip(":").split(".")[0]
        sock = Path(f"/tmp/.X11-unix/X{n}")
        if sock.exists():
            log(f"virtual screen {self.display} already exists - using it")
            return
        if not shutil.which("Xvfb"):
            raise SystemExit("Xvfb is not installed (server/setup-voyo-player.sh)")
        res = str(self.sp.get("resolution") or "1920x1080")
        self.xvfb.start(["Xvfb", self.display, "-screen", "0", f"{res}x24", "-nolisten", "tcp", "-ac"])
        for _ in range(50):
            if sock.exists():
                break
            time.sleep(0.1)
        log(f"virtual screen {self.display} ({res}) started")

    def start_audio(self) -> None:
        if not self.sp.get("audio", True) or self.pulse.alive():
            return
        if not shutil.which("pulseaudio"):
            log("pulseaudio not installed - recording without sound (server/setup-voyo-player.sh)")
            return
        self.pulse_dir.mkdir(parents=True, exist_ok=True)
        env = dict(os.environ, PULSE_RUNTIME_PATH=str(self.pulse_dir), PULSE_STATE_PATH=str(self.pulse_dir))
        self.pulse.start(["pulseaudio", "-n", "--daemonize=no", "--exit-idle-time=-1", "--use-pid-file=no",
                          "--disallow-exit", "-L", f"module-native-protocol-unix auth-anonymous=1 "
                          f"socket={self.pulse_dir / 'native'}",
                          "-L", f"module-null-sink sink_name={SINK} sink_properties=device.description=F1VOYO"],
                         env=env)
        for _ in range(50):
            if (self.pulse_dir / "native").exists():
                break
            time.sleep(0.1)
        log(f"sound: PulseAudio null sink '{SINK}' (recorded from {SINK}.monitor)")

    def start_chrome(self, url: str) -> None:
        browser = find_browser(self.sp)
        if not browser:
            raise SystemExit("Google Chrome not found (server/setup-voyo-player.sh) - or set [voyo.server_player] browser")
        if not has_widevine(browser):
            log(f"WARNING: {browser} has no Widevine CDM - VOYO's protected stream will not play. Install "
                "Google Chrome (server/setup-voyo-player.sh).")
        self.profile.mkdir(parents=True, exist_ok=True)
        w, h = (str(self.sp.get("resolution") or "1920x1080").split("x") + ["1080"])[:2]
        self.chrome.start([browser, f"--user-data-dir={self.profile}", f"--app={url}", "--start-fullscreen",
                           "--window-position=0,0", f"--window-size={w},{h}", "--kiosk",
                           f"--remote-debugging-port={self.cdp_port}", "--remote-allow-origins=http://127.0.0.1",
                           "--autoplay-policy=no-user-gesture-required", "--no-first-run",
                           "--no-default-browser-check", "--disable-session-crashed-bubble", "--disable-infobars",
                           "--disable-features=Translate,MediaRouter", "--password-store=basic",
                           "--disable-gpu", "--disable-backgrounding-occluded-windows", "--noerrdialogs",
                           *(["--no-sandbox"] if hasattr(os, "geteuid") and os.geteuid() == 0 else [])],
                          env=self.env())
        log(f"Chrome opened {url}")

    # ------------------------------------------------------------------ the page
    def _targets(self) -> list[dict]:
        with urllib.request.urlopen(f"http://127.0.0.1:{self.cdp_port}/json/list", timeout=2) as r:
            return [t for t in json.loads(r.read()) if t.get("type") == "page"]

    def page_url(self) -> Optional[str]:
        try:
            pages = self._targets()
        except OSError:
            return None
        return pages[0].get("url") if pages else None

    def cdp(self, calls: list[tuple[str, dict]]) -> list:
        """Run DevTools calls on the VOYO page (local port), in order; -> their results."""
        from websockets.sync.client import connect
        try:
            pages = self._targets()
        except OSError:
            return []
        if not pages:
            return []
        out = []
        with connect(pages[0]["webSocketDebuggerUrl"], open_timeout=3, max_size=2 ** 22) as ws:
            for i, (method, params) in enumerate(calls, 1):
                ws.send(json.dumps({"id": i, "method": method, "params": params}))
                deadline = time.monotonic() + 5
                res = None
                while time.monotonic() < deadline:
                    msg = json.loads(ws.recv(timeout=5))
                    if msg.get("id") == i:
                        res = msg.get("result") or {}
                        break
                out.append(res)
        return out

    def evaluate(self, expr: str) -> Optional[dict]:
        res = self.cdp([("Runtime.evaluate", {"expression": expr, "returnByValue": True, "userGesture": True,
                                              "awaitPromise": False})])
        return ((res[0] or {}).get("result") or {}).get("value") if res else None

    def inspect_page(self) -> Optional[dict]:
        """The open page and its video player (INSPECT_JS); None while it cannot be read (navigating, Chrome
        not up yet). Read only."""
        try:
            st = self.evaluate(INSPECT_JS)
        except Exception:  # noqa: BLE001 - DevTools socket closed / timed out mid-navigation
            return None
        return st if isinstance(st, dict) else None

    # ------------------------------------------------------------------ VOYO sign-in (credentials from /disk)
    def auto_login(self, why: str) -> dict:
        """Type the saved e-mail + password into VOYO's sign-in form, like a password manager.
        -> {"ok": bool, "result": text} (the password never appears in a log or a script)."""
        c = {}
        try:
            c = json.loads(CREDS.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            pass
        self.last_login_try = time.monotonic()
        if not c.get("email") or not c.get("password"):
            return self._login_done(False, "no VOYO e-mail / password saved (the /disk page, VOYO ACCOUNT)")
        st = self.evaluate(LOGIN_FIND_JS) or {}
        if not st.get("form") and st.get("link"):
            self.evaluate(LOGIN_CLICK_JS)
            for _ in range(10):
                time.sleep(1)
                st = self.evaluate(LOGIN_FIND_JS) or {}
                if st.get("form"):
                    break
        if not st.get("form"):
            return self._login_done(True, f"no sign-in form on the page - already signed in? ({st.get('title') or st.get('url')})",
                                    level="INFO")
        steps = []
        if st.get("email"):
            steps += [("Runtime.evaluate", {"expression": LOGIN_FOCUS_JS + "('email')", "returnByValue": True}),
                      ("Input.insertText", {"text": c["email"]})]
        steps += [("Runtime.evaluate", {"expression": LOGIN_FOCUS_JS + "('password')", "returnByValue": True}),
                  ("Input.insertText", {"text": c["password"]}),
                  ("Input.dispatchKeyEvent", {"type": "keyDown", "key": "Enter", "code": "Enter",
                                              "windowsVirtualKeyCode": 13, "text": "\r"}),
                  ("Input.dispatchKeyEvent", {"type": "keyUp", "key": "Enter", "code": "Enter",
                                              "windowsVirtualKeyCode": 13})]
        self.cdp(steps)
        for _ in range(15):
            time.sleep(1)
            st = self.evaluate(LOGIN_FIND_JS) or {}
            if not st.get("form"):
                return self._login_done(True, f"signed in to VOYO ({why})", level="INFO")
        return self._login_done(False, "still on VOYO's sign-in page - wrong e-mail / password, or VOYO asks for an "
                                       "extra check (code / captcha): sign in once by hand (voyo-player.sh login)")

    def _login_done(self, ok: bool, text: str, level: str = "WARNING") -> dict:
        log(("VOYO login: " if ok else "VOYO login FAILED: ") + text)
        self.login_result = {"ok": ok, "result": text, "at": time.time()}
        return self.login_result

    def ensure_playing(self) -> None:
        js = PLAY_JS.replace("FULLSCREEN", "true" if self.sp.get("fullscreen_video", True) else "false")
        try:
            st = self.evaluate(js)
        except Exception as exc:  # noqa: BLE001
            log(f"page not reachable ({exc})")
            return
        if not st:
            return
        if not st.get("video"):
            self.note = f"no video on the page yet ({st.get('title') or st.get('url')})"
            # network error page (internet down, VOYO unreachable): load the stream page again, once a minute
            if str(st.get("url") or "").startswith("chrome-error:") and time.monotonic() - self.last_reload > 60:
                self.last_reload = time.monotonic()
                log("the VOYO page did not load (network error) - loading it again")
                self.cdp([("Page.navigate", {"url": self.stream_url() or "https://voyo.si/"})])
                return
            # VOYO signed us out: sign in again with the saved account (at most every 10 min)
            lf = (self.evaluate(LOGIN_FIND_JS) or {}) if CREDS.exists() else {}
            if CREDS.exists() and time.monotonic() - self.last_login_try > 600 and (lf.get("form") or lf.get("link")):
                if self.auto_login("VOYO asked for it").get("ok"):
                    self.cdp([("Page.navigate", {"url": self.stream_url() or "https://voyo.si/"})])
                return
            if time.monotonic() - self.last_play > 120:
                self.last_play = time.monotonic()
                log(f"no video on the page yet ({st.get('title')!r}, {st.get('url')}) - signed in? the F1 stream "
                    "page? (voyo_server_player.py login)")
            return
        self.note = f"{'paused' if st.get('paused') else 'playing'} at {st.get('t', 0):.0f} s"
        if st.get("did"):
            log(f"player: {', '.join(st['did'])} - position {st.get('t', 0):.0f} s, "
                f"{'paused' if st.get('paused') else 'playing'}")

    # ------------------------------------------------------------------ open / close
    def stream_url(self) -> Optional[str]:
        """/disk page > [voyo.server_player] stream_url > the page learned at "login"."""
        st = load_state()
        return st.get("page_url") or str(self.sp.get("stream_url") or "") or st.get("last_url")

    def login_session(self) -> dict:
        """LOGIN NOW from the /disk page while VOYO is closed: open it, sign in, close it again."""
        if self.is_open:
            return self.auto_login("LOGIN NOW")
        self.start_display()
        self.start_chrome(self.stream_url() or "https://voyo.si/")
        try:
            for _ in range(20):
                time.sleep(1)
                if self.page_url():
                    break
            time.sleep(4)                                      # the page's own scripts
            return self.auto_login("LOGIN NOW")
        finally:
            self.chrome.stop()
            self.xvfb.stop()

    def open(self, url: str, hint: dict) -> None:
        from tools.voyo_capture import VoyoWindowCapture
        from tools.voyo_clock import VoyoClockBridge
        self.hint = hint
        self.start_display()
        self.start_audio()
        self.start_chrome(url)
        match = (urlparse(url).netloc or "voyo").lower()
        self.bridge = VoyoClockBridge(self.server, self.cdp_port, self.token, 5.0, match=match, log=log,
                                      channel="server_player")
        self.bridge.extra = lambda: {"session_hint": self.hint}
        audio = {"format": "pulse", "device": f"{SINK}.monitor"} if self.pulse.alive() else ""
        if audio:
            os.environ.update(PULSE_SERVER=f"unix:{self.pulse_dir / 'native'}")
        self.capture = VoyoWindowCapture(self.server, self.token, self.rc, DATA_DIR / "voyo_server_spool",
                                         lambda: {"window_id": "0", "display": self.display, "audio": audio},
                                         log=log,
                                         live_dir=DATA_DIR / "live" if self.sp.get("live_stream", True) else None)
        self.bridge.on_reply = self.capture.on_reply
        self.bridge.start()
        self.capture.start()
        self.opened_at = time.monotonic()
        log(f"OPEN for {hint.get('meeting') or ''} {hint.get('session_name') or ''} - recording via {self.server}")

    def close(self, why: str) -> None:
        if self.capture is not None:
            self.capture.on_reply(None)
            self.capture.stop()
        if self.bridge is not None:
            self.bridge.stop.set()
        self.chrome.stop()
        self.bridge = self.capture = None
        self.pulse.stop()
        self.xvfb.stop()
        try:                                   # the dashboard server closes this stream's package
            req = urllib.request.Request(f"{self.server}/api/sync/voyo", method="POST", data=json.dumps(
                {"channel": "server_player", "close": True, "why": why, "playback_time": 0}).encode(),
                headers={"Content-Type": "application/json", **({"X-Remote-Token": self.token} if self.token else {})})
            urllib.request.urlopen(req, timeout=5).read()
        except OSError as exc:
            log(f"dashboard server not reachable to close the recording ({exc})")
        log(f"CLOSED ({why})")

    @property
    def is_open(self) -> bool:
        return self.bridge is not None

    def tick(self) -> None:
        """While open: Chrome alive, the video playing."""
        if not self.chrome.alive():
            log("Chrome stopped - opening the stream again")
            self.start_chrome(self.stream_url() or "https://voyo.si/")
            time.sleep(5)
        self.ensure_playing()

    # ------------------------------------------------------------------ schedule
    def feed_live(self) -> bool:
        try:
            with urllib.request.urlopen(f"{self.server}/api/mode", timeout=3) as r:
                return json.loads(r.read()).get("feed_live") is True
        except (OSError, ValueError):
            return False


def session_windows(index: Optional[dict], sp: dict) -> list[dict]:
    """Every session of ``record_sessions`` with its open window (UTC epoch s)."""
    from server.sources.f1_live import _local_to_utc
    kinds = {str(k) for k in (sp.get("record_sessions") or [])}
    lead = float(sp.get("lead_minutes", 15)) * 60
    trail = float(sp.get("trail_minutes", 30)) * 60
    out = []
    for m in (index or {}).get("Meetings") or []:
        for s in m.get("Sessions") or []:
            start = _local_to_utc(s.get("StartDate"), s.get("GmtOffset"))
            end = _local_to_utc(s.get("EndDate"), s.get("GmtOffset")) or start
            if not start:
                continue
            kind = session_kind(s.get("Name"))
            if kind not in kinds:
                continue
            out.append({"meeting": m.get("Name"), "session_name": s.get("Name"), "kind": kind,
                        "start": start.isoformat(), "end": end.isoformat(),
                        "open_from": start.timestamp() - lead, "open_until": end.timestamp() + trail})
    return sorted(out, key=lambda w: w["open_from"])


class Schedule:
    def __init__(self, sp: dict) -> None:
        self.sp = sp
        self._index: dict[int, dict] = {}
        self._at: dict[int, float] = {}

    def windows(self, now: float) -> list[dict]:
        from server.mode import fetch_index
        year = datetime.fromtimestamp(now, timezone.utc).year
        out = []
        for y in (year, year + 1):
            if time.monotonic() - self._at.get(y, -1e9) > 1800:
                try:
                    self._index[y] = asyncio.run(fetch_index(y))
                except Exception as exc:  # noqa: BLE001
                    if y == year:
                        log(f"F1 schedule {y} not reachable ({exc}) - retrying")
                self._at[y] = time.monotonic()
            out += session_windows(self._index.get(y), self.sp)
        return out

    def current(self, now: float) -> Optional[dict]:
        return next((w for w in self.windows(now) if w["open_from"] <= now <= w["open_until"]), None)


# ---------------------------------------------------------------------------------------------
# the dashboard server, read from this machine (status / test)
# ---------------------------------------------------------------------------------------------
def get_json(url: str, timeout: float) -> tuple[Optional[int], object, str]:
    """GET -> (HTTP status, JSON body, problem). Status None = no answer at all (not running, refused,
    timed out); an HTTP error status (401 ...) means the server IS there and answered."""
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            code, raw = r.status, r.read()
    except urllib.error.HTTPError as exc:                 # an answer (401 / 403 / 500 ...) - before OSError
        exc.close()
        return exc.code, None, f"HTTP {exc.code}"
    except (OSError, ValueError) as exc:                  # URLError (refused, DNS), timeout
        return None, None, str(getattr(exc, "reason", None) or exc) or type(exc).__name__
    try:
        return code, json.loads(raw), ""
    except ValueError:
        return code, None, "not a JSON answer"


def recorder_state(server: str, timeout: float = 3) -> tuple[Optional[dict], str]:
    """The recording side's state from /api/health - the dashboard server tells it only to this machine
    (loopback; server/update-f1dash.sh uses it too). -> ({"state", "busy"} or None, why not)."""
    code, body, problem = get_json(f"{server}/api/health", timeout)
    if code is None:
        return None, f"dashboard server not reachable ({problem})"
    if code != 200:
        return None, f"/api/health answered {problem}"
    rec = body.get("recorder") if isinstance(body, dict) else None
    if not isinstance(rec, dict) or not rec.get("state"):
        return None, "/api/health did not say (not asked from this machine, or the server could not tell)"
    return {"state": str(rec.get("state")), "busy": rec.get("busy") is True}, ""


def recorder_line(server: str) -> str:
    rec, why = recorder_state(server)
    if rec is None:
        return f"UNKNOWN - {why}"
    return f"{rec['state']}  (busy: {'yes' if rec['busy'] else 'no'})"


# ---------------------------------------------------------------------------------------------
# commands
# ---------------------------------------------------------------------------------------------
def heartbeat(player: Player, nxt: Optional[dict], problem: str = "") -> list:
    """Tell the dashboard server what the player is doing (the /disk page's status); -> its commands
    for the player (e.g. "login" from the /disk page)."""
    ans = _post("/api/voyo/player/status", {
        "login": player.login_result,
        "state": "open" if player.is_open else "idle", "pid": os.getpid(), "at": time.time(),
        "session": " ".join(x for x in ((player.hint or {}).get("meeting"), (player.hint or {}).get("session_name"))
                            if x) if player.is_open else None,
        "note": player.note if player.is_open else None, "problem": problem or None,
        "stream_url_set": bool(player.stream_url()), "browser": find_browser(player.sp),
        "widevine": has_widevine(find_browser(player.sp)),
        "next": None if not nxt else {k: nxt.get(k) for k in ("meeting", "session_name", "kind", "start", "end",
                                                              "open_from", "open_until")}})
    return list((ans or {}).get("commands") or [])


def cmd_run(player: Player) -> None:
    sp = player.sp
    if not sp.get("enabled"):
        raise SystemExit("[voyo.server_player] enabled = false - nothing to do")
    sched = Schedule(sp)
    stop = {"now": False}
    signal.signal(signal.SIGTERM, lambda *a: stop.update(now=True))
    log(f"server VOYO player: {sp.get('when')} - recording {', '.join(sp.get('record_sessions') or [])}")
    nxt = [w for w in sched.windows(time.time()) if w["open_until"] > time.time()][:1]
    if nxt:
        log(f"next: {nxt[0]['meeting']} {nxt[0]['session_name']} - opens "
            f"{datetime.fromtimestamp(nxt[0]['open_from'], timezone.utc):%Y-%m-%d %H:%M} UTC")
    try:
        while not stop["now"]:
            now = time.time()
            when = str(sp.get("when") or "schedule")
            win = {"session_name": "always"} if when == "always" else sched.current(now) if when == "schedule" else None
            if win and not player.is_open:
                url = player.stream_url()
                if not url:
                    log("no stream_url and no page learned yet - run: voyo_server_player.py login")
                else:
                    player.open(url, {k: win.get(k) for k in ("meeting", "session_name", "start", "end")})
            elif not win and player.is_open:
                if sp.get("keep_open_while_feed_live", True) and player.feed_live():
                    pass                                   # the session still runs (red flag / delay)
                else:
                    player.close("session window over")
            if player.is_open:
                player.tick()
            upcoming = [w for w in sched.windows(now) if w["open_until"] > now] if when == "schedule" else []
            problem = "" if player.stream_url() else "VOYO not set up: no stream page yet (voyo-player.sh login)"
            if not find_browser(sp):
                problem = "Google Chrome is not installed (setup-voyo-player.sh)"
            for c in heartbeat(player, upcoming[0] if upcoming else None, problem):
                if c == "login":
                    log("LOGIN NOW (the /disk page)")
                    player.login_session()
                    heartbeat(player, upcoming[0] if upcoming else None, problem)
            for _ in range(15):
                if stop["now"]:
                    break
                time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        if player.is_open:
            player.close("stopped")
        heartbeat(player, None, "the server VOYO player was stopped")


def cmd_test(player: Player, minutes: float, url: Optional[str]) -> None:
    url = url or player.stream_url()
    if not url:
        raise SystemExit("no stream_url - pass --url or run login first")
    player.open(url, {"meeting": "TEST", "session_name": "Test recording"})
    end = time.monotonic() + minutes * 60
    try:
        while time.monotonic() < end:
            player.tick()
            heartbeat(player, None)
            time.sleep(5)
    except KeyboardInterrupt:
        pass
    finally:
        player.close("test finished")
        heartbeat(player, None)
    code, st, problem = get_json(f"{player.server}/api/voyo/recordings", 5)
    if code is None:
        log(f"dashboard server not reachable - is it running? (the recordings are written by it; {problem})")
        return
    if code in (401, 403):
        log(f"dashboard server reachable; the recordings list needs a /disk login ({problem}) - see the /disk "
            f"page. Recorder: {recorder_line(player.server)}")
        return
    try:
        if code != 200:
            raise ValueError(problem)
        recs = [x for x in st["recordings"] if x.get("channel") == "server_player"][:1]
        for x in recs:
            log(f"recording {x['stream_instance_id']}: {x['status']}, {x['watched_seconds']} s, "
                f"{x['capture_segments']} video segment(s), {x['capture_bytes'] / 1e6:.1f} MB")
    except (ValueError, KeyError, TypeError) as exc:
        log(f"dashboard server answered, but not with the recordings list ({exc or problem})")


class LoginWatch:
    """What "login" has seen: the open page and whether it had a usable video player (player_check)."""

    def __init__(self) -> None:
        self.page: Optional[str] = None                 # the VOYO page open now (its address, never a media URL)
        self.title = ""
        self.ok = False                                 # the page open now has a usable player (last check)
        self.info = "the page could not be read"        # the last check in words
        self.verified: Optional[str] = None             # the last page on which a usable player was seen
        self.verified_info = ""
        self.said = None

    def update(self, st: Optional[dict], fallback_url: Optional[str] = None) -> Optional[str]:
        """One look at the page -> a line to print when something changed (else None)."""
        if st is None and (not fallback_url or _same_page(fallback_url, self.page)):
            return None                                 # unreadable for a moment (navigating): keep the last state
        url = (st or {}).get("url") or fallback_url
        if url and not str(url).startswith("http"):
            url = None                                  # about:blank, chrome-error:// ...: not a VOYO page
        ok, info = player_check(st) if url else (False, "no web page open")
        self.page, self.title, self.ok, self.info = url, str((st or {}).get("title") or "")[:120], ok, info
        if ok:
            self.verified, self.verified_info = url, info
        key = (url, self.title, ok, info.split(" - ")[0] if ok else info)
        if key == self.said:
            return None
        self.said = key
        if not url:
            return f"  page: {info}"
        return f"  page: {url}" + (f"  \"{self.title}\"" if self.title else "") + \
               f"\n    player: {info}" + ("" if ok else " - not usable yet")


def login_decision(watch: LoginWatch, state: dict, config_url: str, force: bool,
                   confirm, say=None) -> tuple[Optional[dict], list[str]]:
    """After Ctrl+C: what to save. -> (the new state to write or None = leave it as it is, the lines said).
    A page counts as set up only when a usable player was seen on it; anything else needs an explicit
    yes (``confirm(question)``) or ``--save-unverified``. A configured stream_url is never touched.
    ``say`` prints each line as it comes (the warning before the question)."""
    class _Out(list):
        def append(self, line: str) -> None:
            super().append(line)
            if say:
                say(line)
    out = _Out()
    page = watch.page
    if config_url:
        out.append(f"[voyo.server_player] stream_url is set ({config_url}) - it stays the stream page; this "
                   "login only refreshed the VOYO sign-in in Chrome's profile. Nothing saved.")
        if page:
            out.append(f"  (open at the end: {page} - {'usable player' if watch.ok else 'no usable player confirmed'})")
        return None, list(out)
    if not page:
        out.append("No VOYO page was open - nothing saved" +
                   (f"; the stream page stays {state['last_url']}." if state.get("last_url") else "."))
        return None, list(out)
    old = state.get("last_url")
    verified = watch.ok or _same_page(page, watch.verified)
    if verified:
        info = watch.verified_info if _same_page(page, watch.verified) else ""
        new = dict(state, last_url=page, learned_at=datetime.now(timezone.utc).isoformat(), last_url_verified=True)
        out.append(f"Saved as the stream page: {page}" + (f"\n  ({info})" if info else ""))
    else:
        out.append(f"WARNING: no usable video player was confirmed on {page}\n  (last check: {watch.info})")
        if watch.verified and not _same_page(watch.verified, page):
            out.append(f"  A usable player was seen earlier on {watch.verified} - open that page again and wait "
                       "for the video before Ctrl+C.")
        replace = f" It would replace the saved stream page {old}." if old and not _same_page(old, page) else ""
        if force:
            ok = True
            out.append("--save-unverified: saving it anyway." + replace)
        else:
            ok = bool(confirm(f"Save {page} as the stream page anyway?{replace} [y/N] "))
        if not ok:
            out.append("Nothing saved" + (f" - the stream page stays {old}." if old else ".") +
                       " Run login again, open the F1 stream page and wait until the video shows (READY / playing) "
                       "before Ctrl+C - or run: voyo-player.sh login --save-unverified")
            return None, list(out)
        new = dict(state, last_url=page, learned_at=datetime.now(timezone.utc).isoformat(), last_url_verified=False)
        out.append(f"Saved (UNVERIFIED - no player was confirmed) as the stream page: {page}")
    if state.get("page_url"):
        out.append(f"Note: the stream page set on the /disk page ({state['page_url']}) still comes first.")
    return new, list(out)


def _ask_tty(question: str) -> bool:
    """An explicit yes on the terminal; no terminal, Ctrl+C or EOF = no."""
    if not sys.stdin or not sys.stdin.isatty():
        print(question + "(no terminal to answer - not saved)", flush=True)
        return False
    try:
        return input(question).strip().lower() in ("y", "yes", "j", "ja", "d", "da")
    except (EOFError, KeyboardInterrupt):
        print(flush=True)
        return False


def cmd_login(player: Player, save_unverified: bool = False, confirm=_ask_tty, poll_s: float = 3.0) -> None:
    """Chrome on the virtual screen + VNC (127.0.0.1 only) so you can sign in to VOYO once."""
    if not shutil.which("x11vnc"):
        raise SystemExit("x11vnc is not installed (server/setup-voyo-player.sh)")
    config_url = str(player.sp.get("stream_url") or "")
    url = config_url or load_state().get("last_url") or "https://voyo.si/"
    player.start_display()
    player.start_audio()
    player.start_chrome(url)
    pw = secrets.token_urlsafe(6)
    port = int(player.sp.get("vnc_port", 5900))
    player.vnc.start(["x11vnc", "-display", player.display, "-localhost", "-rfbport", str(port), "-forever",
                      "-shared", "-passwd", pw, "-quiet"], env=player.env())
    print(f"""
VOYO sign-in on the server's virtual screen
  1. on your PC:   ssh -L {port}:127.0.0.1:{port} <user>@<server>
  2. VNC viewer (e.g. RealVNC Viewer / TigerVNC) -> 127.0.0.1:{port}   password: {pw}
  3. sign in to VOYO, open the F1 live stream page (the one you want recorded) and start the video -
     wait until this terminal says "player: video player ready" (paused is fine)
  4. press Ctrl+C here: that page becomes the stream page (unless [voyo.server_player] stream_url is
     set). A page without a usable player is NOT saved unless you confirm it (or --save-unverified).
""", flush=True)
    watch = LoginWatch()
    try:
        try:
            while True:
                line = watch.update(player.inspect_page(), player.page_url())
                if line:
                    print(line, flush=True)
                time.sleep(poll_s)
        except KeyboardInterrupt:
            print(flush=True)
        # Chrome, the screen and VNC still run here (their own process groups - Ctrl+C does not reach them):
        # one last look at the page, decide, save - and only then close them
        line = watch.update(player.inspect_page(), player.page_url())
        if line:
            print(line, flush=True)
        new, _lines = login_decision(watch, load_state(), config_url, save_unverified, confirm,
                                     say=lambda ln: print(ln, flush=True))
        if new is not None:
            try:
                save_state(new)
            except OSError as exc:
                print(f"ERROR: could not write {STATE} ({exc}) - nothing was saved", flush=True)
                raise
    finally:
        player.vnc.stop()
        player.chrome.stop()
        player.pulse.stop()
        player.xvfb.stop()


def cmd_status(player: Player) -> None:
    sp = player.sp
    browser = find_browser(sp)
    tools = {t: bool(shutil.which(t)) for t in ("Xvfb", "ffmpeg", "pulseaudio", "x11vnc")}
    print(f"enabled            {sp.get('enabled')}   (when = {sp.get('when')})")
    print(f"browser            {browser or 'NOT FOUND'}   Widevine (VOYO DRM): {'yes' if has_widevine(browser) else 'NO'}")
    print("tools              " + "  ".join(f"{k}: {'ok' if v else 'MISSING'}" for k, v in tools.items()))
    print(f"stream page        {player.stream_url() or 'NOT SET (run login)'}")
    print(f"profile            {player.profile}  ({'exists' if player.profile.exists() else 'new - sign in first'})")
    print(f"dashboard server   {player.server}")
    code, st, problem = get_json(f"{player.server}/api/voyo/recordings", 3)
    if code is None:
        print(f"recordings         dashboard server not reachable ({problem})")
    elif code in (401, 403):
        print(f"recordings         dashboard server reachable - the details need a /disk login ({problem})")
    elif code == 200 and isinstance(st, dict):
        rs = st.get("server_player") or st.get("status") or {}
        print(f"recordings         {rs.get('path')}  ok={rs.get('ok')} {rs.get('error') or ''}")
    else:
        print(f"recordings         dashboard server answered {problem or f'HTTP {code}'}")
    print(f"recorder           {recorder_line(player.server)}")
    for w in [w for w in Schedule(sp).windows(time.time()) if w["open_until"] > time.time()][:5]:
        print(f"next               {w['meeting']} {w['session_name']}: "
              f"{datetime.fromtimestamp(w['open_from'], timezone.utc):%a %d %b %H:%M} - "
              f"{datetime.fromtimestamp(w['open_until'], timezone.utc):%H:%M} UTC")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("command", choices=["run", "login", "test", "status"])
    ap.add_argument("--minutes", type=float, default=5.0, help="test: how long")
    ap.add_argument("--url", help="test: this page instead of stream_url")
    ap.add_argument("--save-unverified", action="store_true",
                    help="login: save the open page even if no usable video player was confirmed on it")
    args = ap.parse_args()
    player = Player(load_config())
    if args.command == "run":
        cmd_run(player)
    elif args.command == "login":
        cmd_login(player, save_unverified=args.save_unverified)
    elif args.command == "test":
        cmd_test(player, args.minutes, args.url)
    else:
        cmd_status(player)


if __name__ == "__main__":
    main()
