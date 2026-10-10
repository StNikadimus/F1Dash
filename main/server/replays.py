"""REPLAYS for the /tv player: the VOYO stream recordings that have video, played from the disk.

The recorded video is the package's capture/ folder ([voyo.recording] path, server/voyo_recording.py):
fragmented-MP4 segments written by ffmpeg (tools/voyo_capture.py), each one ``ftyp`` + ``moov`` (the init
part) + ``moof``/``mdat`` fragments, every segment starting again at 0. The browser plays a recording
as one HLS playlist built from those files AS THEY ARE - no remux, no copy, nothing written:

    #EXT-X-MAP:URI="<segment>",BYTERANGE="<init size>@0"      the segment's own init part
    #EXTINF:<seconds>,
    #EXT-X-BYTERANGE:<fragments>@<init size>                   its complete fragments
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
VIDEO_RE = re.compile(r"\.(mp4|mkv|webm|ts)$")          # what a capture can upload (voyo_recording.CAPTURE_NAME)
MAX_SEGMENTS = 2000                       # 60 s pieces: ~33 h - far more than any session
_init_cache: dict[tuple, Optional[tuple[int, int]]] = {}
_lock = threading.Lock()


def layout(path: Path) -> Optional[tuple[int, int]]:
    """(init, end) of a fragmented MP4: ``init`` = bytes before the first ``moof`` (ftyp + moov), ``end`` = the
    end of its last COMPLETE fragment (``moof`` + its whole ``mdat``) - a segment cut short (power loss, a full
    disk) plays up to there. None when the file is not one (plain MP4, still being written, damaged, no
    complete fragment). Reads only the box headers."""
    try:
        st = path.stat()
    except OSError:
        return None
    key = (str(path), st.st_size, st.st_mtime_ns)
    with _lock:
        if key in _init_cache:
            return _init_cache[key]
    found: Optional[tuple[int, int]] = None
    init: Optional[int] = None
    end = 0
    seen_moov = in_frag = False
    try:
        with open(path, "rb") as fh:
            pos = 0
            for _ in range(200_000):                          # ftyp, moov (+ free / sidx), then moof + mdat pairs
                head = fh.read(8)
                if len(head) < 8:
                    break
                size, typ = struct.unpack(">I4s", head)
                if size == 1:                                 # 64-bit size
                    ext = fh.read(8)
                    if len(ext) < 8:
                        break
                    size = struct.unpack(">Q", ext)[0]
                if size < 8 or pos + size > st.st_size:       # the file ends inside this box: cut short here
                    break
                if typ == b"moov":
                    seen_moov = True
                elif typ == b"moof":
                    if init is None:
                        if not seen_moov or pos == 0:
                            break
                        init = pos
                    in_frag = True
                elif typ == b"mdat":
                    if init is None:                          # media before any fragment: not fragmented
                        break
                    if in_frag:
                        end, in_frag = pos + size, False
                pos += size
                fh.seek(pos)
    except OSError:
        init = None
    if init is not None and end > init:
        found = (init, end)
    with _lock:
        if len(_init_cache) > 4096:
            _init_cache.clear()
        _init_cache[key] = found
    return found


def init_size(path: Path) -> Optional[int]:
    """Bytes of the init part (everything before the first ``moof``) of a fragmented MP4, or None when the
    file is not a playable one (see ``layout``)."""
    lay = layout(path)
    return lay[0] if lay else None


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


def segments(pkg: Path, default_s: float = 60.0, stats: Optional[dict] = None) -> list[dict]:
    """The playable segments of a package, oldest first: [{name, bytes, init, end, duration, start}].
    Durations come from the capture log (ffmpeg's own segment times); missing ones use ``default_s``.
    ``stats``: filled with what was found - ``files`` (video files in capture/), ``bad`` (not playable)."""
    cap = pkg / "capture"
    if stats is not None:
        stats.update(files=0, bad=0)
    if not cap.is_dir() or cap.is_symlink():
        return []
    info = _capture_log(pkg)
    real = cap.resolve()
    out = []
    for f in sorted(cap.iterdir())[: MAX_SEGMENTS * 2]:
        if not VIDEO_RE.search(f.name):
            continue
        # only the package's own files: no links (out of the folder, or anywhere else)
        if not SEGMENT_RE.match(f.name) or f.is_symlink() or not f.is_file() or f.resolve().parent != real:
            if stats is not None and not f.is_symlink():
                stats["files"] += 1
                stats["bad"] += 1
            continue
        if stats is not None:
            stats["files"] += 1
        size = f.stat().st_size
        lay = layout(f)
        if not lay:
            if stats is not None:
                stats["bad"] += 1
            continue
        init, end = lay
        rec = info.get(f.name) or {}
        try:
            t0, t1 = float(rec.get("pc_start_epoch")), float(rec.get("pc_end_epoch"))
            dur = t1 - t0 if 0 < t1 - t0 < 4 * 3600 else default_s
        except (TypeError, ValueError):
            t0, dur = None, default_s
        out.append({"name": f.name, "bytes": size, "init": init, "end": end, "duration": round(dur, 3), "start": t0})
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
        lines.append(f"#EXT-X-BYTERANGE:{s.get('end', s['bytes']) - s['init']}@{s['init']}")
        lines.append(uri)
    if complete:
        lines.append("#EXT-X-ENDLIST")
    return "\n".join(lines) + "\n"


def listing(summaries: Iterable[dict], package_dir, default_s: float = 60.0, limit: int = 200,
            hidden: Optional[dict] = None) -> list[dict]:
    """The recordings the /tv player can play (video on the disk that it can read), newest first, with what
    the TV shows. Decided by the files on the disk, not by the index alone. ``hidden``: counts of the packages
    left out - ``no_video`` (timing / sync data only), ``unplayable`` (video files the player cannot read:
    another format, damaged, still being written), ``deleted`` (the video was removed by retention)."""
    if hidden is not None:
        hidden.update(no_video=0, unplayable=0, deleted=0)

    def skip(why: str) -> None:
        if hidden is not None:
            hidden[why] += 1

    out = []
    for r in summaries:
        iid = str(r.get("stream_instance_id") or "")
        if not ID_RE.match(iid):
            continue
        if r.get("capture_deleted_at"):
            skip("deleted")
            continue
        pkg = package_dir(iid)
        if pkg is None:
            continue
        st: dict = {}
        segs = segments(pkg, default_s, st)
        if not segs:
            skip("unplayable" if st.get("files") else "no_video")
            continue
        out.append({
            "id": iid, "meeting": r.get("meeting"), "session_name": r.get("session_name"),
            "session_kind": r.get("session_kind") or "other", "title": r.get("title"),
            "start": r.get("stream_start_wall_time") or r.get("detected_at"),
            "status": r.get("status"), "channel": r.get("channel") or "viewer",
            "duration_s": round(sum(s["duration"] for s in segs), 1), "segments": len(segs),
            "skipped_segments": st.get("bad", 0),
            # the recording's own completeness check (COMPLETE / INCOMPLETE) - a short file is said as such
            "check": r.get("capture_check"), "sound": r.get("capture_sound")})
        if len(out) >= limit:
            break
    return out


def main(argv: Optional[list] = None) -> int:
    """Read-only check of a recordings folder: which packages the /tv player lists, and why the others not.
        python -m server.replays /mnt/f1disk/voyo_streams        (the [voyo.recording] path; nothing is written)"""
    import sys
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 1:
        print("usage: python -m server.replays <[voyo.recording] path>")
        return 2
    root = Path(args[0])
    if not root.is_dir():
        print(f"{root}: not a folder")
        return 1
    n = 0
    for mf in sorted(root.glob("*/manifest.json")):
        try:
            m = json.loads(mf.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            print(f"{mf.parent.name:<24} manifest.json unreadable - not listed")
            continue
        sess = m.get("session") or {}
        cap = m.get("capture") or {}
        st: dict = {}
        segs = [] if cap.get("deleted_at") else segments(mf.parent, stats=st)
        why = ("PLAYABLE" if segs else "video deleted (retention)" if cap.get("deleted_at")
               else "no video files (timing / sync data only)" if not st.get("files")
               else "video files the player cannot read (not fragmented MP4 / damaged / another format)")
        n += bool(segs)
        label = " · ".join(str(x) for x in (sess.get("meeting"), sess.get("session_name")) if x) or m.get("title") or ""
        print(f"{mf.parent.name:<24} {str(m.get('status')):<12} {why:<40} "
              f"{len(segs)}/{st.get('files', 0)} segments, {sum(s['duration'] for s in segs) / 60:.1f} min  {label}")
    print(f"{n} playable recording(s) in {root}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
