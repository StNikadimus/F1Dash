#!/usr/bin/env python3
"""Dashboard + official VOYO player window on one TV screen - launcher and TV agent.

VOYO offers no embeddable player, so the official voyo.si website (your login,
DRM and player untouched) runs in its own browser app window. This script
opens both windows and then keeps running as a small *TV agent* that:

* keeps the VOYO window ALWAYS ON TOP of the dashboard while a video TV mode is
  shown - clicking the dashboard (to give it the keyboard) no longer hides VOYO;
* follows the dashboard's TV mode and moves/resizes the VOYO window:
      RACE_VIEW      -> video slot 1280x720 (top-left of the 1920x1080 stage)
      VIDEO_FOCUS    -> 1920x990 (bottom 90 px = dashboard info bar)
      FULL_DASHBOARD -> VOYO window sent BEHIND the dashboard (never minimized: a
                        minimized / hidden page makes the browser pause muted or
                        video-only playback, and some players pause themselves)
* while the VOYO window has the keyboard focus, forwards the dashboard keys
  (T, H, I, A, 1-5, Up, Down, S, R, D, =, - by default) to the dashboard server,
  so e.g. T switches the TV mode without clicking anything. Left/Right, Space,
  F, M etc. stay with VOYO's own player. While a text field of the VOYO page has
  the focus (login, PIN, search) no key is taken away from the page.
* runs the VOYO playback clock bridge (tools/voyo_clock.py): reads the player's
  HTMLVideoElement.currentTime through the VOYO window's local DevTools port
  and sends it to the server, which shows the F1 data for exactly the moment
  that is on screen (pause / seek / buffering included). Read-only; disable
  with --no-clock.

Windows: full support (Win32 via ctypes, no extra packages).
Linux (X11): window placement via `wmctrl`; no hotkeys.

    python tools/tv_launcher.py                              # launch both + run agent
    python tools/tv_launcher.py --server http://192.168.1.10:8080
    python tools/tv_launcher.py --attach                     # windows already open: only run the agent
    python tools/tv_launcher.py --no-agent                   # launch only (old behaviour)
    python tools/tv_launcher.py --titlebar 0                 # do not hide VOYO's window title bar
    python tools/tv_launcher.py --hotkeys T,H,I,1,2,3,4,5,UP,DOWN,A
    python tools/tv_launcher.py --no-clock                   # no VOYO playback clock (fixed delay)

Stop the agent with Ctrl+C (the windows stay open).
"""
from __future__ import annotations

import argparse
import atexit
import json
import os
import platform
import shutil
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path
from typing import Callable, Optional

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from server.config import load_config  # noqa: E402

STAGE_W, STAGE_H = 1920, 1080
SLOTS = {"RACE_VIEW": (0, 0, 1280, 720), "VIDEO_FOCUS": (0, 0, 1920, 990)}
DASHBOARD_TITLES = ("F1 Timing Wall", "F1 Remote")
VK = {**{c: ord(c) for c in "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"},
      "UP": 0x26, "DOWN": 0x28, "LEFT": 0x25, "RIGHT": 0x27, "PAGEUP": 0x21, "PAGEDOWN": 0x22,
      "EQUAL": 0xBB, "MINUS": 0xBD, "KPPLUS": 0x6B, "KPMINUS": 0x6D}


def slot_rects(screen_w: int, screen_h: int) -> dict[str, tuple[int, int, int, int]]:
    """Video slot rectangles in screen pixels (the dashboard letterboxes its stage)."""
    scale = min(screen_w / STAGE_W, screen_h / STAGE_H)
    ox, oy = (screen_w - STAGE_W * scale) / 2, (screen_h - STAGE_H * scale) / 2
    return {m: (round(ox + x * scale), round(oy + y * scale), round(w * scale), round(h * scale))
            for m, (x, y, w, h) in SLOTS.items()}


# ---------------------------------------------------------------------------
# window backends
# ---------------------------------------------------------------------------
class NullOps:
    name = "none"
    dpi_scale = 1.0

    def screen_size(self) -> tuple[int, int]:
        return STAGE_W, STAGE_H

    def find_voyo(self, pid: Optional[int], title_hint: str): return None
    def is_valid(self, win) -> bool: return False
    def place(self, win, rect, topmost: bool) -> None: pass
    def minimize(self, win) -> None: pass
    def send_back(self, win) -> None: pass
    def foreground_is(self, win) -> bool: return False
    def poll_hotkeys(self, active: bool) -> list[str]: return []
    def set_taskbar_hidden(self, hidden: bool) -> None: pass
    def find_dashboard(self, pid: Optional[int]): return None
    def close_window(self, win) -> None: pass


