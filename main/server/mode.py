"""LIVE / VOD mode controller: AUTO detection + manual override from the dashboard.

    selected_mode   what the user chose:  AUTO | LIVE | VOD
    detected_mode   what the automatic detection says: LIVE (an F1 session is on now, or about
                    to start) | VOD (no session is on - VOYO shows a recording)
    effective_mode  what the server actually runs:  selected unless AUTO, else detected

The detection is the one tools/tv_launcher.py used to pick the mode at start (the official F1
season schedule, ``livetiming.formula1.com/static/<year>/Index.json``: a session counts as "on"
from 90 min before its start until 60 min after its end), now done by the running server and
repeated every minute (the schedule is re-read every 30 min). While the live feed is running,
its own SessionStatus (Started / Finished ...) is used as well. A manual LIVE / VOD selection
overrides the detection until AUTO is selected again; the detection keeps running, so the
dashboard always shows what it says.

``test`` / ``replay`` (started with --test / --replay) are developer sources: AUTO then means
"the source the server was started with" (detected = TEST / REPLAY); LIVE / VOD still switch.

Switching never restarts the process: the controller calls ``switch(mode)`` (server/app.py),
which stops the current data source + engine and starts the other one in place.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Optional

import httpx

log = logging.getLogger("mode")

SELECTABLE = ("AUTO", "LIVE", "VOD")
LIVE_WINDOW_BEFORE_S = 90 * 60      # a session "is on" from 90 min before its start ...
LIVE_WINDOW_AFTER_S = 60 * 60       # ... until 60 min after its scheduled end
INDEX_URL = "https://livetiming.formula1.com/static/{year}/Index.json"
INDEX_MAX_AGE_S = 30 * 60
DETECT_EVERY_S = 60.0
CYCLE_SETTLE_S = 1.5                # remote "next mode": applied after this pause without another press
LIVE_STATES = {"Started", "Aborted", "Inactive"}      # feed SessionStatus while a session is running/held


def detect_from_schedule(index: Optional[dict], now: Optional[float] = None) -> tuple[str, Optional[dict], str]:
    """-> (detected LIVE|VOD, the session on now or None, reason). ``index`` None: unknown schedule."""
    from .sources.f1_live import _local_to_utc
    now_s = now if now is not None else time.time()
    if index is None:
        return "VOD", None, "F1 schedule not reachable - assuming a recording"
    for m in index.get("Meetings") or []:
        for s in m.get("Sessions") or []:
            start = _local_to_utc(s.get("StartDate"), s.get("GmtOffset"))
            end = _local_to_utc(s.get("EndDate"), s.get("GmtOffset")) or start
            if start and start.timestamp() - LIVE_WINDOW_BEFORE_S <= now_s <= end.timestamp() + LIVE_WINDOW_AFTER_S:
                sess = {"name": f"{m.get('Name')} {s.get('Name')}", "start": start.isoformat(),
                        "end": end.isoformat() if end else None,
                        "running": start.timestamp() <= now_s <= end.timestamp()}
                when = "is on now" if sess["running"] else (
                    "starts soon" if now_s < start.timestamp() else "just ended")
                return "LIVE", sess, f"{sess['name']} {when}"
    return "VOD", None, "no F1 session is on now - VOYO shows a recording"


async def fetch_index(year: int) -> dict:
    async with httpx.AsyncClient(timeout=15, headers={"User-Agent": "f1-tv-dashboard/1.0"},
                                 follow_redirects=True) as http:
        r = await http.get(INDEX_URL.format(year=year))
    if r.status_code != 200:
        raise RuntimeError(f"HTTP {r.status_code}")
    return json.loads(r.content.decode("utf-8-sig"))


class ModeController:
    def __init__(self, selected: str = "AUTO", fixed: Optional[str] = None,
                 switch: Optional[Callable[[str], Awaitable[None]]] = None,
                 publish: Optional[Callable[[dict], None]] = None,
                 index_loader: Optional[Callable[[int], Awaitable[dict]]] = None,
                 feed_status: Optional[Callable[[], Optional[str]]] = None,
                 clock: Callable[[], float] = time.time) -> None:
        sel = str(selected or "AUTO").upper()
        self.selected = sel if sel in SELECTABLE else "AUTO"
        self.fixed = fixed.upper() if fixed else None         # TEST / REPLAY start source (AUTO = it)
        self.detected = "VOD"
        self.detected_reason = "detecting…"
        self.detected_known = False
        self.live_session: Optional[dict] = None
        self.feed_live: Optional[bool] = None                 # the live feed's own SessionStatus says running
        self.running: Optional[str] = None                    # source mode currently running (live/vod/test/replay)
        self.switching = False
        self.last_switch: Optional[str] = None
        self.error: Optional[str] = None
        self._switch = switch
        self._publish = publish
        self._load_index = index_loader or fetch_index
        self._clock = clock
        self._feed_status = feed_status                       # SessionStatus of the running live feed
        self._index: Optional[dict] = None
        self._index_at = -1e18
        self._index_year: Optional[int] = None
        self._lock = asyncio.Lock()
        self._pending: Optional[asyncio.Task] = None

    # ------------------------------------------------------------------ state
    @property
    def detected_mode(self) -> str:
        return self.fixed or self.detected

    @property
    def effective(self) -> str:
        return self.selected if self.selected != "AUTO" else self.detected_mode

    def wanted_source(self) -> str:
        """Source mode the effective mode runs (``live`` / ``vod`` / ``test`` / ``replay``)."""
        return self.effective.lower()

    def state(self) -> dict[str, Any]:
        eff = self.effective
        manual = self.selected != "AUTO"
        no_live = eff == "LIVE" and self.fixed is None and not self.live_session and self.feed_live is not True
        return {
            "type": "mode",
            "selected_mode": self.selected,
            "detected_mode": self.detected_mode,
            "effective_mode": eff,
            "manual": manual,
            "label": f"{eff} (MANUAL)" if manual else f"AUTO · {eff}",
            "detected_reason": self.fixed and f"started as {self.fixed}" or self.detected_reason,
            "detected_known": self.detected_known or self.fixed is not None,
            "live_session": self.live_session,
            "feed_live": self.feed_live,
            "notice": "No active live F1 session" if no_live else None,
            "running": self.running,
            "switching": self.switching,
            "error": self.error,
            "options": list(SELECTABLE),
        }

    def _emit(self) -> None:
        if self._publish:
            self._publish(self.state())

    # ------------------------------------------------------------------ detection
    async def detect(self) -> None:
        """Re-read the schedule when due and recompute detected_mode (never raises)."""
        now = self._clock()
        year = datetime.fromtimestamp(now, timezone.utc).year
        if self._index is None or now - self._index_at > INDEX_MAX_AGE_S or year != self._index_year:
            try:
                self._index = await self._load_index(year)
                self._index_year = year
                self._index_at = now
            except Exception as exc:  # noqa: BLE001
                if self._index is None:
                    self.detected_reason = f"F1 schedule not reachable ({type(exc).__name__}) - assuming a recording"
                self._index_at = now - INDEX_MAX_AGE_S + 300     # retry in 5 min
                log.info("Mode detection: F1 schedule %s not readable (%s)", year, exc)
        if self._index is not None:
            det, sess, why = detect_from_schedule(self._index, now)
            self.detected_known = True
        else:
            det, sess, why = "VOD", None, self.detected_reason
        if self._feed_status is not None:
            try:
                st = self._feed_status()
            except Exception:  # noqa: BLE001
                st = None
            self.feed_live = (st in LIVE_STATES) if st else None
        if self.feed_live:
            det, why = "LIVE", "the live F1 feed reports a running session"
            sess = sess or {"name": "live session", "running": True}
        if (det, why) != (self.detected, self.detected_reason) or sess != self.live_session:
            if det != self.detected:
                log.info("Mode detection: %s (%s)", det, why)
            self.detected, self.detected_reason, self.live_session = det, why, sess
            self._emit()

    # ------------------------------------------------------------------ selection
    async def select(self, mode: str) -> str:
        """Dashboard / remote / API: AUTO, LIVE or VOD (NEXT = the next one, for a remote key).
        Returns a short text for a toast. A NEXT press is applied after a short pause so that
        stepping AUTO -> LIVE -> VOD with the remote does not start every source on the way."""
        m = str(mode or "").upper()
        if m == "NEXT":
            m = SELECTABLE[(SELECTABLE.index(self.selected) + 1) % len(SELECTABLE)]
            self.selected = m
            self._emit()
            if self._pending is not None and not self._pending.done():
                self._pending.cancel()
            self._pending = asyncio.get_event_loop().create_task(self._apply_later(CYCLE_SETTLE_S))
            return f"Mode {m}" + (" (MANUAL)" if m != "AUTO" else f" · detected {self.detected_mode}")
        if m not in SELECTABLE:
            return f"Unknown mode {mode!r}"
        if self._pending is not None and not self._pending.done():
            self._pending.cancel()
        before = self.effective
        self.selected = m
        log.info("Mode selected: %s (detected %s -> effective %s)", m, self.detected_mode, self.effective)
        await self.apply()
        return self._result_text(m, before)

    def _result_text(self, m: str, before: str) -> str:
        eff = self.effective
        if self.error:
            return f"Mode {m}: {self.error}"
        if m == "AUTO":
            return f"Mode AUTO · detected {self.detected_mode}" + ("" if eff == before else f" → {eff}")
        text = f"Mode {m} (MANUAL)"
        if self.state()["notice"]:
            text += " · no active live F1 session"
        return text

    async def _apply_later(self, delay: float) -> None:
        await asyncio.sleep(delay)
        log.info("Mode selected: %s (detected %s -> effective %s)", self.selected, self.detected_mode,
                 self.effective)
        await self.apply()

    async def apply(self) -> None:
        """Run the source of the effective mode (switch when it differs)."""
        async with self._lock:
            want = self.wanted_source()
            if want != self.running and self._switch is not None:
                self.switching = True
                self._emit()
                try:
                    await self._switch(want)
                    self.running = want
                    self.error = None
                    self.last_switch = time.strftime("%H:%M:%S")
                    self.feed_live = None
                except Exception as exc:  # noqa: BLE001 - keep the dashboard alive, say why
                    log.exception("Switching to %s failed", want.upper())
                    self.error = f"switch to {want.upper()} failed: {exc}"
                finally:
                    self.switching = False
            self._emit()

    async def run(self) -> None:
        """Keep detecting; in AUTO follow the detection."""
        while True:
            try:
                await self.detect()
                if self.selected == "AUTO" and self.wanted_source() != self.running:
                    log.info("AUTO mode: detection changed to %s - switching", self.detected_mode)
                    await self.apply()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                log.exception("Mode detection failed")
            await asyncio.sleep(DETECT_EVERY_S)
