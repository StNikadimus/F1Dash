"""REPLAYS for the /tv player: the VOYO stream recordings that have video, played from the disk.

The recorded video is the package's capture/ folder ([voyo.recording] path, server/voyo_recording.py):
fragmented-MP4 segments written by ffmpeg (tools/voyo_capture.py), each one ``ftyp`` + ``moov`` (the init
part) + ``moof``/``mdat`` fragments, every segment starting again at 0. The browser plays a recording
as one HLS playlist built from those files AS THEY ARE - no remux, no copy, nothing written:

    #EXT-X-MAP:URI="<segment>",BYTERANGE="<init size>@0"      the segment's own init part
    #EXTINF:<seconds>,
    #EXT-X-BYTERANGE:<rest>@<init size>                        its fragments
    <segment>
    #EXT-X-DISCONTINUITY                                       next segment (timestamps start at 0 again)

hls.js (or Safari's native HLS) fetches those byte ranges from GET /tv/replay/<id>/<segment> - only
segments listed in the package, only for an approved /tv page (server/app.py).
"""
from __future__ import annotations

import json
import math
import re
import struct
import threading
from pathlib import Path
from typing import Iterable, Optional

ID_RE = re.compile(r"^[A-Za-z0-9_-]{4,64}$")
SEGMENT_RE = re.compile(r"^[A-Za-z0-9_.-]{1,96}\.mp4$")
MAX_SEGMENTS = 2000                       # 60 s pieces: ~33 h - far more than any session
_init_cache: dict[tuple, Optional[int]] = {}
_lock = threading.Lock()


def init_size(path: Path) -> Optional[int]:
    """Bytes of the init part (everything before the first ``moof``) of a fragmented MP4, or None when the
    file is not one (plain MP4, still being written, damaged). Reads only the box headers."""
    try:
        st = path.stat()
    except OSError:
        return None
    key = (str(path), st.st_size, st.st_mtime_ns)
    with _lock:
        if key in _init_cache:
            return _init_cache[key]
    found: Optional[int] = None
    seen_moov = False
    try:
        with open(path, "rb") as fh:
            pos = 0
            for _ in range(64):                               # ftyp, moov (+ free / sidx) come first
                head = fh.read(8)
                if len(head) < 8:
                    break
                size, typ = struct.unpack(">I4s", head)
                if size == 1:                                 # 64-bit size
                    ext = fh.read(8)
                    if len(ext) < 8:
                        break
                    size = struct.unpack(">Q", ext)[0]
                if size < 8 or pos + size > st.st_size:
                    break
                if typ == b"moov":
                    seen_moov = True
                elif typ == b"moof":
                    found = pos if seen_moov and pos > 0 else None
                    break
                elif typ == b"mdat":                          # media before any fragment: not fragmented
                    break
                pos += size
                fh.seek(pos)
    except OSError:
        found = None
    with _lock:
        if len(_init_cache) > 4096:
            _init_cache.clear()
        _init_cache[key] = found
    return found


def _capture_log(pkg: Path) -> dict[str, dict]:
    """capture/capture.jsonl: name -> {pc_start_epoch, pc_end_epoch, bytes, ...} (as uploaded)."""
    out: dict[str, dict] = {}
    try:
        lines = (pkg / "capture" / "capture.jsonl").read_text(encoding="utf-8").splitlines()
    except OSError:
        return out
    for line in lines:
        try:
            r = json.loads(line)
        except ValueError:
            continue
        if isinstance(r, dict) and isinstance(r.get("name"), str):
            out[r["name"]] = r
    return out


def segments(pkg: Path, default_s: float = 60.0) -> list[dict]:
    """The playable segments of a package, oldest first: [{name, bytes, init, duration, start}].
    Durations come from the capture log (ffmpeg's own segment times); missing ones use ``default_s``."""
    cap = pkg / "capture"
    if not cap.is_dir():
        return []
    info = _capture_log(pkg)
    real = cap.resolve()
    out = []
    for f in sorted(cap.iterdir())[: MAX_SEGMENTS * 2]:
        # only the package's own files: no links (out of the folder, or anywhere else)
        if not SEGMENT_RE.match(f.name) or f.is_symlink() or not f.is_file() or f.resolve().parent != real:
            continue
        size = f.stat().st_size
        init = init_size(f)
        if not init or init >= size:
            continue
        rec = info.get(f.name) or {}
        try:
            t0, t1 = float(rec.get("pc_start_epoch")), float(rec.get("pc_end_epoch"))
            dur = t1 - t0 if 0 < t1 - t0 < 4 * 3600 else default_s
        except (TypeError, ValueError):
            t0, dur = None, default_s
        out.append({"name": f.name, "bytes": size, "init": init, "duration": round(dur, 3), "start": t0})
        if len(out) >= MAX_SEGMENTS:
            break
    out.sort(key=lambda s: (s["start"] is None, s["start"] or 0, s["name"]))
    return out


def playlist(segs: list[dict], complete: bool, query: str = "") -> str:
    """The HLS playlist of a recording (``complete``: closed - VOD with an end; else EVENT, still growing).
    ``query``: appended to every URI (Safari's native HLS cannot send the page header)."""
    target = max([math.ceil(s["duration"]) for s in segs] or [1])
    lines = ["#EXTM3U", "#EXT-X-VERSION:7", f"#EXT-X-TARGETDURATION:{target}", "#EXT-X-MEDIA-SEQUENCE:0",
             f"#EXT-X-PLAYLIST-TYPE:{'VOD' if complete else 'EVENT'}", "#EXT-X-INDEPENDENT-SEGMENTS"]
    for i, s in enumerate(segs):
        uri = s["name"] + query
        if i:
            lines.append("#EXT-X-DISCONTINUITY")
        lines.append(f'#EXT-X-MAP:URI="{uri}",BYTERANGE="{s["init"]}@0"')
        lines.append(f"#EXTINF:{s['duration']:.3f},")
        lines.append(f"#EXT-X-BYTERANGE:{s['bytes'] - s['init']}@{s['init']}")
        lines.append(uri)
    if complete:
        lines.append("#EXT-X-ENDLIST")
    return "\n".join(lines) + "\n"


def listing(summaries: Iterable[dict], package_dir, default_s: float = 60.0, limit: int = 200) -> list[dict]:
    """The recordings the /tv player can play (video on the disk), newest first, with what the TV shows."""
    out = []
    for r in summaries:
        iid = str(r.get("stream_instance_id") or "")
        if not ID_RE.match(iid) or not r.get("capture_segments") or r.get("capture_deleted_at"):
            continue
        pkg = package_dir(iid)
        if pkg is None:
            continue
        segs = segments(pkg, default_s)
        if not segs:
            continue
        out.append({
            "id": iid, "meeting": r.get("meeting"), "session_name": r.get("session_name"),
            "session_kind": r.get("session_kind") or "other", "title": r.get("title"),
            "start": r.get("stream_start_wall_time") or r.get("detected_at"),
            "status": r.get("status"), "channel": r.get("channel") or "viewer",
            "duration_s": round(sum(s["duration"] for s in segs), 1), "segments": len(segs)})
        if len(out) >= limit:
            break
    return out
