"""WebSocket fan-out with per-client queues and state diffing."""
from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any, Optional

from starlette.websockets import WebSocket

log = logging.getLogger("hub")

SECTIONS = ("session", "track_status", "weather", "order", "race_control", "radio", "availability", "timeline",
            "map")
LOSSY = {"pos", "tel"}          # may be dropped for slow clients
# phone remotes (``/ws?client=remote``) get only what a remote needs - never positions, telemetry,
# the full board, the track or diagnostics; sync at most once a second
REMOTE_TYPES = {"hello", "ui", "mode", "remote", "sync", "sync_result"}
REMOTE_SYNC_S = 1.0


_bad_types: set = set()


def _fallback(o: Any) -> Any:
    """A value JSON cannot hold (a bug upstream): sent as text instead of breaking every
    dashboard connection, and reported once per type with where it came from."""
    t = type(o).__name__
    if t not in _bad_types:
        _bad_types.add(t)
        log.error("Value of type %s in a dashboard message is not JSON - sent as text: %.200r", t, o)
    if isinstance(o, (set, frozenset, tuple)):
        return list(o)
    return str(o)


def _dumps(obj: Any) -> str:
    return json.dumps(obj, separators=(",", ":"), ensure_ascii=False, default=_fallback)


class Client:
    def __init__(self, ws: WebSocket, addr: str, kind: str = "dashboard") -> None:
        self.ws = ws
        self.addr = addr
        self.kind = kind                         # "dashboard" | "remote" (phone)
        self.device_id: Optional[str] = None    # /remote: the device identity (server/security.py)
        self.last_sync = 0.0
        self.queue: asyncio.Queue[str] = asyncio.Queue(maxsize=400)
        self.task: Optional[asyncio.Task] = None
        self.needs_resync = False

    async def writer(self) -> None:
        try:
            while True:
                msg = await self.queue.get()
                await self.ws.send_text(msg)
        except Exception:  # noqa: BLE001 - client went away
            pass


