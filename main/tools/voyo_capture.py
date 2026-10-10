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
import json
import platform
import re
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
                 live_dir: Optional[Path] = None,
                 override: Optional[Callable[[], Optional[dict]]] = None) -> None:
        """``override``: the server VOYO player's session recorder decides itself (tools/voyo_session.py):
        it returns {"instance": package key, ...} while its recording runs (None = not). Then the capture does
        NOT stop when the dashboard server is briefly unreachable (a restart / update of it) - the segments
        wait in the spool and are uploaded when it is back. Only a FRESH server answer that blocks the
        capture (disk full, recording off) stops it."""
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
        self.override = override
        self.retry_s = 5.0 if override else 30.0       # supervised: every second of a session counts
        self.spool_min_free = int(self.rc.get("spool_min_free_bytes", 2 * 1024 ** 3))
        self.err_log: Optional[Path] = None
        self.last_error = ""
        self._grow = (None, 0, 0.0)                    # (file, size, monotonic time it last grew)
        self.uploads_ok = 0
        self.uploads_failed = 0
        self.last_segment: Optional[dict] = None       # the newest finished segment's check (streams, sound)
        self.audio_failed = 0                          # ffmpeg could not open the sound this often in a row
        self._checked: dict[str, dict] = {}            # segment name -> check_segment result (until uploaded)
        self.failures = 0                              # how often ffmpeg stopped by itself

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
        fresh = rec is not None and time.monotonic() - at <= 15
        if self.override is not None:
            want = self.override()
            if not want:
                return None
            if fresh and rec.get("blocked"):           # the server says no (disk full / recording off)
                self._once(f"blocked:{rec['blocked']}", f"  VOYO capture: the server does not take video now "
                                                         f"({rec['blocked']}) - stopped")
                return None
            self._said = {k for k in self._said if not k.startswith("blocked:")}
            return {**(rec if fresh else {}), **want, "capture": True}
        if not fresh or not rec.get("capture") or not rec.get("instance"):
            return None
        return rec

    def health(self) -> dict:
        """The recorder now (the session recorder's checks, /disk): running, the file growing, uploads."""
        alive = self.proc is not None and self.proc.poll() is None
        f, size, grew = self._grow
        return {"alive": alive, "instance": self.instance, "run": self.run_id,
                "grew_age_s": round(time.monotonic() - grew, 1) if alive and grew else None,
                "file_bytes": size if alive else None, "uploads_ok": self.uploads_ok,
                "uploads_failed": self.uploads_failed, "pending": self._pending_count(),
                "encoder": self.used_encoder, "last_error": self.last_error or None, "failures": self.failures,
                "last_segment": self.last_segment}

    def restart(self, why: str = "") -> None:
        """Stop a recorder that hangs (its file does not grow); the next step starts a new run at once."""
        if self.proc is not None:
            self.log(f"  VOYO capture: restarting the recorder{' (' + why + ')' if why else ''}")
            self._stop_ffmpeg()
            self._retry_at = 0.0

    def _pending_count(self) -> int:
        try:
            return sum(1 for _ in self.spool.glob("*/*_seg_*.mp4")) if self.spool.exists() else 0
        except OSError:
            return 0

    def _track_growth(self) -> None:
        """The newest segment file of the running recorder: when did it last grow?"""
        if self.proc is None or self.instance is None or not self.run_id:
            return
        d = self.spool / self.instance
        try:
            files = sorted(d.glob(f"{self.run_id}_seg_*.mp4"))
            f = files[-1] if files else None
            size = f.stat().st_size if f else 0
        except OSError:
            return
        old_f, old_size, at = self._grow
        if f != old_f or size != old_size or not at:
            self._grow = (f, size, time.monotonic())

    def _spool_free(self) -> Optional[int]:
        try:
            self.spool.mkdir(parents=True, exist_ok=True)
            return shutil.disk_usage(self.spool).free
        except OSError:
            return None

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
        free = self._spool_free() if iid else None
        if iid and free is not None and free < self.spool_min_free:
            self._once("spoolfull", f"  VOYO capture: less than {self.spool_min_free / 1e9:.0f} GB free for the spool "
                                    f"({self.spool}) - not recording until the segments are uploaded")
            self.last_error = "spool disk full"
            iid = None
        else:
            self._said.discard("spoolfull")
        if self.proc is not None and (iid != self.instance or self.proc.poll() is not None):
            if self.proc.poll() is not None and iid == self.instance:
                err = self._err_tail()
                self.last_error = f"ffmpeg stopped ({self.proc.returncode}) {err}".strip()[:300]
                self.failures += 1
                sound = bool(re.search(r"monitor|pulse|dshow|audio", err, re.I)) and time.time() - self.run_start < 20
                if sound:
                    self.audio_failed += 1             # the sound input does not open: after 2 tries, video only
                    if self.audio_failed == 2:
                        self.log("  VOYO capture: the sound cannot be recorded - recording the picture WITHOUT sound")
                if self.used_encoder == "vaapi" and not sound and time.time() - self.run_start < 20:
                    self.vaapi_failed = True        # e.g. no permission on /dev/dri, driver missing
                    self.log(f"  VOYO capture: Intel Quick Sync (vaapi) failed: {err} - recording with x264 "
                             "(CPU) instead")
                else:
                    # supervised: a recorder that ran for a while (> 10 s) is started again at once (each second is video);
                    # one that keeps failing right away waits longer each time (5, 10, 20, 30 s)
                    ran = time.time() - self.run_start
                    if self.override is not None:
                        self._quick_fails = 0 if ran > 10 else getattr(self, "_quick_fails", 0) + 1
                        wait = (1.0, 5.0, 10.0, 30.0)[min(self._quick_fails, 3)]
                    else:
                        wait = self.retry_s
                    self.log(f"  VOYO capture: ffmpeg stopped ({self.proc.returncode}) {err} - retrying in {wait:.0f} s")
                    self._retry_at = time.monotonic() + wait
            self._stop_ffmpeg()
        if iid and self.proc is None and time.monotonic() >= self._retry_at:
            self._start_ffmpeg(iid, want)
        self._track_growth()
        self.upload_pending()

    def _err_tail(self) -> str:
        """The end of ffmpeg's own error output (a file, not a pipe: a pipe nobody reads fills up after
        64 KB and then BLOCKS ffmpeg - the recording would silently stop growing)."""
        try:
            return self.err_log.read_bytes()[-400:].decode(errors="replace").strip() if self.err_log else ""
        except OSError:
            return ""

    def _start_ffmpeg(self, iid: str, want: dict) -> None:
        if not self.ffmpeg:
            self._once("noffmpeg", "  VOYO capture: the server asks for the window capture "
                                   "([voyo.recording] record_video_capture) but ffmpeg was not found - install it "
                                   "and/or set [voyo.recording] ffmpeg = \"C:/ffmpeg/bin/ffmpeg.exe\"")
            return
        spec = self.window()
        if spec and self.audio_failed >= 2:
            spec = {**spec, "audio": ""}
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
        self.err_log = out / f"{self.run_id}_ffmpeg.log"
        with open(self.err_log, "ab") as errf:
            self.proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=errf,
                                         creationflags=flags)
        self._grow = (None, 0, time.monotonic())
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
        """Segments ffmpeg has finished (listed in a <run>_list.csv), with their PC start / end epoch - and the
        last segment of a run that ended without finishing it (ffmpeg killed / crashed / the machine stopped):
        its complete fragments are video too (up to a whole segment), uploaded with its measured length."""
        out = []
        ends: dict[str, float] = {}
        for lst in d.glob("*_list.csv"):
            run = lst.name[:-len("_list.csv")]
            t0 = run_epoch(run)
            ends.setdefault(run, t0)
            try:
                rows = list(csv.reader(lst.read_text(encoding="utf-8").splitlines()))
            except OSError:
                continue
            for row in rows:
                if len(row) < 3:
                    continue
                try:
                    a, b = t0 + float(row[1]), t0 + float(row[2])
                except ValueError:
                    continue
                ends[run] = max(ends[run], b)               # also of segments already uploaded (and deleted)
                if (d / row[0]).exists():
                    out.append((d / row[0], a, b))
        listed = {p.name for p, _a, _b in out}
        running = self.proc is not None and self.proc.poll() is None and self.instance == d.name
        for f in sorted(d.glob("*_seg_*.mp4")):
            run = f.name.split("_seg_")[0]
            if f.name in listed or (running and run == self.run_id):
                continue
            chk = self._checked.get(f.name) or check_segment(f, self.ffmpeg)
            self._checked[f.name] = chk
            if not chk.get("duration"):
                if f.stat().st_size < 4096:                 # nothing in it (killed at once): no video to keep
                    f.unlink(missing_ok=True)
                continue
            start = ends.get(run, run_epoch(run))
            out.append((f, start, start + float(chk["duration"])))
            ends[run] = start + float(chk["duration"])
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
                for f in [*d.glob("*_list.csv"), *d.glob("*_ffmpeg.log")]:
                    f.unlink(missing_ok=True)
                try:
                    d.rmdir()
                except OSError:
                    pass

    def _upload(self, iid: str, seg: Path, t0: float, t1: float) -> bool:
        size = seg.stat().st_size
        chk = self._checked.get(seg.name)
        if chk is None:
            chk = check_segment(seg, self.ffmpeg)
            self._checked[seg.name] = chk
            self.last_segment = {"name": seg.name, **chk}
            if chk.get("audio") is False or chk.get("silent"):
                self.log(f"  VOYO capture: {seg.name} has {'no sound track' if chk.get('audio') is False else 'only silence'}")
        extra = {"X-Capture-Duration": f"{chk['duration']:.3f}" if chk.get("duration") is not None else None,
                 "X-Capture-Audio": None if chk.get("audio") is None else ("1" if chk["audio"] else "0"),
                 "X-Capture-Video": None if chk.get("video") is None else ("1" if chk["video"] else "0"),
                 "X-Capture-Audio-Max-Db": f"{chk['max_db']:.1f}" if chk.get("max_db") is not None else None}
        req = urllib.request.Request(f"{self.server}/api/voyo/recordings/{iid}/capture/{seg.name}", method="PUT",
                                     data=open(seg, "rb"), headers={
                                         "Content-Type": "video/mp4", "Content-Length": str(size),
                                         "X-Capture-Start": f"{t0:.3f}", "X-Capture-End": f"{t1:.3f}",
                                         "X-PC-Now": f"{time.time():.3f}",
                                         **{k: v for k, v in extra.items() if v is not None}})
        if self.token:
            req.add_header("X-Remote-Token", self.token)
        try:
            with urllib.request.urlopen(req, timeout=max(30, size / 2e6)) as r:
                r.read()
        except urllib.error.HTTPError as exc:
            self.uploads_failed += 1
            self._once(f"http{exc.code}:{iid}", f"  VOYO capture: upload of {seg.name} refused ({exc.code}: "
                                                f"{exc.read()[:120]!r}) - kept in {seg.parent}")
            return False
        except OSError as exc:
            self.uploads_failed += 1
            self._once(f"net:{iid}", f"  VOYO capture: server not reachable for uploads ({exc}) - retrying")
            return False
        finally:
            req.data.close()
        seg.unlink(missing_ok=True)
        self.uploads_ok += 1
        self._checked.pop(seg.name, None)
        return True

    def stop(self) -> None:
        self.stop_ev.set()
        if self.thread is not None:
            self.thread.join(50)


