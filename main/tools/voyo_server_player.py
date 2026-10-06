#!/usr/bin/env python3
"""SERVER VOYO PLAYER - the server opens VOYO itself and records it (no PC needed).

    python tools/voyo_server_player.py login     # once: sign in to VOYO (VNC through an SSH tunnel)
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


def log(msg: str) -> None:
    print(time.strftime("%Y-%m-%d %H:%M:%S ") + msg, flush=True)


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

    def evaluate(self, expr: str) -> Optional[dict]:
        from websockets.sync.client import connect
        try:
            pages = self._targets()
        except OSError:
            return None
        if not pages:
            return None
        with connect(pages[0]["webSocketDebuggerUrl"], open_timeout=3, max_size=2 ** 22) as ws:
            ws.send(json.dumps({"id": 1, "method": "Runtime.evaluate",
                                "params": {"expression": expr, "returnByValue": True, "userGesture": True,
                                           "awaitPromise": False}}))
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                msg = json.loads(ws.recv(timeout=5))
                if msg.get("id") == 1:
                    return ((msg.get("result") or {}).get("result") or {}).get("value")
        return None

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
            if time.monotonic() - self.last_play > 120:
                self.last_play = time.monotonic()
                log(f"no video on the page yet ({st.get('title')!r}, {st.get('url')}) - signed in? the F1 stream "
                    "page? (voyo_server_player.py login)")
            return
        if st.get("did"):
            log(f"player: {', '.join(st['did'])} - position {st.get('t', 0):.0f} s, "
                f"{'paused' if st.get('paused') else 'playing'}")

    # ------------------------------------------------------------------ open / close
    def stream_url(self) -> Optional[str]:
        return str(self.sp.get("stream_url") or "") or load_state().get("last_url")

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
                                         log=log)
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
# commands
# ---------------------------------------------------------------------------------------------
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
            for _ in range(15):
                if stop["now"]:
                    break
                time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        if player.is_open:
            player.close("stopped")


def cmd_test(player: Player, minutes: float, url: Optional[str]) -> None:
    url = url or player.stream_url()
    if not url:
        raise SystemExit("no stream_url - pass --url or run login first")
    player.open(url, {"meeting": "TEST", "session_name": "Test recording"})
    end = time.monotonic() + minutes * 60
    try:
        while time.monotonic() < end:
            player.tick()
            time.sleep(5)
    except KeyboardInterrupt:
        pass
    finally:
        player.close("test finished")
    try:
        with urllib.request.urlopen(f"{player.server}/api/voyo/recordings", timeout=5) as r:
            recs = [x for x in json.loads(r.read())["recordings"] if x.get("channel") == "server_player"][:1]
        for x in recs:
            log(f"recording {x['stream_instance_id']}: {x['status']}, {x['watched_seconds']} s, "
                f"{x['capture_segments']} video segment(s), {x['capture_bytes'] / 1e6:.1f} MB")
    except (OSError, ValueError, KeyError):
        log("dashboard server not reachable - is it running? (the recordings are written by it)")


def cmd_login(player: Player) -> None:
    """Chrome on the virtual screen + VNC (127.0.0.1 only) so you can sign in to VOYO once."""
    if not shutil.which("x11vnc"):
        raise SystemExit("x11vnc is not installed (server/setup-voyo-player.sh)")
    url = str(player.sp.get("stream_url") or "") or load_state().get("last_url") or "https://voyo.si/"
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
  3. sign in to VOYO, open the F1 live stream page (the one you want recorded) - it may play
  4. press Ctrl+C here: the page that is open then becomes the stream page (unless
     [voyo.server_player] stream_url is set)
""", flush=True)
    last = None
    try:
        while True:
            u = player.page_url()
            if u and u.startswith("http") and u != last:
                last = u
                print(f"  open page: {u}", flush=True)
            time.sleep(3)
    except KeyboardInterrupt:
        pass
    finally:
        player.vnc.stop()
        player.chrome.stop()
        player.pulse.stop()
        player.xvfb.stop()
    if last:
        st = load_state()
        st.update(last_url=last, learned_at=datetime.now(timezone.utc).isoformat())
        save_state(st)
        print(f"Saved as the stream page: {last}")


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
    try:
        with urllib.request.urlopen(f"{player.server}/api/voyo/recordings", timeout=3) as r:
            st = json.loads(r.read())
        rs = st.get("server_player") or st.get("status") or {}
        print(f"recordings         {rs.get('path')}  ok={rs.get('ok')} {rs.get('error') or ''}")
    except (OSError, ValueError):
        print("recordings         dashboard server not reachable")
    for w in [w for w in Schedule(sp).windows(time.time()) if w["open_until"] > time.time()][:5]:
        print(f"next               {w['meeting']} {w['session_name']}: "
              f"{datetime.fromtimestamp(w['open_from'], timezone.utc):%a %d %b %H:%M} - "
              f"{datetime.fromtimestamp(w['open_until'], timezone.utc):%H:%M} UTC")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("command", choices=["run", "login", "test", "status"])
    ap.add_argument("--minutes", type=float, default=5.0, help="test: how long")
    ap.add_argument("--url", help="test: this page instead of stream_url")
    args = ap.parse_args()
    player = Player(load_config())
    if args.command == "run":
        cmd_run(player)
    elif args.command == "login":
        cmd_login(player)
    elif args.command == "test":
        cmd_test(player, args.minutes, args.url)
    else:
        cmd_status(player)


if __name__ == "__main__":
    main()
