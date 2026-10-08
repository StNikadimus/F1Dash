#!/usr/bin/env python3
"""OPT-IN window capture of the VOYO player window - for the VOYO stream recordings on the server.

Off unless the SERVER's ``[voyo.recording] record_video_capture = true``: the server answers every
VOYO clock sample (tools/voyo_clock.py -> POST /api/sync/voyo) with the open stream recording
(``stream_instance_id``) and whether to capture it. This module then runs ffmpeg on THIS PC (the one
that shows VOYO) as a plain screen recording of the VOYO window you are watching:

* Windows: ``gdigrab`` of the window by its title; Linux/X11: ``x11grab`` of the window id.
* It records what the screen shows. It never reads VOYO's stream, its buffers or its DRM; if the
  browser / OS blanks protected video in screen captures, the recording is black - not worked around.
* Segments (fragmented MP4, ``capture_segment_seconds`` long) are spooled in
  data/voyo_capture_spool/<stream_instance_id>/ and uploaded when finished to
  ``PUT /api/voyo/recordings/<id>/capture/<name>`` - the server stores them in that package's
  capture/ folder ([voyo.recording] path). Uploaded segments are deleted here; failed uploads are
  retried (also after a restart of the launcher).
* A new stream instance on the server = ffmpeg restarts into the new package.

For your own use - check VOYO's terms of service.
"""
from __future__ import annotations

import csv
import platform
import shutil
import subprocess
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Callable, Optional


def find_vaapi_device(configured: str = "") -> Optional[str]:
    """The render node of an Intel GPU (Quick Sync) for ffmpeg's h264_vaapi: the configured one, else
    the /dev/dri/renderD* whose PCI vendor is Intel (0x8086) - a laptop's NVIDIA / nouveau node is
    skipped. None when there is none (Windows, a VM / container without the GPU)."""
    if configured:
        return configured if Path(configured).exists() else None
    nodes = sorted(Path("/dev/dri").glob("renderD*")) if Path("/dev/dri").is_dir() else []
    unknown = []
    for node in nodes:
        try:
            vendor = (Path("/sys/class/drm") / node.name / "device" / "vendor").read_text().strip().lower()
        except OSError:
            vendor = ""
        if vendor == "0x8086":
            return str(node)
        if not vendor:
            unknown.append(node)
    # a container that was given only the Intel node may not see its sysfs entry: that one node
    return str(unknown[0]) if len(unknown) == 1 else None


