"""Remote-control abstraction.

    Remote input (WD TV IR bridge, evdev bridge, keyboard, HTTP, WebSocket)
        -> key name (e.g. "KEY_UP")
        -> keymap layer (video focus > TV video mode > base)   [from config]
        -> whitelisted dashboard command       (e.g. "MOVE_UP")
        -> RemoteController updates the shared UI state
        -> UI state is pushed to every dashboard over the websocket

Nothing from the network is ever executed: only commands in COMMANDS are
accepted and arguments are validated against strict patterns. Video commands
only change the UI state; the browser's video component carries them out.
"""
from __future__ import annotations

import asyncio
import logging
import re
import time
from dataclasses import asdict, dataclass, field
from typing import Any, Awaitable, Callable, Optional

log = logging.getLogger("remote")

VIEWS = ["overview", "telemetry", "strategy", "racecontrol", "weather"]
TV_MODES = ["FULL_DASHBOARD", "RACE_VIEW", "VIDEO_FOCUS"]
COMMANDS = {
    "SELECT_DRIVER", "NEXT_DRIVER", "PREVIOUS_DRIVER", "MOVE_UP", "MOVE_DOWN",
    "OPEN_TELEMETRY", "CLOSE_PANEL", "OPEN_RACE_CONTROL", "CHANGE_VIEW",
    "TOGGLE_HELP", "TOGGLE_AUTO_CYCLE",
    # TV / video layer
    "CYCLE_TV_MODE", "SET_TV_MODE", "VIDEO_FOCUS", "VIDEO_UNFOCUS", "VIDEO_PLAY_PAUSE",
    "VIDEO_MUTE", "VIDEO_VOLUME", "VIDEO_SEEK", "VIDEO_FULLSCREEN",
    # VOYO <-> F1 synchronisation
    "SYNC_PLUS", "SYNC_MINUS", "SYNC_ADJUST", "SYNC_MARK", "SYNC_RESYNC", "SYNC_DEBUG", "PITLANE_DEBUG",
    "SYNC_START", "SYNC_CONFIRM", "SYNC_CLEAR", "SYNC_PIN", "SYNC_MENU", "SYNC_KEEP_OLD", "SYNC_USE_NEW",
    # track map wrong: rebuild the outline of this circuit (pit lane kept)
    "TRACK_REPORT",
    # LIVE / VOD mode selector (server/mode.py): AUTO detection or a manual override
    "SET_MODE", "CYCLE_MODE", "MODE_AUTO", "MODE_LIVE", "MODE_VOD",
}
MODE_COMMANDS = {"SET_MODE": None, "CYCLE_MODE": "NEXT", "MODE_AUTO": "AUTO", "MODE_LIVE": "LIVE",
                 "MODE_VOD": "VOD"}
MODE_ARG_RE = re.compile(r"^(AUTO|LIVE|VOD|NEXT)$")
SYNC_ACTIONS = {"SYNC_PLUS", "SYNC_MINUS", "SYNC_ADJUST", "SYNC_MARK", "SYNC_RESYNC",
                "SYNC_START", "SYNC_CONFIRM", "SYNC_CLEAR", "SYNC_PIN", "SYNC_KEEP_OLD", "SYNC_USE_NEW"}
VIDEO_ACTIONS = {"VIDEO_PLAY_PAUSE": "play_pause", "VIDEO_MUTE": "mute", "VIDEO_VOLUME": "volume",
                 "VIDEO_SEEK": "seek", "VIDEO_FULLSCREEN": "fullscreen"}
KEY_RE = re.compile(r"^[A-Z0-9_]{1,32}$")
DRIVER_RE = re.compile(r"^\d{1,3}$")
SEEK_RE = re.compile(r"^[+-]?\d{1,3}$")
ADJUST_RE = re.compile(r"^[+-]?\d{1,3}(\.\d{1,3})?$")