class WinOps(NullOps):
    """Win32 window control through ctypes."""
    name = "windows"

    def __init__(self, hotkeys: list[str]) -> None:
        import ctypes
        from ctypes import wintypes
        self.ct, self.wt = ctypes, wintypes
        self.user32 = ctypes.WinDLL("user32", use_last_error=True)
        try:
            self.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))   # per-monitor v2
        except Exception:  # noqa: BLE001
            try:
                ctypes.windll.shcore.SetProcessDpiAwareness(2)
            except Exception:  # noqa: BLE001
                pass
        try:
            self.dpi_scale = self.user32.GetDpiForSystem() / 96.0
        except Exception:  # noqa: BLE001
            self.dpi_scale = 1.0
        self.dwm = ctypes.WinDLL("dwmapi")
        u = self.user32
        HWND = wintypes.HWND
        u.SetWindowPos.argtypes = [HWND, HWND, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, wintypes.UINT]
        u.GetWindowRect.argtypes = [HWND, ctypes.POINTER(wintypes.RECT)]
        u.GetWindowTextW.argtypes = [HWND, wintypes.LPWSTR, ctypes.c_int]
        u.GetClassNameW.argtypes = [HWND, wintypes.LPWSTR, ctypes.c_int]
        u.GetWindowThreadProcessId.argtypes = [HWND, ctypes.POINTER(wintypes.DWORD)]
        u.ShowWindow.argtypes = [HWND, ctypes.c_int]
        u.IsWindow.argtypes = [HWND]
        u.IsWindowVisible.argtypes = [HWND]
        u.IsIconic.argtypes = [HWND]
        u.GetAncestor.argtypes = [HWND, wintypes.UINT]
        u.GetAncestor.restype = HWND
        u.GetForegroundWindow.restype = HWND
        u.PostMessageW.argtypes = [HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
        u.RegisterHotKey.argtypes = [HWND, ctypes.c_int, wintypes.UINT, wintypes.UINT]
        u.UnregisterHotKey.argtypes = [HWND, ctypes.c_int]
        self.hotkeys = [(i + 1, k) for i, k in enumerate(hotkeys) if k in VK]
        self.registered = False
        self._borders: dict[int, tuple[int, int, int, int]] = {}

    def screen_size(self) -> tuple[int, int]:
        return self.user32.GetSystemMetrics(0), self.user32.GetSystemMetrics(1)

    def _text(self, fn, hwnd) -> str:
        buf = self.ct.create_unicode_buffer(512)
        fn(hwnd, buf, 512)
        return buf.value

    def find_dashboard(self, pid: Optional[int]):
        return self._find(pid, "F1 Timing Wall", dashboard=True)

    def close_window(self, win) -> None:
        if win and self.user32.IsWindow(win):
            self.user32.PostMessageW(win, 0x0010, 0, 0)       # WM_CLOSE (normal close, like the X button)

    def find_voyo(self, pid: Optional[int], title_hint: str):
        return self._find(pid, title_hint, dashboard=False)

    def _find(self, pid: Optional[int], title_hint: str, dashboard: bool):
        ct, wt, u = self.ct, self.wt, self.user32
        found: list[tuple[int, int]] = []

        @ct.WINFUNCTYPE(ct.c_bool, wt.HWND, wt.LPARAM)
        def cb(hwnd, _):
            if not u.IsWindowVisible(hwnd) and not u.IsIconic(hwnd):
                return True
            if self._text(u.GetClassNameW, hwnd) != "Chrome_WidgetWin_1":
                return True
            title = self._text(u.GetWindowTextW, hwnd)
            is_dash = any(t in title for t in DASHBOARD_TITLES)
            if not title or is_dash != dashboard:
                return True
            wpid = wt.DWORD()
            u.GetWindowThreadProcessId(hwnd, ct.byref(wpid))
            score = 2 if pid and wpid.value == pid else (1 if title_hint.lower() in title.lower() else 0)
            if score:
                found.append((score, hwnd))
            return True

        u.EnumWindows(cb, 0)
        if not found:
            return None
        return max(found, key=lambda f: f[0])[1]

    def is_valid(self, win) -> bool:
        return bool(win) and bool(self.user32.IsWindow(win))

    def _frame_borders(self, win) -> tuple[int, int, int, int]:
        """Invisible resize borders around the visible frame (Windows 10/11)."""
        ct, wt = self.ct, self.wt
        wr, fr = wt.RECT(), wt.RECT()
        self.user32.GetWindowRect(win, ct.byref(wr))
        if self.dwm.DwmGetWindowAttribute(win, 9, ct.byref(fr), ct.sizeof(fr)) != 0:
            return 0, 0, 0, 0
        return fr.left - wr.left, fr.top - wr.top, wr.right - fr.right, wr.bottom - fr.bottom

    def place(self, win, rect, topmost: bool) -> None:
        u = self.user32
        if u.IsIconic(win):
            u.ShowWindow(win, 9)                       # SW_RESTORE
        x, y, w, h = rect
        after = self.wt.HWND(-1 if topmost else -2)     # HWND_TOPMOST / HWND_NOTOPMOST
        flags = 0x0010 | 0x0040                        # SWP_NOACTIVATE | SWP_SHOWWINDOW
        u.SetWindowPos(win, after, x, y, w, h, flags)
        l, t, r, b = self._frame_borders(win)
        if l or t or r or b:
            u.SetWindowPos(win, after, x - l, y - t, w + l + r, h + t + b, flags)

    def set_taskbar_hidden(self, hidden: bool) -> None:
        """Hide the Windows taskbar(s). Windows shows the taskbar as soon as a normal
        (non-fullscreen) window like the VOYO window is in the foreground."""
        u = self.user32
        u.FindWindowW.argtypes = [self.wt.LPCWSTR, self.wt.LPCWSTR]
        u.FindWindowW.restype = self.wt.HWND
        u.FindWindowExW.argtypes = [self.wt.HWND, self.wt.HWND, self.wt.LPCWSTR, self.wt.LPCWSTR]
        u.FindWindowExW.restype = self.wt.HWND
        bars = []
        main = u.FindWindowW("Shell_TrayWnd", None)
        if main:
            bars.append(main)
        h = None
        while True:                                   # taskbars on additional monitors
            h = u.FindWindowExW(None, h, "Shell_SecondaryTrayWnd", None)
            if not h:
                break
            bars.append(h)
        for bar in bars:
            visible = bool(u.IsWindowVisible(bar))
            if hidden and visible:
                u.ShowWindow(bar, 0)                  # SW_HIDE
            elif not hidden and not visible:
                u.ShowWindow(bar, 5)                  # SW_SHOW

    def minimize(self, win) -> None:
        u = self.user32
        u.SetWindowPos(win, self.wt.HWND(-2), 0, 0, 0, 0, 0x0001 | 0x0002 | 0x0010)  # NOTOPMOST, keep size/pos
        u.ShowWindow(win, 6)                           # SW_MINIMIZE

    def send_back(self, win) -> None:
        """Behind every other window, but NOT minimized: the page stays a shown page for
        the browser, so VOYO's playback is not paused (minimized = hidden page)."""
        u = self.user32
        if u.IsIconic(win):
            u.ShowWindow(win, 4)                       # SW_SHOWNOACTIVATE
        keep = 0x0001 | 0x0002 | 0x0010                # SWP_NOSIZE | SWP_NOMOVE | SWP_NOACTIVATE
        u.SetWindowPos(win, self.wt.HWND(-2), 0, 0, 0, 0, keep)   # HWND_NOTOPMOST
        u.SetWindowPos(win, self.wt.HWND(1), 0, 0, 0, 0, keep)    # HWND_BOTTOM

    def foreground_is(self, win) -> bool:
        fg = self.user32.GetForegroundWindow()
        if not fg:
            return False
        root = self.user32.GetAncestor(fg, 2)          # GA_ROOT
        return bool(win) and (fg == win or root == win)

    def poll_hotkeys(self, active: bool) -> list[str]:
        """Hotkeys are registered only while the VOYO window has the focus, so the
        keys keep their normal meaning in every other application."""
        u = self.user32
        if active and not self.registered:
            for hid, key in self.hotkeys:
                if not u.RegisterHotKey(None, hid, 0x4000, VK[key]):     # MOD_NOREPEAT
                    print(f"  hotkey {key} is used by another program - skipped")
            self.registered = True
        elif not active and self.registered:
            for hid, _ in self.hotkeys:
                u.UnregisterHotKey(None, hid)
            self.registered = False
        pressed = []
        msg = self.wt.MSG()
        while u.PeekMessageW(self.ct.byref(msg), None, 0, 0, 1):          # PM_REMOVE
            if msg.message == 0x0312:                                    # WM_HOTKEY
                pressed += [k for hid, k in self.hotkeys if hid == msg.wParam]
        return pressed


class WmctrlOps(NullOps):
    """Linux/X11 fallback: wmctrl for placement. No hotkeys."""
    name = "wmctrl"

    def screen_size(self) -> tuple[int, int]:
        try:
            out = subprocess.run(["xrandr", "--current"], capture_output=True, text=True, timeout=5).stdout
            for line in out.splitlines():
                if "*" in line:
                    w, h = line.split()[0].split("x")
                    return int(w), int(h)
        except Exception:  # noqa: BLE001
            pass
        return STAGE_W, STAGE_H

    def find_voyo(self, pid: Optional[int], title_hint: str):
        try:
            out = subprocess.run(["wmctrl", "-l", "-p"], capture_output=True, text=True, timeout=5).stdout
        except Exception:  # noqa: BLE001
            return None
        best = None
        for line in out.splitlines():
            parts = line.split(None, 4)
            if len(parts) < 5 or any(t in parts[4] for t in DASHBOARD_TITLES):
                continue
            if pid and parts[2] == str(pid):
                return parts[0]
            if title_hint.lower() in parts[4].lower():
                best = parts[0]
        return best

    def is_valid(self, win) -> bool:
        try:
            out = subprocess.run(["wmctrl", "-l"], capture_output=True, text=True, timeout=5).stdout
        except Exception:  # noqa: BLE001
            return bool(win)
        return bool(win) and any(line.split(None, 1)[0] == win for line in out.splitlines() if line.strip())

    def find_dashboard(self, pid: Optional[int]):
        try:
            out = subprocess.run(["wmctrl", "-l"], capture_output=True, text=True, timeout=5).stdout
        except Exception:  # noqa: BLE001
            return None
        return next((ln.split(None, 1)[0] for ln in out.splitlines() if "F1 Timing Wall" in ln), None)

    def close_window(self, win) -> None:
        if win:
            subprocess.run(["wmctrl", "-i", "-c", win], timeout=5)

    def place(self, win, rect, topmost: bool) -> None:
        x, y, w, h = rect
        subprocess.run(["wmctrl", "-i", "-r", win, "-b", "remove,hidden"], timeout=5)
        subprocess.run(["wmctrl", "-i", "-r", win, "-e", f"0,{x},{y},{w},{h}"], timeout=5)
        subprocess.run(["wmctrl", "-i", "-r", win, "-b", "remove,below"], timeout=5)
        subprocess.run(["wmctrl", "-i", "-r", win, "-b", ("add" if topmost else "remove") + ",above"], timeout=5)

    def send_back(self, win) -> None:
        subprocess.run(["wmctrl", "-i", "-r", win, "-b", "remove,above,hidden"], timeout=5)
        subprocess.run(["wmctrl", "-i", "-r", win, "-b", "add,below"], timeout=5)

    def minimize(self, win) -> None:
        subprocess.run(["wmctrl", "-i", "-r", win, "-b", "remove,above"], timeout=5)
        if shutil.which("xdotool"):
            subprocess.run(["xdotool", "windowminimize", str(int(win, 16))], timeout=5)


# ---------------------------------------------------------------------------
# agent
# ---------------------------------------------------------------------------
class TvAgent:
    def __init__(self, ops, server: str, token: str, titlebar_dip: int, title_hint: str,
                 pid: Optional[int], http_get: Callable[[str], dict] | None = None,
                 http_key: Callable[[str], None] | None = None) -> None:
        self.ops = ops
        # Windows resolves "localhost" to ::1 first; the server listens on IPv4 only, so
        # every request would wait ~2 s for the IPv6 attempt to fail -> use 127.0.0.1
        self.server = server.rstrip("/").replace("://localhost", "://127.0.0.1")
        self.token = token
        self.title_hint = title_hint
        self.pid = pid
        w, h = ops.screen_size()
        self.rects = slot_rects(w, h)
        self.titlebar = round(titlebar_dip * ops.dpi_scale)
        self.win = None
        self.applied: Optional[str] = None
        self._get = http_get or self._http_get
        self._key = http_key or self._http_key
        self.last_error = ""
        self.poll_now = threading.Event()
        self.hide_taskbar = True
        self._taskbar_check = 0.0
        self.close_together = True
        self.dash_pid: Optional[int] = None
        self.dash_win = None
        self._seen = {"voyo": False, "dash": False}
        self._missing_since = {"voyo": None, "dash": None}
        self.stop_reason: Optional[str] = None
        # extra window-title hints (the VOYO page title read by the clock bridge): a
        # recording's page is titled e.g. "VN Azerbajdžana - Glej dirke online", without "VOYO"
        self.title_hints: Callable[[], list[str]] = lambda: []
        self._search_since: Optional[float] = None
        self._search_warned = False
        # True while a text field of the VOYO page has the focus (clock bridge): the
        # hotkeys are released so login / PIN / search typing reaches VOYO
        self.typing: Callable[[], bool] = lambda: False
        self._typing_said = False

    # -- server ---------------------------------------------------------------
    def _http_get(self, path: str) -> dict:
        req = urllib.request.Request(self.server + path)
        with urllib.request.urlopen(req, timeout=2) as r:
            return json.loads(r.read())

    def _http_key(self, key: str) -> None:
        def run():
            req = urllib.request.Request(self.server + "/api/remote/key", method="POST",
                                         data=json.dumps({"key": key}).encode(),
                                         headers={"Content-Type": "application/json"})
            if self.token:
                req.add_header("X-Remote-Token", self.token)
            try:
                body = json.loads(urllib.request.urlopen(req, timeout=3).read() or b"{}")
                ui = body.get("ui") or {}
                print(f"    {key}: {'ok' if body.get('ok') else 'not mapped'} - TV mode "
                      f"{ui.get('tv_mode_effective')}" + ("" if ui.get("video_available", True) else
                                                        f" (video layer unavailable: {ui.get('video_notice')})"))
            except Exception as exc:  # noqa: BLE001
                print(f"  key {key} not delivered: {exc}")
            self.poll_now.set()             # read the new TV mode immediately
        threading.Thread(target=run, daemon=True).start()

    # -- geometry -------------------------------------------------------------
    def window_rect(self, mode: str) -> tuple[int, int, int, int]:
        """Slot rectangle; the window's own title bar is pushed above the slot so only
        the page content covers the video area."""
        x, y, w, h = self.rects[mode]
        return x, y - self.titlebar, w, h + self.titlebar

    def _watch(self, name: str, present: bool) -> None:
        """Remember that a window existed; if it disappears for >2 s, the user closed it."""
        now = time.monotonic()
        if present:
            self._seen[name], self._missing_since[name] = True, None
        elif self._seen[name]:
            if self._missing_since[name] is None:
                self._missing_since[name] = now
            elif now - self._missing_since[name] > 2 and self.close_together:
                self.stop_reason = ("VOYO" if name == "voyo" else "dashboard") + " window was closed"

    # -- main step --------------------------------------------------------------
    def step(self, ui: Optional[dict]) -> None:
        if not self.ops.is_valid(self.dash_win):
            self.dash_win = self.ops.find_dashboard(self.dash_pid)
        self._watch("dash", bool(self.dash_win))
        if not self.ops.is_valid(self.win):
            self.win = None
            for hint in [self.title_hint] + [h for h in self.title_hints() if h]:
                self.win = self.ops.find_voyo(self.pid, hint)
                if self.win:
                    break
            self.applied = None
            if self.win:
                print(f"  VOYO window found ({self.win})")
                self._search_since, self._search_warned = None, False
            else:
                now = time.monotonic()
                self._search_since = self._search_since or now
                if now - self._search_since > 8 and not self._search_warned:
                    self._search_warned = True
                    hints = [self.title_hint] + [h for h in self.title_hints() if h]
                    print(f"  VOYO window NOT found (looking for a browser window titled {hints!r}) - TV modes and "
                          "the T/H/... keys in the VOYO window do not work until it is found. "
                          "Use --title \"<part of the VOYO window title>\" if needed.")
        self._watch("voyo", bool(self.win))
        if ui is not None and self.win:
            mode = ui.get("tv_mode_effective") or "FULL_DASHBOARD"
            if mode != self.applied:
                if mode in self.rects:
                    self.ops.place(self.win, self.window_rect(mode), topmost=True)
                    print(f"  TV mode {mode}: VOYO window -> {self.window_rect(mode)} (always on top)")
                else:
                    # never minimize: a minimized window is a hidden page and the browser
                    # (or the player itself) pauses the video - keep it shown, behind the dashboard
                    self.ops.send_back(self.win)
                    print(f"  TV mode {mode}: VOYO window behind the dashboard (keeps playing)")
                self.applied = mode
        active = bool(self.win) and self.applied in self.rects and self.ops.foreground_is(self.win)
        if active and self.typing():
            active = False
            if not self._typing_said:
                self._typing_said = True
                print("  typing in the VOYO page - dashboard keys paused until the text field loses the focus")
        elif not self.typing():
            self._typing_said = False
        for key in self.ops.poll_hotkeys(active):
            print(f"  key {key} (VOYO window) -> dashboard")
            self._key("KEY_" + key)

    def run(self) -> None:
        print(f"TV agent running ({self.ops.name}); slots: {self.rects}")
        print("Close this window (or press Ctrl+C, or close the dashboard/VOYO window) to stop everything.")
        last_poll, ui = 0.0, None
        while self.stop_reason is None:
            now = time.monotonic()
            if now - last_poll > 0.25 or self.poll_now.is_set():
                self.poll_now.clear()
                last_poll = now
                try:
                    ui = self._get("/api/ui")
                    self.last_error = ""
                except Exception as exc:  # noqa: BLE001
                    if str(exc) != self.last_error:
                        self.last_error = str(exc)
                        print(f"  dashboard server not reachable ({exc}) - retrying")
                    ui = None
            self.step(ui)
            if self.hide_taskbar and now - self._taskbar_check > 2:   # re-hide e.g. after an Explorer restart
                self._taskbar_check = now
                self.ops.set_taskbar_hidden(True)
            time.sleep(0.05)


# ---------------------------------------------------------------------------
# launcher
# ---------------------------------------------------------------------------
def find_browser() -> str | None:
    system = platform.system()
    candidates: list[str] = []
    if system == "Windows":
        for base in (os.environ.get("PROGRAMFILES(X86)", ""), os.environ.get("PROGRAMFILES", ""),
                     os.environ.get("LOCALAPPDATA", "")):
            candidates += [os.path.join(base, "Microsoft", "Edge", "Application", "msedge.exe"),
                           os.path.join(base, "Google", "Chrome", "Application", "chrome.exe")]
    elif system == "Darwin":
        candidates += ["/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
                       "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"]
    else:
        for name in ("microsoft-edge", "microsoft-edge-stable", "google-chrome", "google-chrome-stable",
                     "chromium", "chromium-browser"):
            p = shutil.which(name)
            if p:
                candidates.append(p)
    return next((c for c in candidates if c and os.path.exists(c)), None)


def reset_window_placement(profile: Path) -> None:
    pref = profile / "Default" / "Preferences"
    if not pref.exists():
        return
    try:
        data = json.loads(pref.read_text(encoding="utf-8"))
        browser = data.get("browser") or {}
        if any(browser.pop(k, None) is not None for k in ("app_window_placement", "window_placement")):
            pref.write_text(json.dumps(data), encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass


# Screen-capture compatibility of the VOYO window (AirParrot / Miracast / OBS mirroring shows
# the video black while the monitor shows it). Each level only changes how THIS browser window
# renders: the video is composited into the normal window image instead of a GPU overlay
# plane. VOYO's player, login, DRM and stream are untouched.
CAPTURE_LEVELS = {
    "off": [],
    "no-overlays": ["--disable-direct-composition-video-overlays"],
    "no-hw-decode": ["--disable-direct-composition-video-overlays", "--disable-accelerated-video-decode"],
    "no-gpu": ["--disable-gpu"],
}
CAPTURE_ALIASES = {"0": "off", "1": "no-overlays", "2": "no-hw-decode", "3": "no-gpu"}


def capture_flags(level: str) -> list[str]:
    level = CAPTURE_ALIASES.get(str(level).strip().lower(), str(level).strip().lower() or "off")
    if level not in CAPTURE_LEVELS:
        raise SystemExit(f"--capture must be one of {', '.join(CAPTURE_LEVELS)} (or 0-3), not {level!r}")
    return CAPTURE_LEVELS[level]


def make_ops(hotkeys: list[str]):
    system = platform.system()
    if system == "Windows":
        return WinOps(hotkeys)
    if system == "Linux" and shutil.which("wmctrl"):
        return WmctrlOps()
    return NullOps()


class Session:
    """Everything the launcher started; shutdown() closes it all exactly once."""

    def __init__(self, ops) -> None:
        self.ops = ops
        self.agent: Optional[TvAgent] = None
        self.server_proc: Optional[subprocess.Popen] = None
        self.browser_pids: list[int] = []
        self.clock = None                                         # VoyoClockBridge
        self.close_browsers = True
        self._done = threading.Lock()
        self._finished = False

    def shutdown(self, reason: str) -> None:
        with self._done:
            if self._finished:
                return
            self._finished = True
        print(f"\nShutting down ({reason}) ...")
        self.ops.set_taskbar_hidden(False)                      # first: the taskbar always comes back
        if self.clock:
            self.clock.stop.set()
        if self.close_browsers and self.agent:
            for win in (self.agent.win, self.agent.dash_win):
                try:
                    self.ops.close_window(win)
                except Exception:  # noqa: BLE001
                    pass
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline and any(_alive(p) for p in self.browser_pids):
                time.sleep(0.2)
            for pid in self.browser_pids:                       # still running -> end the process tree
                if _alive(pid):
                    _kill_tree(pid)
        if self.server_proc and self.server_proc.poll() is None:
            print("  stopping dashboard server")
            self.server_proc.terminate()
            try:
                self.server_proc.wait(5)
            except subprocess.TimeoutExpired:
                self.server_proc.kill()
        print("Done - taskbar restored.")


def _alive(pid: int) -> bool:
    if platform.system() == "Windows":
        out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH"], capture_output=True, text=True).stdout
        return str(pid) in out
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _kill_tree(pid: int) -> None:
    if platform.system() == "Windows":
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True)
    else:
        try:
            os.kill(pid, 15)
        except OSError:
            pass


def _server_up(url: str) -> bool:
    try:
        with urllib.request.urlopen(url.rstrip("/") + "/api/health", timeout=1.5) as r:
            return r.status == 200
    except Exception:  # noqa: BLE001
        return False


SERVER_LOG_SHOW = ("AUTO MEDIA SYNC", "VOD", "SYNC CHECK", "Sync:", "VOYO playback clock", "ERROR", "Traceback",
                   "RECORDING IN LIVE MODE", "PHONE REMOTE")
SERVER_LOG_HIDE = ("Circuit geometry", "No geometry", "multiviewer")


SERVER_LOG_ERROR_START = ("Traceback (most recent call last)", "Exception in ASGI application")


def _log_line(line: str) -> str:
    """Drop the date of a timestamped log line; other lines (uvicorn, tracebacks) stay whole."""
    s = line.rstrip()
    return s[11:] if len(s) > 11 and s[:4].isdigit() and s[4] == "-" else s


def follow_server_log(path: Path) -> None:
    """Show the server lines that matter for VOD / sync in this terminal (the full log stays in the file).
    An error is shown with its whole traceback, ending with the line that says what went wrong."""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            in_trace = 0
            while True:
                line = fh.readline()
                if not line:
                    time.sleep(0.5)
                    continue
                if in_trace:
                    if line[:4].isdigit() or line.startswith("INFO:") or not line.strip():
                        in_trace = 0                     # the next normal log line: the traceback ended
                    else:
                        in_trace -= 1
                        print("  server | " + line.rstrip())
                        continue
                if any(k in line for k in SERVER_LOG_ERROR_START):
                    in_trace = 80
                    print("  server " + _log_line(line))
                    continue
                if any(k in line for k in SERVER_LOG_SHOW) and not any(k in line for k in SERVER_LOG_HIDE):
                    print("  server " + _log_line(line))
    except OSError:
        pass


def choose_mode(now: Optional[float] = None, index: Optional[dict] = None) -> tuple[str, str]:
    """What the dashboard's AUTO mode would pick now: LIVE while an F1 session is on (or about to
    start), otherwise VOD (a VOYO recording). Same rule as server/mode.py (the running server
    keeps re-checking it; the MODE selector on the dashboard overrides it)."""
    from datetime import datetime, timezone
    from server.mode import detect_from_schedule
    now_s = now if now is not None else time.time()
    if index is None:
        try:
            year = datetime.fromtimestamp(now_s, timezone.utc).year
            req = urllib.request.Request(f"https://livetiming.formula1.com/static/{year}/Index.json",
                                         headers={"User-Agent": "f1-tv-dashboard/1.0"})
            with urllib.request.urlopen(req, timeout=8) as r:
                index = json.loads(r.read().decode("utf-8-sig"))
        except Exception as exc:  # noqa: BLE001
            return "vod", f"F1 schedule not reachable ({type(exc).__name__}) - assuming a recording"
    det, _sess, why = detect_from_schedule(index, now_s)
    return det.lower(), why


def print_phone_remote(server: str) -> None:
    """The phone remote's address on the LAN / Tailscale (the server detects it) - 127.0.0.1 is no use
    on a phone."""
    try:
        with urllib.request.urlopen(server.rstrip("/") + "/api/remote/info", timeout=3) as r:
            info = json.loads(r.read())
    except Exception:  # noqa: BLE001
        print(f"Phone remote: http://<this PC's IP>:{server.rsplit(':', 1)[-1]}/remote")
        return
    print("PHONE REMOTE (open on the phone, same Wi-Fi - or press H on the dashboard for the QR code):")
    for u in (info.get("lan") or [info.get("url")])[:2]:
        print(f"  {u}")
    for u in info.get("tailscale") or []:
        print(f"  {u}   (Tailscale)")
    if info.get("local_only"):
        print(f"  NOTE: {info.get('note')}")


def _server_mode(url: str) -> Optional[str]:
    try:
        with urllib.request.urlopen(url.rstrip("/") + "/api/health", timeout=1.5) as r:
            return json.loads(r.read()).get("mode")
    except Exception:  # noqa: BLE001
        return None


def start_server(url: str, mode: Optional[str]) -> Optional[subprocess.Popen]:
    """Start main.py in the background unless a dashboard server already answers."""
    if _server_up(url):
        running = _server_mode(url)
        print(f"Dashboard server already running ({running} mode) - using it.")
        if mode and mode != "auto" and running and running != mode:
            print(f"  WARNING: it runs in {running.upper()} mode, not {mode.upper()} - switch it with the MODE "
                  "selector on the dashboard (no restart needed).")
        return None
    cmd = [sys.executable, str(ROOT / "main.py")] + ([f"--{mode}"] if mode else [])
    log_path = ROOT / "data" / "server.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    flags = 0x08000000 if platform.system() == "Windows" else 0   # CREATE_NO_WINDOW
    print(f"Starting dashboard server ({mode or 'mode from config'}), log: {log_path}")
    proc = subprocess.Popen(cmd, cwd=str(ROOT), stdout=open(log_path, "w", encoding="utf-8"),
                            stderr=subprocess.STDOUT, creationflags=flags,
                            env={**os.environ, "PYTHONUNBUFFERED": "1"})      # log lines reach the file at once
    for _ in range(60):
        if proc.poll() is not None:
            sys.exit(f"Dashboard server exited - see {log_path}")
        if _server_up(url):
            threading.Thread(target=follow_server_log, args=(log_path,), name="server-log", daemon=True).start()
            return proc
        time.sleep(0.5)
    proc.terminate()
    sys.exit(f"Dashboard server did not start within 30 s - see {log_path}")


def main() -> None:
    cfg = load_config()
    voyo = cfg.get("voyo") or {}
    port = cfg["server"].get("port", 8080)

    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--server", default=f"http://127.0.0.1:{port}", help="dashboard URL")
    ap.add_argument("--start-server", action="store_true",
                    help="start the dashboard server (main.py) too, and stop it again at the end")
    ap.add_argument("--mode", choices=["auto", "live", "test", "replay", "vod"],
                    help="server mode for --start-server (vod = follow a VOYO recording)")
    ap.add_argument("--voyo-url", default=voyo.get("url") or "https://voyo.si/")
    ap.add_argument("--browser", help="path to Edge/Chrome executable")
    ap.add_argument("--token", default=cfg["remote"].get("token", ""), help="remote token, if configured")
    ap.add_argument("--attach", action="store_true", help="do not start browsers, only run the agent")
    ap.add_argument("--no-agent", action="store_true", help="only start the browsers")
    ap.add_argument("--keep-open", action="store_true",
                    help="do not close the dashboard/VOYO windows when the launcher stops (and vice versa)")
    ap.add_argument("--windowed", action="store_true", help="do not start the dashboard full screen")
    ap.add_argument("--titlebar", type=int, default=32,
                    help="height of VOYO's window title bar in DIP, hidden above the slot (0 = keep visible)")
    ap.add_argument("--title", default="VOYO", help="text in the VOYO window title used to find it")
    ap.add_argument("--hotkeys", default="T,H,I,A,1,2,3,4,5,UP,DOWN,S,R,D,Y,L,C,O,N,G,EQUAL,MINUS",
                    help="keys forwarded to the dashboard while the VOYO window has the focus")
    ap.add_argument("--keep-taskbar", action="store_true", help="do not hide the Windows taskbar")
    sync = cfg.get("sync") or {}
    ap.add_argument("--no-clock", action="store_true",
                    help="do not read VOYO's playback clock (no DevTools port; the server uses a fixed delay)")
    ap.add_argument("--cdp-port", type=int, default=int(sync.get("cdp_port", 9223)),
                    help="local DevTools port of the VOYO window (127.0.0.1 only)")
    ap.add_argument("--restore-taskbar", action="store_true",
                    help="only show the Windows taskbar again (if the agent was killed) and exit")
    ap.add_argument("--capture", default=str(voyo.get("capture_compat") or "off"),
                    help="screen-capture compatibility of the VOYO window for AirParrot / Miracast mirroring: "
                         "off | no-overlays | no-hw-decode | no-gpu (or 0-3); default from [voyo] capture_compat")
    ap.add_argument("--print", action="store_true", help="print the browser commands and exit")
    args = ap.parse_args()
    args.server = args.server.rstrip("/").replace("://localhost", "://127.0.0.1")
    use_clock = not args.no_clock and bool(sync.get("enabled", True)) and bool(sync.get("voyo_playback_clock", True))

    hotkeys = [k.strip().upper() for k in args.hotkeys.split(",") if k.strip()]
    ops = make_ops(hotkeys) if not args.print else NullOps()
    if args.restore_taskbar:
        ops.set_taskbar_hidden(False)
        print("Taskbar shown again.")
        return
    session = Session(ops)
    session.close_browsers = not args.keep_open
    atexit.register(session.shutdown, "exit")

    if platform.system() == "Windows" and not args.print:
        import ctypes
        handler_type = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_uint)

        def _on_console_event(event):
            if event in (2, 5, 6):          # console window closed / logoff / shutdown
                session.shutdown("console window closed")
                return True
            return False                    # Ctrl+C / Ctrl+Break -> KeyboardInterrupt below
        main.console_handler = handler_type(_on_console_event)   # keep a reference
        ctypes.windll.kernel32.SetConsoleCtrlHandler(main.console_handler, True)

    if args.start_server and not args.print:
        mode = args.mode
        if mode is None and str((cfg.get("source") or {}).get("mode", "auto")).lower() in ("live", "auto"):
            det, why = choose_mode()
            mode = "auto"
            print(f"Mode: AUTO - detected {det.upper()} ({why}). The dashboard keeps detecting it; switch with "
                  "the MODE selector (AUTO / LIVE / VOD) at the top right, or start with launch.bat live / vod")
        session.server_proc = start_server(args.server, mode)

    sw, sh = ops.screen_size()
    rects = slot_rects(sw, sh)
    voyo_pid = dash_pid = None
    if not args.attach:
        browser = args.browser or find_browser()
        if not browser:
            sys.exit("No Edge/Chrome found - pass --browser <path>")
        # initial position in DIP for the browser flags; the agent corrects it right away
        x, y, w, h = (round(v / ops.dpi_scale) for v in rects["RACE_VIEW"])
        profiles = ROOT / "data" / "browser-profiles"
        common = ["--no-first-run", "--no-default-browser-check", "--disable-session-crashed-bubble"]
        dash_cmd = [browser, f"--user-data-dir={profiles / 'dashboard'}", f"--app={args.server}",
                    "--autoplay-policy=no-user-gesture-required", *common]
        if not args.windowed:
            dash_cmd.append("--start-fullscreen")
        # --disable-backgrounding-occluded-windows: Windows' occlusion tracking would treat
        # the VOYO window as hidden while the full-screen dashboard covers it (FULL_DASHBOARD)
        # and the browser would then pause muted / video-only playback. Only the browser's
        # own background throttling - nothing of VOYO's player, login or DRM.
        voyo_cmd = [browser, f"--user-data-dir={profiles / 'voyo'}", f"--app={args.voyo_url}",
                    f"--window-position={x},{y}", f"--window-size={w},{h}",
                    "--disable-backgrounding-occluded-windows", *capture_flags(args.capture), *common]
        if capture_flags(args.capture):
            print(f"VOYO window screen-capture mode: {args.capture} ({' '.join(capture_flags(args.capture))}). "
                  "Close the VOYO window completely before switching modes - flags apply on a fresh start.")
        if use_clock:
            # local DevTools port of the dedicated VOYO profile: only used to READ the
            # <video> element's currentTime / paused / playbackRate (tools/voyo_clock_probe.js)
            voyo_cmd.append(f"--remote-debugging-port={args.cdp_port}")
        if args.print:
            print(subprocess.list2cmdline(dash_cmd))
            print(subprocess.list2cmdline(voyo_cmd))
            return
        dash_pid = subprocess.Popen(dash_cmd).pid
        time.sleep(4)
        reset_window_placement(profiles / "voyo")
        voyo_pid = subprocess.Popen(voyo_cmd).pid
        session.browser_pids = [dash_pid, voyo_pid]
        print("Browsers started. First time: log in to VOYO in its window and open the F1 stream.")
        print_phone_remote(args.server)

    if args.no_agent:
        atexit.unregister(session.shutdown)
        return
    if type(ops) is NullOps:
        print("Window control is not available on this system (Windows, or Linux with wmctrl, needed).")
        return
    agent = TvAgent(ops, args.server, args.token, args.titlebar, args.title, voyo_pid)
    agent.dash_pid = dash_pid
    agent.hide_taskbar = not args.keep_taskbar
    agent.close_together = not args.keep_open
    session.agent = agent
    if use_clock:
        from tools.voyo_clock import VoyoClockBridge
        session.clock = VoyoClockBridge(args.server, args.cdp_port, args.token,
                                        float(sync.get("clock_poll_hz", 5.0))).start()
        agent.title_hints = lambda: [session.clock.page_title] if session.clock and session.clock.page_title else []
        agent.typing = lambda: bool(session.clock and session.clock.typing)
    reason = "Ctrl+C"
    try:
        agent.run()
        reason = agent.stop_reason or "agent stopped"
    except KeyboardInterrupt:
        pass
    finally:
        ops.poll_hotkeys(False)
        session.shutdown(reason)


if __name__ == "__main__":
    main()
