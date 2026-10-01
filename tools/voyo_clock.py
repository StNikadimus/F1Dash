#!/usr/bin/env python3
"""VOYO playback clock bridge.

Reads ``HTMLVideoElement.currentTime`` (+ paused, playbackRate, readyState,
seeking, buffered, seekable, media events) of the official VOYO player and
posts it to the dashboard server (``POST /api/sync/voyo``) 5x per second.

How: the VOYO window is started by tools/tv_launcher.py with its own browser
profile and ``--remote-debugging-port=9223``. Chrome/Edge serve that DevTools
port on 127.0.0.1 only. This bridge evaluates tools/voyo_clock_probe.js in the
VOYO page (``Runtime.evaluate``) - a read-only snippet, see that file. No
browser extension, no user script, no DOM scraping of the page layout, no
access to the protected stream.

Security note: a DevTools port gives full control over *that* browser profile
to programs running on this computer (not to the network). The profile is the
dedicated VOYO one (data/browser-profiles/voyo). Disable with
``tv_launcher.py --no-clock`` if you do not want it.

Standalone (VOYO already open with the debug port):
    python tools/voyo_clock.py --server http://127.0.0.1:8080 --port 9223
"""
from __future__ import annotations

import argparse
import json
import threading
import time
import urllib.request
from pathlib import Path
from typing import Callable, Optional

PROBE = (Path(__file__).resolve().parent / "voyo_clock_probe.js").read_text(encoding="utf-8")