class Hub:
    def __init__(self) -> None:
        self.clients: set[Client] = set()
        self.state: dict[str, Any] = {}
        self._last: dict[str, str] = {}
        self.track: Optional[dict] = None
        self.status: dict[str, Any] = {}
        self.ui: dict[str, Any] = {}
        self.hello: dict[str, Any] = {}
        self.video: dict[str, Any] = {}
        self.pit_debug: Optional[dict] = None     # pit-lane reconstruction details (debug overlay)
        self.sync: dict[str, Any] = {}
        self.clock: dict[str, Any] = {}
        self.mode: dict[str, Any] = {}            # LIVE / VOD mode selector state (server/mode.py)
        self.remote: dict[str, Any] = {}          # compact state for phone remotes (built from the board state)

    # ---- client management ---------------------------------------------
    async def add(self, ws: WebSocket, addr: str, kind: str = "dashboard") -> Client:
        c = Client(ws, addr, kind)
        self.clients.add(c)
        c.task = asyncio.create_task(c.writer())
        log.info("%s connected: %s (%d total)", "Phone remote" if kind == "remote" else "Dashboard client", addr,
                 len(self.clients))
        if kind == "remote":
            hello = {"type": "hello", "mode": self.hello.get("mode"), "remote": True,
                     "config": {"remote_enabled": (self.hello.get("config") or {}).get("remote_enabled", True)}}
            for msg in (hello, self.ui, self.mode, self.remote or self._remote_summary(), self.sync):
                if msg:
                    self._send(c, _dumps(msg))
            return c
        initial = [("hello", self.hello), ("state", self._full_state()),
                   ("track", {"type": "track", "track": self.track} if self.track else None),
                   ("status", self.status), ("video", self.video), ("pit_debug", self.pit_debug),
                   ("ui", self.ui), ("clock", self.clock), ("sync", self.sync), ("mode", self.mode)]
        for name, msg in initial:
            if not msg:
                continue
            try:
                self._send(c, _dumps(msg))
            except Exception:  # noqa: BLE001 - one broken message must never refuse the connection
                log.exception("Could not send the initial '%s' message to %s (skipped)", name, addr)
        return c

    def remove(self, c: Client) -> None:
        if c in self.clients:
            self.clients.discard(c)
            if c.task:
                c.task.cancel()
            log.info("Dashboard client disconnected: %s (%d left)", c.addr, len(self.clients))

    def _send(self, c: Client, text: str, lossy: bool = False) -> None:
        if c.queue.full():
            if lossy or c.kind == "remote":
                return
            # client is too slow: drop everything queued and resync with a full state
            while not c.queue.empty():
                c.queue.get_nowait()
            c.needs_resync = True
            text = _dumps(self._full_state())
        c.queue.put_nowait(text)

    def broadcast(self, msg: dict) -> None:
        if msg.get("type") == "clock":
            self.clock = msg
        if not self.clients:
            return
        try:
            text = _dumps(msg)
        except Exception:  # noqa: BLE001
            log.exception("Message '%s' cannot be sent to the dashboards (dropped)", msg.get("type"))
            return
        kind = msg.get("type")
        lossy = kind in LOSSY
        now = None
        for c in list(self.clients):
            if c.kind == "remote":
                if kind not in REMOTE_TYPES:
                    continue
                if kind == "sync":
                    now = now or time.monotonic()
                    if now - c.last_sync < REMOTE_SYNC_S:
                        continue
                    c.last_sync = now
            if lossy and c.queue.qsize() > 150:
                continue
            self._send(c, text, lossy)

    # ---- state -------------------------------------------------------------
    def _full_state(self) -> dict:
        return {"type": "state", "full": True, **self.state}

    def publish_state(self, state: dict[str, Any], force_full: bool = False) -> None:
        patch: dict[str, Any] = {"type": "state", "full": False}
        changed = False
        for key in SECTIONS:
            try:
                s = _dumps(state.get(key))
            except Exception:  # noqa: BLE001 - e.g. a circular structure: keep the last good section
                log.exception("State section '%s' cannot be sent (kept the previous one)", key)
                state[key] = (self.state or {}).get(key)
                continue
            if self._last.get(key) != s:
                self._last[key] = s
                patch[key] = state.get(key)
                changed = True
        drivers = state.get("drivers") or {}
        dpatch = {}
        for num, d in list(drivers.items()):
            try:
                s = _dumps(d)
            except Exception:  # noqa: BLE001
                log.exception("Driver %s cannot be sent (kept the previous state)", num)
                drivers[num] = ((self.state or {}).get("drivers") or {}).get(num) or {}
                continue
            if self._last.get(f"d:{num}") != s:
                self._last[f"d:{num}"] = s
                dpatch[num] = d
        removed = [k[2:] for k in list(self._last) if k.startswith("d:") and k[2:] not in drivers]
        for num in removed:
            self._last.pop(f"d:{num}", None)
        self.state = state                       # (only sendable content from here on)
        if dpatch or removed:
            patch["drivers"] = dpatch
            patch["removed"] = removed
            changed = True
        if force_full:
            self.broadcast(self._full_state())
        elif changed:
            self.broadcast(patch)
        if changed or force_full:
            self._remote_update()

    # ---- phone remote: a small summary of the board state (the board state stays the source) ----
    def _remote_summary(self) -> dict:
        st = self.state or {}
        s, ts, drivers = st.get("session") or {}, st.get("track_status") or {}, st.get("drivers") or {}
        sel = (self.ui or {}).get("selected")
        d = drivers.get(sel) or {}
        order = [[n, (drivers.get(n) or {}).get("tla") or n, (drivers.get(n) or {}).get("position")]
                 for n in (st.get("order") or [])]
        return {"type": "remote",
                "session": {k: s.get(k) for k in ("meeting_name", "session_name", "session_kind", "lap", "total_laps",
                                                  "live", "phase", "status")},
                "flag": {k: ts.get(k) for k in ("status", "state", "sc_phase", "pit_exit")},
                "selected": {"num": sel, "tla": d.get("tla"), "position": d.get("position"),
                             "team_color": d.get("team_color")} if sel else None,
                "order": order}

    def _remote_update(self) -> None:
        if not any(c.kind == "remote" for c in self.clients):
            self.remote = {}
            return
        r = self._remote_summary()
        if r != self.remote:
            self.remote = r
            self.broadcast(r)

    def reset_diff(self) -> None:
        self._last.clear()

    def set_track(self, track: Optional[dict]) -> None:
        self.track = track
        self.broadcast({"type": "track", "track": track})

    def set_status(self, status: dict) -> None:
        self.status = {"type": "status", **status}
        self.broadcast(self.status)

    def set_pit_debug(self, data: Optional[dict]) -> None:
        self.pit_debug = {"type": "pitlane_debug", **(data or {})}
        self.broadcast(self.pit_debug)

    def set_video(self, video: dict) -> None:
        if video != self.video:
            self.video = video
            self.broadcast(video)

    def set_ui(self, ui: dict) -> None:
        self.ui = ui
        self.broadcast(ui)
        self._remote_update()                    # the selected driver is part of the remote summary

    def set_mode(self, mode: dict) -> None:
        self.mode = mode
        self.broadcast(mode)

    def reset_data(self, hello: dict) -> None:
        """The data source was switched (LIVE <-> VOD): forget everything of the old one and tell
        the dashboards to start over (new hello, empty state, no track)."""
        self.state = {}
        self._last.clear()
        self.track = None
        self.status = {}
        self.pit_debug = None
        self.sync = {}
        self.clock = {}
        self.hello = hello
        self.broadcast(hello)
        self.broadcast(self._full_state())
        self.broadcast({"type": "track", "track": None})

    def set_sync(self, sync: dict) -> None:
        self.sync = sync
        self.broadcast(sync)