@dataclass
class UIState:
    view: str = "overview"
    selected: Optional[str] = None
    help: bool = False
    auto_cycle: bool = False
    tv_mode: str = "FULL_DASHBOARD"          # requested TV mode
    tv_mode_effective: str = "FULL_DASHBOARD"  # what is shown (FULL_DASHBOARD when no video is available)
    video_focus: bool = False
    video_available: bool = False
    video_notice: Optional[str] = None
    video_cmd: dict = field(default_factory=lambda: {"n": 0, "action": None, "arg": None})
    sync_menu: bool = False                  # SYNC menu open on the dashboards
    pit_debug: bool = False                  # pit-lane reconstruction debug overlay on the map
    toast: dict = field(default_factory=lambda: {"n": 0, "text": None})
    seq: int = 0


def _parse_keymap(raw: Any, section: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for key, cmd in (raw or {}).items():
        k = str(key).upper()
        name = str(cmd).split(":", 1)[0].upper()
        if not KEY_RE.match(k) or name not in COMMANDS:
            log.error("Ignoring invalid %s entry %s = %s", section, key, cmd)
            continue
        out[k] = str(cmd)
    return out


class RemoteController:
    def __init__(self, cfg: dict[str, Any], order_provider: Callable[[], list[str]],
                 publish: Callable[[dict], Awaitable[None]]) -> None:
        self.enabled = bool(cfg.get("enabled", True))
        self.token = str(cfg.get("token") or "")
        self.allow_get = bool(cfg.get("allow_get", True))
        self.keymap = _parse_keymap(cfg.get("keymap"), "keymap")
        # newer features get their key even with an older config.toml (unless that key is mapped)
        if self.keymap:
            self.keymap.setdefault("KEY_G", "PITLANE_DEBUG")
            self.keymap.setdefault("KEY_E", "CYCLE_MODE")         # mode selector AUTO -> LIVE -> VOD
            self.keymap.setdefault("KEY_MENU", "CYCLE_MODE")      # (WD TV remote MENU)
        # active while a TV mode with video is shown (RACE_VIEW / VIDEO_FOCUS)
        self.keymap_video = _parse_keymap(cfg.get("keymap_video"), "keymap_video")
        # active while the video player has the remote focus
        self.keymap_video_focus = _parse_keymap(cfg.get("keymap_video_focus"), "keymap_video_focus")
        self.auto_cycle_seconds = 20
        self.ui = UIState()
        self._order = order_provider
        self._publish = publish
        self._lock = asyncio.Lock()
        self._rate: list[float] = []
        # set by the app: executes SYNC_* commands, returns a toast text (or None if rejected)
        self.sync_hook: Optional[Callable[[str, Optional[str]], Optional[str]]] = None
        self.track_hook: Optional[Callable[[], str]] = None
        # set by the app: select AUTO / LIVE / VOD / NEXT, returns a toast text (switching may take a moment)
        self.mode_hook: Optional[Callable[[str], Awaitable[str]]] = None
        self._mode_task: Optional[asyncio.Task] = None

    # ------------------------------------------------------------------
    def check_token(self, supplied: Optional[str]) -> bool:
        return not self.token or supplied == self.token

    def rate_ok(self) -> bool:
        now = time.monotonic()
        self._rate = [t for t in self._rate if now - t < 1.0]
        if len(self._rate) >= 25:
            return False
        self._rate.append(now)
        return True

    def set_default_tv_mode(self, mode: str) -> None:
        if mode in TV_MODES:
            self.ui.tv_mode = mode
        self._recompute()

    async def set_video_available(self, available: bool, notice: Optional[str]) -> None:
        """Called by the video monitor. Without video the effective mode is FULL_DASHBOARD."""
        async with self._lock:
            before = asdict(self.ui)
            self.ui.video_available = available
            self.ui.video_notice = notice
            self._recompute()
            changed = asdict(self.ui) != before
            if changed:
                self.ui.seq += 1
        if changed:
            log.info("Video layer %s%s", "available" if available else "unavailable",
                     f" ({notice})" if notice else "")
            await self._publish(self.message())

    def _recompute(self) -> None:
        self.ui.tv_mode_effective = self.ui.tv_mode if self.ui.video_available else "FULL_DASHBOARD"
        if self.ui.tv_mode_effective == "FULL_DASHBOARD":
            self.ui.video_focus = False

    def _lookup(self, key: str) -> tuple[Optional[str], str]:
        video_shown = self.ui.tv_mode_effective != "FULL_DASHBOARD"
        if video_shown and self.ui.video_focus and key in self.keymap_video_focus:
            return self.keymap_video_focus[key], "video-focus"
        if video_shown and key in self.keymap_video:
            return self.keymap_video[key], "video"
        return self.keymap.get(key), "base"

    async def handle_key(self, key: str, origin: str) -> bool:
        key = (key or "").strip().upper()
        if not KEY_RE.match(key):
            log.warning("Rejected malformed key from %s", origin)
            return False
        if not key.startswith("KEY_"):
            key = "KEY_" + key
        mapped, layer = self._lookup(key)
        if not mapped:
            log.info("Unmapped remote key %s from %s", key, origin)
            return False
        name, _, arg = mapped.partition(":")
        log.info("Remote key %s -> %s%s [%s] (%s)", key, name, f":{arg}" if arg else "", layer, origin)
        return await self.handle_command(name, arg or None, origin, log_it=False)

    async def handle_command(self, name: str, arg: Any = None, origin: str = "?", log_it: bool = True) -> bool:
        name = str(name or "").upper()
        if name not in COMMANDS:
            log.warning("Rejected unknown command %r from %s", name[:40], origin)
            return False
        if arg is not None:
            arg = str(arg)[:32]
        if log_it:
            log.info("Remote command %s%s (%s)", name, f":{arg}" if arg else "", origin)
        if name in MODE_COMMANDS:
            target = MODE_COMMANDS[name] or (arg or "").upper()
            if not MODE_ARG_RE.match(target) or self.mode_hook is None:
                return False
            # the switch runs in the background (stopping one source and starting the other takes a
            # moment); the selector state is pushed by the mode controller, the result as a toast
            self._mode_task = asyncio.get_event_loop().create_task(self._run_mode(target))
            return True
        async with self._lock:
            changed = self._apply(name, arg)
            if changed:
                self.ui.seq += 1
        if changed:
            await self._publish(self.message())
        return True

    async def _run_mode(self, target: str) -> None:
        try:
            text = await self.mode_hook(target)
        except Exception as exc:  # noqa: BLE001
            log.exception("Mode selection %s failed", target)
            text = f"Mode {target}: {exc}"
        async with self._lock:
            self._toast(text)
            self.ui.seq += 1
        await self._publish(self.message())

    def message(self) -> dict:
        return {"type": "ui", **asdict(self.ui)}

    # ------------------------------------------------------------------
    def _ensure_selected(self, order: list[str]) -> None:
        if self.ui.selected not in order and order:
            self.ui.selected = order[0]

    def _step(self, delta: int) -> None:
        order = self._order()
        if not order:
            return
        if self.ui.selected not in order:
            self.ui.selected = order[0]
            return
        i = order.index(self.ui.selected)
        self.ui.selected = order[(i + delta) % len(order)]

    def _toast(self, text: str) -> None:
        self.ui.toast = {"n": self.ui.toast["n"] + 1, "text": text[:120]}

    def _video_cmd(self, action: str, arg: Optional[str]) -> None:
        self.ui.video_cmd = {"n": self.ui.video_cmd["n"] + 1, "action": action, "arg": arg}

    def _apply(self, name: str, arg: Optional[str]) -> bool:
        before = asdict(self.ui)
        order = self._order()
        video_shown = self.ui.tv_mode_effective != "FULL_DASHBOARD"
        if name in ("MOVE_UP", "PREVIOUS_DRIVER"):
            self._step(-1)
        elif name in ("MOVE_DOWN", "NEXT_DRIVER"):
            self._step(1)
        elif name == "SELECT_DRIVER":
            if arg and DRIVER_RE.match(arg) and arg in order:
                self.ui.selected = arg
            else:
                self._ensure_selected(order)
        elif name == "OPEN_TELEMETRY":
            self._ensure_selected(order)
            self.ui.view = "telemetry"
            self.ui.help = False
        elif name == "CLOSE_PANEL":
            if self.ui.help:
                self.ui.help = False
            else:
                self.ui.view = "overview"
        elif name == "OPEN_RACE_CONTROL":
            self.ui.view = "overview" if self.ui.view == "racecontrol" else "racecontrol"
        elif name == "CHANGE_VIEW":
            if arg in VIEWS:
                self.ui.view = arg
            elif arg in ("next", "prev", None):
                i = VIEWS.index(self.ui.view) if self.ui.view in VIEWS else 0
                self.ui.view = VIEWS[(i + (-1 if arg == "prev" else 1)) % len(VIEWS)]
            if self.ui.view == "telemetry":
                self._ensure_selected(order)
        elif name == "TOGGLE_HELP":
            self.ui.help = not self.ui.help
        elif name == "TOGGLE_AUTO_CYCLE":
            self.ui.auto_cycle = not self.ui.auto_cycle
            log.info("Auto view cycling %s", "enabled" if self.ui.auto_cycle else "disabled")
        # ---- TV / video ---------------------------------------------------
        elif name == "CYCLE_TV_MODE":
            if not self.ui.video_available:
                # say why T "does nothing" instead of silently staying on the dashboard
                self._toast("No video layer: " + (self.ui.video_notice or "VOYO is disabled ([voyo] enabled)")
                            + " - only the dashboard can be shown")
            modes = TV_MODES if self.ui.video_available else ["FULL_DASHBOARD"]
            i = modes.index(self.ui.tv_mode) if self.ui.tv_mode in modes else -1
            self.ui.tv_mode = modes[(i + 1) % len(modes)]
            self.ui.video_focus = False
        elif name == "SET_TV_MODE":
            if arg and arg.upper() in TV_MODES:
                self.ui.tv_mode = arg.upper()
                self.ui.video_focus = False
        elif name == "VIDEO_FOCUS":
            if video_shown:
                self.ui.video_focus = not self.ui.video_focus
        elif name == "VIDEO_UNFOCUS":
            # Back: leave video focus -> leave VIDEO_FOCUS mode -> behave like CLOSE_PANEL
            if self.ui.video_focus:
                self.ui.video_focus = False
            elif self.ui.tv_mode == "VIDEO_FOCUS" and video_shown:
                self.ui.tv_mode = "RACE_VIEW"
            elif self.ui.help:
                self.ui.help = False
            else:
                self.ui.view = "overview"
        # ---- sync -----------------------------------------------------------
        elif name == "TRACK_REPORT":
            if self.track_hook is None:
                return False
            text = self.track_hook()
            log.info("Track map: %s", text)
            self._toast(text)
        elif name == "PITLANE_DEBUG":
            self.ui.pit_debug = not self.ui.pit_debug
        elif name in ("SYNC_MENU", "SYNC_DEBUG"):
            self.ui.sync_menu = not self.ui.sync_menu if arg not in ("open", "close") else arg == "open"
            if self.ui.sync_menu:
                self.ui.help = False
                if self.ui.tv_mode == "VIDEO_FOCUS":        # the video window would cover the menu
                    self.ui.tv_mode = "RACE_VIEW"
                    self.ui.video_focus = False
        elif name in SYNC_ACTIONS:
            if name == "SYNC_ADJUST" and (arg is None or not ADJUST_RE.match(arg)):
                return False
            if self.sync_hook is None:
                return False
            text = self.sync_hook(name, arg)
            if text is None:
                return False
            log.info("Sync: %s", text)
            self._toast(text)
        elif name in VIDEO_ACTIONS:
            if not video_shown:
                return False
            if name == "VIDEO_SEEK" and (arg is None or not SEEK_RE.match(arg)):
                return False
            if name == "VIDEO_VOLUME" and arg not in ("up", "down"):
                return False
            self._video_cmd(VIDEO_ACTIONS[name], arg)
        self._recompute()
        return asdict(self.ui) != before

    async def auto_cycle_loop(self) -> None:
        while True:
            await asyncio.sleep(max(5, self.auto_cycle_seconds))
            if self.ui.auto_cycle:
                await self.handle_command("CHANGE_VIEW", "next", "auto-cycle", log_it=False)
