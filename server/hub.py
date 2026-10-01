"""WebSocket fan-out with per-client queues and state diffing."""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Optional

from starlette.websockets import WebSocket

log = logging.getLogger("hub")

SECTIONS = ("session", "track_status", "weather", "order", "race_control", "radio", "availability", "timeline")
LOSSY = {"pos", "tel"}          # may be dropped for slow clients


def _dumps(obj: Any) -> str:
    return json.dumps(obj, separators=(",", ":"), ensure_ascii=False)


class Client:
    def __init__(self, ws: WebSocket, addr: str) -> None:
        self.ws = ws
        self.addr = addr
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

    # ---- client management ---------------------------------------------
    async def add(self, ws: WebSocket, addr: str) -> Client:
        c = Client(ws, addr)
        self.clients.add(c)
        c.task = asyncio.create_task(c.writer())
        log.info("Dashboard client connected: %s (%d total)", addr, len(self.clients))
        self._send(c, _dumps(self.hello))
        self._send(c, _dumps(self._full_state()))
        if self.track:
            self._send(c, _dumps({"type": "track", "track": self.track}))
        if self.status:
            self._send(c, _dumps(self.status))
        if self.video:
            self._send(c, _dumps(self.video))
        if self.pit_debug:
            self._send(c, _dumps(self.pit_debug))
        if self.ui:
            self._send(c, _dumps(self.ui))
        if self.clock:
            self._send(c, _dumps(self.clock))
        if self.sync:
            self._send(c, _dumps(self.sync))
        return c

    def remove(self, c: Client) -> None:
        if c in self.clients:
            self.clients.discard(c)
            if c.task:
                c.task.cancel()
            log.info("Dashboard client disconnected: %s (%d left)", c.addr, len(self.clients))

    def _send(self, c: Client, text: str, lossy: bool = False) -> None:
        if c.queue.full():
            if lossy:
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
        text = _dumps(msg)
        lossy = msg.get("type") in LOSSY
        for c in list(self.clients):
            if lossy and c.queue.qsize() > 150:
                continue
            self._send(c, text, lossy)

    # ---- state -------------------------------------------------------------
    def _full_state(self) -> dict:
        return {"type": "state", "full": True, **self.state}

    def publish_state(self, state: dict[str, Any], force_full: bool = False) -> None:
        self.state = state
        patch: dict[str, Any] = {"type": "state", "full": False}
        changed = False
        for key in SECTIONS:
            s = _dumps(state.get(key))
            if self._last.get(key) != s:
                self._last[key] = s
                patch[key] = state.get(key)
                changed = True
        drivers = state.get("drivers") or {}
        dpatch = {}
        for num, d in drivers.items():
            s = _dumps(d)
            if self._last.get(f"d:{num}") != s:
                self._last[f"d:{num}"] = s
                dpatch[num] = d
        removed = [k[2:] for k in list(self._last) if k.startswith("d:") and k[2:] not in drivers]
        for num in removed:
            self._last.pop(f"d:{num}", None)
        if dpatch or removed:
            patch["drivers"] = dpatch
            patch["removed"] = removed
            changed = True
        if force_full:
            self.broadcast(self._full_state())
        elif changed:
            self.broadcast(patch)

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

    def set_sync(self, sync: dict) -> None:
        self.sync = sync
        self.broadcast(sync)