def ffmpeg_cmd(ffmpeg: str, spec: dict, out_dir: Path, run: str, fps: int = 30, crf: int = 23,
               segment_s: int = 60, audio: "str | dict" = "", encoder: str = "x264", preset: str = "veryfast",
               vaapi_device: Optional[str] = None, live_dir: Optional[Path] = None) -> list[str]:
    """ffmpeg command for one capture run of the window ``spec`` ({"title": ...} on Windows,
    {"window_id": "0x..."} on X11) into fragmented-MP4 segments listed in <run>_list.csv.
    ``encoder``: "x264" (CPU) or "vaapi" (Intel Quick Sync on ``vaapi_device`` - the GPU encodes and
    converts the picture, the CPU only grabs it). ``live_dir``: the same encoded picture also goes out as a
    live HLS stream (2 s pieces, the newest 8 kept, each with its capture time) - ffmpeg's tee, no second
    encode - for the /tv page."""
    vaapi = encoder == "vaapi" and bool(vaapi_device)
    cmd = [ffmpeg, "-hide_banner", "-loglevel", "error"]          # stdin stays open: "q" stops it cleanly
    if vaapi:
        cmd += ["-vaapi_device", str(vaapi_device)]
    if spec.get("title"):
        cmd += ["-f", "gdigrab", "-framerate", str(fps), "-draw_mouse", "0", "-i", f"title={spec['title']}"]
    elif spec.get("window_id"):
        cmd += ["-f", "x11grab", "-framerate", str(fps), "-draw_mouse", "0", "-window_id", str(int(str(spec["window_id"]), 0)),
                "-i", spec.get("display") or ":0"]
    else:
        raise ValueError("no window to capture")
    if isinstance(audio, dict) and audio.get("format") == "pulse":
        cmd += ["-thread_queue_size", "1024", "-f", "pulse", "-i", str(audio.get("device") or "default")]
    elif audio:
        cmd += ["-f", "dshow", "-i", f"audio={audio}"]
    # a keyframe at every segment boundary: segments can only be cut there (live: every 2 s, the HLS pieces;
    # the recording's segment_s is then a multiple of 2)
    kf = 2 if live_dir is not None else int(segment_s)
    keys = ["-force_key_frames", f"expr:gte(t,n_forced*{kf})", "-g", str(int(fps) * kf)]
    if vaapi:
        cmd += ["-vf", "format=bgr0,hwupload,scale_vaapi=format=nv12", "-c:v", "h264_vaapi",
                "-qp", str(crf), *keys]
    else:
        cmd += ["-c:v", "libx264", "-preset", str(preset or "veryfast"), "-crf", str(crf), "-pix_fmt", "yuv420p",
                *keys, "-vf", "pad=ceil(iw/2)*2:ceil(ih/2)*2"]
    if audio:
        cmd += ["-c:a", "aac", "-b:a", "160k"]
    if live_dir is None:
        cmd += ["-f", "segment", "-segment_time", str(segment_s), "-reset_timestamps", "1",
                "-segment_list", str(out_dir / f"{run}_list.csv"), "-segment_list_type", "csv",
                "-segment_format", "mp4",
                "-segment_format_options", "movflags=+frag_keyframe+empty_moov+default_base_moof",
                str(out_dir / f"{run}_seg_%05d.mp4")]
        return cmd
    seg_s = max(2, int(segment_s) // 2 * 2)
    rec = (f"[f=segment:segment_time={seg_s}:reset_timestamps=1:segment_list={out_dir / f'{run}_list.csv'}:"
           f"segment_list_type=csv:segment_format=mp4:"
           f"segment_format_options=movflags=+frag_keyframe+empty_moov+default_base_moof]{out_dir / f'{run}_seg_%05d.mp4'}")
    live = (f"[f=hls:hls_time=2:hls_list_size=8:hls_flags=delete_segments+program_date_time+independent_segments"
            f"+temp_file:hls_segment_filename={Path(live_dir) / 'live_%05d.ts'}]{Path(live_dir) / 'index.m3u8'}")
    cmd += ["-map", "0:v"] + (["-map", "1:a"] if audio else []) + ["-flags", "+global_header",
                                                                  "-f", "tee", f"{rec}|{live}"]
    return cmd


class VoyoWindowCapture:
    def __init__(self, server: str, token: str, rc: dict, spool: Path,
                 window: Callable[[], Optional[dict]], log: Callable[[str], None] = print,
                 live_dir: Optional[Path] = None) -> None:
        self.server = server.rstrip("/").replace("://localhost", "://127.0.0.1")
        self.token = token
        self.rc = rc or {}
        self.spool = spool
        self.window = window
        self.log = log
        self.ffmpeg = shutil.which(str(self.rc.get("ffmpeg") or "ffmpeg")) or \
            (str(self.rc.get("ffmpeg")) if Path(str(self.rc.get("ffmpeg") or "")).is_file() else None)
        self.stop_ev = threading.Event()
        self._lock = threading.Lock()
        self._reply: tuple[Optional[dict], float] = (None, 0.0)
        self.proc: Optional[subprocess.Popen] = None
        self.instance: Optional[str] = None
        self.run_id: Optional[str] = None
        self.run_start = 0.0
        self.used_encoder = "x264"
        self._retry_at = 0.0
        self._said: set[str] = set()
        self.thread: Optional[threading.Thread] = None
        # Intel Quick Sync: [voyo.recording] capture_encoder = "vaapi" (falls back to x264 if it fails)
        self.encoder = str(self.rc.get("capture_encoder") or "x264").lower()
        self.live_dir = live_dir                       # server VOYO player: also a live HLS stream (/tv)
        self.vaapi_failed = False

    def _once(self, key: str, text: str) -> None:
        if key not in self._said:
            self._said.add(key)
            self.log(text)

    def on_reply(self, rec: Optional[dict]) -> None:
        """The server's answer to a clock sample: {"instance": id, "capture": bool, ...}."""
        with self._lock:
            self._reply = (rec if isinstance(rec, dict) else None, time.monotonic())

    def wanted(self) -> Optional[dict]:
        with self._lock:
            rec, at = self._reply
        if rec is None or not rec.get("capture") or not rec.get("instance") or time.monotonic() - at > 15:
            return None
        return rec

    # ------------------------------------------------------------------
    def start(self) -> "VoyoWindowCapture":
        self.thread = threading.Thread(target=self.run, name="voyo-capture", daemon=True)
        self.thread.start()
        return self

    def run(self) -> None:
        while not self.stop_ev.is_set():
            try:
                self.step()
            except Exception as exc:  # noqa: BLE001
                self._once(f"err:{exc}", f"  VOYO capture: {exc}")
            self.stop_ev.wait(1.0)
        self._stop_ffmpeg()
        self.upload_pending(deadline=time.monotonic() + 30)

    def step(self) -> None:
        want = self.wanted()
        iid = want.get("instance") if want else None
        if self.proc is not None and (iid != self.instance or self.proc.poll() is not None):
            if self.proc.poll() is not None and iid == self.instance:
                err = (self.proc.stderr.read() if self.proc.stderr else b"")[-400:].decode(errors="replace")
                if self.used_encoder == "vaapi" and time.time() - self.run_start < 20:
                    self.vaapi_failed = True        # e.g. no permission on /dev/dri, driver missing
                    self.log(f"  VOYO capture: Intel Quick Sync (vaapi) failed: {err.strip()} - recording with x264 "
                             "(CPU) instead")
                else:
                    self.log(f"  VOYO capture: ffmpeg stopped ({self.proc.returncode}) {err.strip()} - retrying in 30 s")
                    self._retry_at = time.monotonic() + 30
            self._stop_ffmpeg()
        if iid and self.proc is None and time.monotonic() >= self._retry_at:
            self._start_ffmpeg(iid, want)
        self.upload_pending()

    def _start_ffmpeg(self, iid: str, want: dict) -> None:
        if not self.ffmpeg:
            self._once("noffmpeg", "  VOYO capture: the server asks for the window capture "
                                   "([voyo.recording] record_video_capture) but ffmpeg was not found - install it "
                                   "and/or set [voyo.recording] ffmpeg = \"C:/ffmpeg/bin/ffmpeg.exe\"")
            return
        spec = self.window()
        if not spec:
            self._once("nowin", "  VOYO capture: VOYO window not found yet - waiting")
            return
        self._said.discard("nowin")
        out = self.spool / iid
        out.mkdir(parents=True, exist_ok=True)
        self.run_id = time.strftime("run%Y%m%d-%H%M%S")
        enc, dev = "x264", None
        if self.encoder == "vaapi" and not self.vaapi_failed:
            dev = find_vaapi_device(str(self.rc.get("capture_vaapi_device") or ""))
            if dev:
                enc = "vaapi"
            else:
                self._once("novaapi", "  VOYO capture: capture_encoder = vaapi but no Intel GPU render node "
                                      "(/dev/dri/renderD*) is available here - recording with x264 (CPU)")
        self.used_encoder = enc
        cmd = ffmpeg_cmd(self.ffmpeg, spec, out, self.run_id, int(want.get("fps") or self.rc.get("capture_fps", 30)),
                         int(want.get("crf") or self.rc.get("capture_crf", 23)),
                         int(want.get("segment_seconds") or self.rc.get("capture_segment_seconds", 60)),
                         spec.get("audio") or str(self.rc.get("capture_audio_device") or ""),
                         encoder=enc, preset=str(self.rc.get("capture_preset") or "veryfast"), vaapi_device=dev,
                         live_dir=self._live_reset())
        flags = subprocess.CREATE_NO_WINDOW if platform.system() == "Windows" else 0
        self.run_start = time.time()
        self.proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                                     creationflags=flags)
        self.instance = iid
        self.log(f"  VOYO capture: recording the VOYO window for stream {iid} (opt-in window capture, "
                 f"{'Intel Quick Sync ' + dev if enc == 'vaapi' else 'x264 CPU'}) -> {self.server}")

    def _live_reset(self) -> Optional[Path]:
        """An empty live folder for a new run (the /tv page sees "off air" until the first piece)."""
        if self.live_dir is None:
            return None
        self.live_dir.mkdir(parents=True, exist_ok=True)
        for f in self.live_dir.iterdir():
            if f.is_file() and (f.suffix in (".ts", ".m3u8", ".tmp") or f.name.endswith(".m3u8.tmp")):
                f.unlink(missing_ok=True)
        return self.live_dir

    def _stop_ffmpeg(self) -> None:
        p, self.proc = self.proc, None
        if p is None:
            return
        if p.poll() is None:
            try:
                p.stdin.write(b"q")                 # ffmpeg finishes the current segment cleanly
                p.stdin.flush()
                p.wait(15)
            except Exception:  # noqa: BLE001
                p.terminate()
                try:
                    p.wait(5)
                except subprocess.TimeoutExpired:
                    p.kill()
        if self.live_dir is not None:
            (self.live_dir / "index.m3u8").unlink(missing_ok=True)      # off air
        self.log(f"  VOYO capture: stopped for stream {self.instance}")

    # ------------------------------------------------------------------ upload
    def _finished(self, d: Path) -> list[tuple[Path, float, float]]:
        """Segments ffmpeg has finished (listed in a <run>_list.csv), with their PC start / end epoch."""
        out = []
        for lst in d.glob("*_list.csv"):
            run = lst.name[:-len("_list.csv")]
            try:
                t0 = time.mktime(time.strptime(run, "run%Y%m%d-%H%M%S"))
            except ValueError:
                t0 = 0.0
            try:
                rows = list(csv.reader(lst.read_text(encoding="utf-8").splitlines()))
            except OSError:
                continue
            for row in rows:
                if len(row) >= 3 and (d / row[0]).exists():
                    try:
                        out.append((d / row[0], t0 + float(row[1]), t0 + float(row[2])))
                    except ValueError:
                        continue
        return out

    def upload_pending(self, deadline: Optional[float] = None) -> None:
        if not self.spool.exists():
            return
        for d in [x for x in self.spool.iterdir() if x.is_dir()]:
            for seg, t0, t1 in self._finished(d):
                if deadline is not None and time.monotonic() > deadline:
                    return
                self._upload(d.name, seg, t0, t1)
            if not any(d.glob("*.mp4")) and (self.proc is None or self.instance != d.name):
                for f in d.glob("*_list.csv"):
                    f.unlink(missing_ok=True)
                try:
                    d.rmdir()
                except OSError:
                    pass

    def _upload(self, iid: str, seg: Path, t0: float, t1: float) -> bool:
        size = seg.stat().st_size
        req = urllib.request.Request(f"{self.server}/api/voyo/recordings/{iid}/capture/{seg.name}", method="PUT",
                                     data=open(seg, "rb"), headers={
                                         "Content-Type": "video/mp4", "Content-Length": str(size),
                                         "X-Capture-Start": f"{t0:.3f}", "X-Capture-End": f"{t1:.3f}",
                                         "X-PC-Now": f"{time.time():.3f}"})
        if self.token:
            req.add_header("X-Remote-Token", self.token)
        try:
            with urllib.request.urlopen(req, timeout=max(30, size / 2e6)) as r:
                r.read()
        except urllib.error.HTTPError as exc:
            self._once(f"http{exc.code}:{iid}", f"  VOYO capture: upload of {seg.name} refused ({exc.code}: "
                                                f"{exc.read()[:120]!r}) - kept in {seg.parent}")
            return False
        except OSError as exc:
            self._once(f"net:{iid}", f"  VOYO capture: server not reachable for uploads ({exc}) - retrying")
            return False
        finally:
            req.data.close()
        seg.unlink(missing_ok=True)
        return True

    def stop(self) -> None:
        self.stop_ev.set()
        if self.thread is not None:
            self.thread.join(50)