SILENT_DB = -70.0          # a segment whose loudest sample is below this has no real sound


def run_epoch(run: str) -> float:
    """run20261010-124539 -> its local start time (epoch s); 0 when the name is not one."""
    try:
        return time.mktime(time.strptime(run, "run%Y%m%d-%H%M%S"))
    except ValueError:
        return 0.0


def check_segment(seg: Path, ffmpeg: Optional[str]) -> dict:
    """A finished segment: its length, whether it has a picture and a sound track, and whether the sound is
    silence (ffprobe + ffmpeg volumedetect on the sound only - a fraction of a second for a minute).
    -> {"duration", "video", "audio", "max_db", "silent"} - None where it could not be told."""
    out: dict = {"duration": None, "video": None, "audio": None, "max_db": None, "silent": None}
    probe = shutil.which("ffprobe")
    if not probe and ffmpeg and Path(ffmpeg).with_name("ffprobe").exists():
        probe = str(Path(ffmpeg).with_name("ffprobe"))
    if probe:
        try:
            r = subprocess.run([probe, "-v", "error", "-show_entries", "format=duration:stream=codec_type", "-of", "json",
                                str(seg)], capture_output=True, text=True, timeout=30)
            j = json.loads(r.stdout or "{}")
            types = {s.get("codec_type") for s in j.get("streams") or []}
            out["video"], out["audio"] = "video" in types, "audio" in types
            out["duration"] = float((j.get("format") or {}).get("duration") or 0) or None
        except (OSError, ValueError, subprocess.SubprocessError):
            pass
    if ffmpeg and out["audio"]:
        try:
            r = subprocess.run([ffmpeg, "-hide_banner", "-nostats", "-i", str(seg), "-map", "0:a:0", "-af", "volumedetect",
                                "-f", "null", "-"], capture_output=True, text=True, timeout=60)
            m = re.search(r"max_volume:\s*(-?[0-9.]+|-inf) dB", r.stderr or "")
            if m:
                out["max_db"] = -200.0 if m.group(1) == "-inf" else float(m.group(1))
                out["silent"] = out["max_db"] < SILENT_DB
        except (OSError, subprocess.SubprocessError):
            pass
    return out