class VoyoClockBridge:
    def __init__(self, server: str, port: int = 9223, token: str = "", hz: float = 5.0,
                 match: str = "voyo", log: Callable[[str], None] = print) -> None:
        self.server = server.rstrip("/").replace("://localhost", "://127.0.0.1")
        self.port = int(port)
        self.token = token
        self.period = 1.0 / max(1.0, min(10.0, float(hz)))
        self.match = match.lower()
        self.log = log
        self.stop = threading.Event()
        self.state = "starting"
        self._said: set[str] = set()
        self._msg_id = 0
        self._last_post = 0.0
        self._last_sent: Optional[tuple] = None
        self.thread: Optional[threading.Thread] = None
        self.page_title: Optional[str] = None     # current title of the VOYO page (to find its window)

    # ------------------------------------------------------------------
    def start(self) -> "VoyoClockBridge":
        self.thread = threading.Thread(target=self.run, name="voyo-clock", daemon=True)
        self.thread.start()
        return self

    def _once(self, key: str, text: str) -> None:
        if key not in self._said:
            self._said.add(key)
            self.log(text)

    def _find_page(self) -> Optional[str]:
        with urllib.request.urlopen(f"http://127.0.0.1:{self.port}/json/list", timeout=2) as r:
            targets = json.loads(r.read())
        pages = [t for t in targets if t.get("type") == "page" and t.get("webSocketDebuggerUrl")]
        for t in pages:
            if self.match in (t.get("url") or "").lower():
                self.page_title = t.get("title") or self.page_title
                return t["webSocketDebuggerUrl"]
        return None

    def _evaluate(self, ws) -> Optional[dict]:
        self._msg_id += 1
        mid = self._msg_id
        ws.send(json.dumps({"id": mid, "method": "Runtime.evaluate",
                            "params": {"expression": PROBE, "returnByValue": True, "silent": True}}))
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            msg = json.loads(ws.recv(timeout=3))
            if msg.get("id") != mid:
                continue                      # no domains are enabled, but ignore anything else
            res = msg.get("result") or {}
            if res.get("exceptionDetails"):
                return None
            return (res.get("result") or {}).get("value")
        return None

    def _diagnose(self, value: dict) -> None:
        """One terminal line whenever something that matters for the sync changes:
        another page / video, the length becomes known, playback starts or stops."""
        meta = value.get("meta") or {}
        page = value.get("page") or {}
        dur = value.get("duration") or meta.get("length")
        state = "paused" if value.get("paused") else "playing"
        key = (page.get("title"), page.get("media_id"), bool(dur), state, value.get("videos"))
        if key == getattr(self, "_diag", None):
            return
        self._diag = key
        length = f"{dur / 60:.0f} min" if dur else "unknown"
        self.log(f"  VOYO clock: {state} at {value['playback_time']:.1f}s, video length {length}, "
                 f"media id {'yes' if page.get('media_id') else 'NO'}, <video> elements {value.get('videos', '?')}, "
                 f"title {page.get('media_title') or page.get('title')!r}")
        if not dur and value["playback_time"] == 0 and not value.get("paused"):
            self.log("    (the player has not loaded the stream yet)")

    def _post(self, sample: dict) -> None:
        req = urllib.request.Request(self.server + "/api/sync/voyo", method="POST",
                                     data=json.dumps(sample).encode(),
                                     headers={"Content-Type": "application/json"})
        if self.token:
            req.add_header("X-Remote-Token", self.token)
        urllib.request.urlopen(req, timeout=1.5).read()

    def run(self) -> None:
        from websockets.sync.client import connect      # websockets >= 13 (requirements.txt)
        while not self.stop.is_set():
            try:
                url = self._find_page()
            except OSError:
                self.state = "no-devtools"
                self._once("port", f"  VOYO clock: DevTools port {self.port} not reachable - is the VOYO window "
                                   "started by tv_launcher.py (with the clock enabled)? Retrying.")
                self.stop.wait(3)
                continue
            if not url:
                self.state = "no-page"
                self._once("page", "  VOYO clock: waiting for a voyo page in the VOYO window")
                self.stop.wait(2)
                continue
            try:
                with connect(url, max_size=4 * 1024 * 1024, open_timeout=3) as ws:
                    self._said.discard("port")
                    self._said.discard("page")
                    self._loop(ws)
            except Exception as exc:  # noqa: BLE001 - page closed / navigated / browser gone
                self.state = "reconnecting"
                self._once(f"err:{type(exc).__name__}", f"  VOYO clock: DevTools connection lost ({exc}) - reconnecting")
                self.stop.wait(1)

    def _loop(self, ws) -> None:
        while not self.stop.is_set():
            t0 = time.monotonic()
            value = self._evaluate(ws)
            if not value or not value.get("found") or value.get("playback_time") is None:
                if self.state != "no-video":
                    self.state = "no-video"
                    self.log("  VOYO clock: no <video> on the VOYO page yet (open the F1 stream)")
                self.stop.wait(1)
                continue
            if self.state != "running":
                self.state = "running"
                self.log(f"  VOYO clock: posting the video position to {self.server}/api/sync/voyo")
            self._diagnose(value)
            value.pop("found", None)
            self.page_title = (value.get("page") or {}).get("title") or self.page_title
            moving = not value.get("paused") and not value.get("seeking")
            key = (round(value["playback_time"], 1), value.get("paused"), value.get("playback_rate"),
                   value.get("ready_state"), value.get("seeking"))
            now = time.monotonic()
            # playing: every poll (the video clock moves); paused: on change or 1 s keep-alive
            if moving or value.get("events") or key != self._last_sent or now - self._last_post >= 1.0:
                try:
                    self._post(value)
                    self._last_post, self._last_sent = now, key
                    self._said.discard("post")
                except Exception as exc:  # noqa: BLE001
                    self._once("post", f"  VOYO clock: dashboard server not reachable ({exc})")
            wait = (self.period if moving else 0.5) - (time.monotonic() - t0)
            if wait > 0:
                self.stop.wait(wait)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--server", default="http://127.0.0.1:8080")
    ap.add_argument("--port", type=int, default=9223, help="DevTools port of the VOYO browser window")
    ap.add_argument("--token", default="")
    ap.add_argument("--hz", type=float, default=5.0)
    ap.add_argument("--match", default="voyo", help="text in the URL of the page with the player")
    args = ap.parse_args()
    bridge = VoyoClockBridge(args.server, args.port, args.token, args.hz, args.match)
    try:
        bridge.run()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
