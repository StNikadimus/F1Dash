"""VOYO video layer - availability monitor.

The video layer is completely separate from the F1 data pipeline: it only
decides whether a video slot is shown (RACE_VIEW / VIDEO_FOCUS) and tells the
browser which *official* player to show. It never touches, proxies, records or
re-streams video.

Modes (config ``[voyo] mode``):

* ``window`` - VOYO's own website/player runs in its own browser window that
  ``tools/tv_launcher.py`` places exactly over the dashboard's video slot.
  Always possible, uses VOYO's official player, login and DRM unchanged. The
  server's reachability check is informational only in this mode: it never
  turns the video layer off (which would push the VOYO window away).
* ``embed``  - iframe of ``voyo.url``. Only used if the page does not forbid
  framing (X-Frame-Options / CSP frame-ancestors are checked, never bypassed).
* ``hls``    - ``<video>`` element for a stream URL that the provider officially
  makes available for external players (``voyo.hls_url``). VOYO publishes no
  such URL; the mode exists for a future official offer / other providers.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Optional

import httpx

log = logging.getLogger("video")

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/140.0 Safari/537.36 f1-tv-dashboard")


def framing_allowed(headers: httpx.Headers) -> tuple[Optional[bool], str]:
    """Evaluate the response headers that control iframe embedding."""
    xfo = (headers.get("x-frame-options") or "").strip().upper()
    csp = headers.get("content-security-policy") or ""
    fa = None
    for part in csp.split(";"):
        p = part.strip()
        if p.lower().startswith("frame-ancestors"):
            fa = p[len("frame-ancestors"):].strip()
    if fa is not None:
        tokens = fa.split()
        if "*" in tokens:
            return True, f"CSP frame-ancestors {fa}"
        shown = fa or "'none'"
        return False, f"CSP frame-ancestors {shown}"
    if xfo in ("DENY", "SAMEORIGIN") or xfo.startswith("ALLOW-FROM"):
        return False, f"X-Frame-Options: {xfo}"
    return True, "no framing restriction headers"


class VideoMonitor:
    def __init__(self, cfg: dict[str, Any], remote, hub) -> None:
        self.cfg = cfg
        self.remote = remote
        self.hub = hub
        self.enabled = bool(cfg.get("enabled", False))
        self.mode = str(cfg.get("mode", "window")).lower()
        self.url = str(cfg.get("url") or "")
        self.hls_url = str(cfg.get("hls_url") or "")
        self.check = bool(cfg.get("check_reachability", True))
        self.interval = max(15, int(cfg.get("check_interval_seconds", 60)))
        self.fallback = bool(cfg.get("fallback_when_unreachable", True))
        self.info: dict[str, Any] = {}

    def _publish(self, **info: Any) -> None:
        self.info = {
            "type": "video", "enabled": self.enabled, "mode": self.mode,
            "url": self.url if self.mode in ("embed", "window") else None,
            "hls_url": self.hls_url if self.mode == "hls" else None,
            "checked": time.strftime("%H:%M:%S"), **info,
        }
        self.hub.set_video(self.info)

    async def run(self) -> None:
        if not self.enabled or self.mode not in ("window", "embed", "hls"):
            self._publish(reachable=None, embeddable=None, notice="VOYO video layer disabled (config [voyo])")
            await self.remote.set_video_available(False, None)
            return
        target = self.hls_url if self.mode == "hls" else self.url
        if not target:
            self._publish(reachable=None, embeddable=None, notice="No VOYO URL configured")
            await self.remote.set_video_available(False, "No VOYO URL configured")
            return
        last: Optional[tuple] = None
        async with httpx.AsyncClient(timeout=12, follow_redirects=True,
                                     headers={"User-Agent": UA}) as http:
            while True:
                reachable: Optional[bool] = None
                embeddable: Optional[bool] = None
                detail = ""
                if self.check or self.mode == "embed":
                    try:
                        r = await http.get(target)
                        reachable = r.status_code < 500
                        detail = f"HTTP {r.status_code}"
                        if self.mode == "embed":
                            embeddable, why = framing_allowed(r.headers)
                            detail += f", {why}"
                    except httpx.HTTPError as exc:
                        reachable = False
                        detail = f"{type(exc).__name__}"
                available, notice = True, None
                if self.mode == "embed" and embeddable is False:
                    available = False
                    notice = f"VOYO does not allow embedding ({detail}). Use [voyo] mode = \"window\"."
                elif reachable is False and self.fallback and self.mode != "window":
                    available = False
                    notice = f"VOYO not reachable ({detail}) - showing full dashboard"
                elif reachable is False:
                    # window mode: the official player runs in its own browser window with the
                    # user's login, cookies and network. This server-side request (no browser, no
                    # cookies) can be refused by bot protection or time out while that window
                    # plays fine - so it only informs and never hides the VOYO window.
                    notice = f"server-side VOYO check failed ({detail}) - VOYO window kept (informational)"
                state = (available, notice, reachable, embeddable)
                if state != last:
                    last = state
                    log.info("VOYO %s check: %s -> %s", self.mode, detail or "skipped",
                             "video layer ON" if available else f"video layer OFF ({notice})")
                self._publish(reachable=reachable, embeddable=embeddable, notice=notice, detail=detail)
                await self.remote.set_video_available(available, notice)
                await asyncio.sleep(self.interval)
