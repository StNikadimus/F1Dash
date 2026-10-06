"""VOYO stream recordings - one package per detected VOYO stream instance.

The SERVER writes them (the Linux server is the system of record): the PC that shows VOYO only
posts its read-only playback-clock samples (tools/voyo_clock.py -> POST /api/sync/voyo); the
stream-instance detection (server/autosync.py StreamTracker) and this writer run here.
Separate from the F1 timing recordings (server/recorder.py -> data/recordings).

Layout under ``[voyo.recording] path`` (any disk / mount, see resolve_root)::

    <root>/
      index.json                        list of all packages (manifest summaries) - the fast listing
      <stream_instance_id>/
        manifest.json                   summary: identity, session, mode, status, counts, sync quality, files
        meta.json                       identity in full: VOYO identifiers, fingerprint, page fields, detection
        timeline.jsonl                  VOYO position samples with server receive time + play/pause/seek/...
        anchors.json                    sync anchors of this stream (applied / pending / history) + MARK STREAM START
        sync_observations.jsonl         anchors as they happened, (position, wall, F1 time) pairs,
                                        LIVE DATA DELAY, AUTO SYNC state changes, notes
        capture/                        opt-in window capture segments uploaded by the PC (+ capture.jsonl)

stream_instance_id is the identity (one package = one timeline); the F1 session is a secondary
association that may be learned later. A new package starts only when the tracker reports a new
instance - never on position updates, pause, buffering, seek or a temporary loss of the clock;
a reopened recording / continued live stream (tracker "resumed") appends to its package.
"""
from __future__ import annotations

import json
import logging
import os
import re
import shutil
import time
import uuid
from pathlib import Path
from statistics import median
from typing import Any, Callable, Optional

from .config import resolve_path

log = logging.getLogger("voyo-rec")

SCHEMA = 1
INDEX = "index.json"
FILES = ("manifest.json", "meta.json", "timeline.jsonl", "anchors.json", "sync_observations.jsonl")
CAPTURE_NAME = re.compile(r"^[A-Za-z0-9_.-]{1,96}\.(mp4|mkv|webm|ts)$")
ID_RE = re.compile(r"^[A-Za-z0-9_-]{4,64}$")
GAP_S = 5.0                 # no sample for this long while the package is open: a clock gap (noted, no new package)
RECHECK_S = 60.0            # after a write error: re-check the path this often
MANIFEST_EVERY_S = 30.0
FLUSH_EVERY_S = 5.0
RETENTION_EVERY_S = 3600.0


def session_kind(name: Optional[str]) -> str:
    """F1 session name -> the retention key (keep_<kind>_days)."""
    n = (name or "").strip().lower()
    if not n:
        return "other"
    if "sprint" in n and ("qualifying" in n or "shootout" in n):
        return "sprint_qualifying"
    if "sprint" in n:
        return "sprint"
    for k in ("1", "2", "3"):
        if n in (f"practice {k}", f"fp{k}", f"free practice {k}"):
            return f"practice{k}"
    if "qualifying" in n:
        return "qualifying"
    if n in ("race", "grand prix", "gp"):
        return "race"
    return "other"


def _iso(epoch_s: Optional[float]) -> Optional[str]:
    if epoch_s is None:
        return None
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(epoch_s)) + f".{int(epoch_s % 1 * 1000):03d}Z"


def _write_json(path: Path, data: Any) -> None:
    """Atomic: a crash / unplugged disk never leaves a half-written manifest."""
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=1, ensure_ascii=False, default=str)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


def _read_json(path: Path) -> Optional[Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def resolve_root(rc: dict, f1_recordings: Optional[Path] = None) -> tuple[Optional[Path], Optional[str]]:
    """The configured recordings folder, created if allowed, verified writable.
    Returns (path, None) or (path_or_None, error) - never another disk as a fallback."""
    raw = str(rc.get("path") or "").strip()
    if not raw:
        return None, "[voyo.recording] path is empty"
    root = resolve_path(raw).resolve()
    if f1_recordings is not None:
        f1 = Path(f1_recordings).resolve()
        if root == f1 or f1 in root.parents:
            return root, (f"{root} is the F1 timing recordings folder ({f1}) - VOYO stream recordings need their own "
                          "folder")
    if not root.exists():
        if not rc.get("create_path_if_missing", True):
            return root, (f"{root} does not exist and create_path_if_missing = false (disk not mounted?) - VOYO "
                          "stream recording is OFF")
        try:
            root.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            return root, f"cannot create {root}: {exc}"
    if not root.is_dir():
        return root, f"{root} is not a folder"
    probe = root / f".write-test-{uuid.uuid4().hex[:8]}"
    try:
        probe.write_bytes(b"ok")
        probe.unlink()
    except OSError as exc:
        return root, f"{root} is not writable: {exc}"
    return root, None


class VoyoStreamRecorder:
    """Writes the package of the current VOYO stream instance (server side)."""

    def __init__(self, rc: Optional[dict], f1_recordings: Optional[Path] = None,
                 clock: Callable[[], float] = time.time) -> None:
        self.rc = dict(rc or {})
        self.enabled = bool(self.rc.get("enabled", True))
        self.want_meta = bool(self.rc.get("record_metadata", True))
        self.want_timeline = bool(self.rc.get("record_timeline", True))
        self.want_sync = bool(self.rc.get("record_sync", True))
        self.capture_enabled = bool(self.rc.get("record_video_capture", False))
        self.tl_every = max(0.0, float(self.rc.get("timeline_interval_seconds", 1.0)))
        self.pair_every = max(1.0, float(self.rc.get("pair_interval_seconds", 10.0)))
        self.min_free = max(0, int(self.rc.get("min_free_bytes", 0) or 0))
        self.f1_recordings = f1_recordings
        self.now = clock
        self.root: Optional[Path] = None
        self.error: Optional[str] = None
        self.mode_info: Callable[[], dict] = lambda: {}       # set by the app: AUTO / LIVE / VOD selection
        self.cur: Optional[dict] = None                       # the open package (manifest dict)
        self._fh: dict[str, Any] = {}
        self._last_sample: Optional[dict] = None
        self._last_tl = -1e18
        self._last_pair = -1e18
        self._last_dd = -1e18
        self._last_flush = 0.0
        self._last_manifest = 0.0
        self._last_index = 0.0
        self._last_recheck = 0.0
        self._last_retention = -1e18
        self._auto_state: Optional[str] = None
        self._anchor_sig: Optional[str] = None
        self._said_error = False

    # ------------------------------------------------------------------ start / checks
    def start(self) -> "VoyoStreamRecorder":
        if not self.enabled:
            log.info("VOYO stream recording: off ([voyo.recording] enabled = false)")
            return self
        self.root, self.error = resolve_root(self.rc, self.f1_recordings)
        if self.error:
            log.error("VOYO stream recording DISABLED: %s", self.error)
            return self
        free = self.free_bytes()
        log.info("VOYO stream recordings: %s (free %s)%s", self.root, _human(free),
                 " - window capture ON (opt-in)" if self.capture_enabled else "")
        self._recover()
        self.apply_retention()
        return self

    @property
    def ok(self) -> bool:
        return self.enabled and self.root is not None and self.error is None

    def free_bytes(self) -> Optional[int]:
        try:
            return shutil.disk_usage(self.root).free if self.root else None
        except OSError:
            return None

    def _space_ok(self, need: int = 0) -> bool:
        if not self.min_free:
            return True
        free = self.free_bytes()
        return free is None or free - need >= self.min_free

    def _fail(self, what: str, exc: BaseException) -> None:
        self.error = f"{what}: {exc}"
        self._last_recheck = time.monotonic()
        log.error("VOYO stream recording stopped - %s (re-checking the path every %.0f s; no other disk is used)",
                  self.error, RECHECK_S)
        self._close_files()

    def _recheck(self) -> None:
        """After a write error (disk full / unplugged): writable again -> continue the open package."""
        if self.error is None or not self.enabled or time.monotonic() - self._last_recheck < RECHECK_S:
            return
        self._last_recheck = time.monotonic()
        root, err = resolve_root(self.rc, self.f1_recordings)
        if err:
            return
        self.root, self.error = root, None
        log.info("VOYO stream recording: %s writable again - continuing", root)
        if self.cur is not None:
            self._open_files(self.cur["stream_instance_id"])
            self._obs({"type": "note", "text": "recording interrupted by a write error and continued - gap"})

    # ------------------------------------------------------------------ the samples
    def observe(self, s, inst, reason: Optional[str], sync) -> None:
        """One VOYO clock sample (server receive time = now). ``inst`` = the tracker's current instance,
        ``reason`` = why it started (None = same instance)."""
        if not self.enabled:
            return
        self._recheck()
        if not self.ok or inst is None:
            return
        try:
            wall = self.now() - max(0.0, time.monotonic() - s.mono)
            if self.cur is None or self.cur["stream_instance_id"] != inst.id:
                self._switch(inst, reason or "first stream seen", sync, s, wall)
            elif reason == "resumed":
                self._obs({"type": "note", "t": round(wall, 3), "text": "server restarted - same stream instance resumed"})
            self._timeline(s, wall, sync)
            self._sync_obs(s, wall, sync)
            self._session(sync)
            self._periodic()
        except OSError as exc:
            self._fail("write failed", exc)

    def _switch(self, inst, reason: str, sync, s, wall: float) -> None:
        if self.cur is not None:
            self.finalize(f"new stream instance ({reason})")
        pkg = self.root / inst.id
        existing = _read_json(pkg / "manifest.json") if pkg.exists() else None
        if not self._space_ok():
            raise OSError(f"less than min_free_bytes ({_human(self.min_free)}) free on {self.root}")
        pkg.mkdir(parents=True, exist_ok=True)
        if existing:                                  # resumed instance: append to its package
            self.cur = existing
            self.cur.update(status="recording", closed_at=None, close_reason=None)
            self.cur.setdefault("resumes", []).append({"at": _iso(wall), "reason": reason})
            log.info("VOYO stream recording %s continued (%s)", inst.id, reason)
        else:
            mode = self._mode(sync)
            self.cur = {
                "schema": SCHEMA, "stream_instance_id": inst.id, "status": "recording",
                "detected_at": _iso(inst.first_seen_wall), "detected_at_epoch": inst.first_seen_wall,
                "stream_start_wall_time": _iso(inst.origin_wall or inst.first_seen_wall),
                "stream_start_wall_epoch": inst.origin_wall or inst.first_seen_wall,
                "stream_start_how": inst.origin_how if inst.origin_wall else
                "not seen from 0:00 - the detection time is used (" + inst.origin_how + ")",
                "detection_reason": reason, "live": inst.live, "asset": inst.asset, "media_id": inst.media_id,
                "title": inst.title, "mode": mode, "session": None, "session_history": [],
                "duration": None, "position": {"first": round(s.pb, 3), "min": round(s.pb, 3), "max": round(s.pb, 3)},
                "counts": {"timeline": 0, "observations": 0, "anchors": 0, "pairs": 0, "gaps": 0},
                "sync": None, "live_data_delay": None, "stream_start_marks": 0,
                "capture": {"enabled": self.capture_enabled, "segments": [], "bytes": 0, "deleted_at": None},
                "closed_at": None, "close_reason": None, "updated_at": _iso(wall), "files": list(FILES)}
            log.info("VOYO stream recording %s started (%s): %s, %s -> %s", inst.id, reason,
                     "live" if inst.live else "recording", inst.title or inst.asset[:60], pkg)
        self._open_files(inst.id)
        if self.want_meta:
            page = dict(getattr(sync, "page", None) or s.page or {})
            _write_json(pkg / "meta.json", {
                "schema": SCHEMA, "stream_instance_id": inst.id, "instance": inst.to_json(),
                "voyo": {"asset": inst.asset, "media_id": inst.media_id, "title": inst.title,
                         "options_fp": inst.options_fp, "load_id": inst.load_id, "url_path": page.get("url_path"),
                         "page": page, "meta": dict(s.meta or {})},
                "detection": {"reason": reason, "first_seen_utc": _iso(inst.first_seen_wall),
                              "stream_start_wall_utc": _iso(inst.origin_wall) if inst.origin_wall else None,
                              "stream_start_how": inst.origin_how, "mode": self.cur.get("mode")},
                "server": {"recorder_root": str(self.root)}})
        self._last_sample = None
        self._last_tl = self._last_pair = self._last_dd = -1e18
        self._auto_state = None
        self._anchor_sig = None
        self.write_manifest(force=True)
        self.write_index(force=True)

    def _mode(self, sync) -> dict:
        try:
            m = self.mode_info() or {}
        except Exception:  # noqa: BLE001
            m = {}
        return {"selected": m.get("selected_mode"), "effective": m.get("effective_mode"),
                "source": "VOD" if getattr(sync, "vod", False) else "LIVE"}

    def _timeline(self, s, wall: float, sync) -> None:
        cur = self.cur
        pos = cur["position"]
        pos["min"], pos["max"] = round(min(pos["min"], s.pb), 3), round(max(pos["max"], s.pb), 3)
        if s.duration and s.duration < 1e7:
            cur["duration"] = round(s.duration, 3)
        state = ("ended" if s.ended else "seeking" if s.seeking else "buffering" if s.ready < 3 and not s.paused
                 else "paused" if s.paused else "playing")
        rec = {"t": round(wall, 3), "pb": round(s.pb, 3), "state": state, "rate": s.rate}
        prev = self._last_sample
        if prev is not None and wall - prev["t"] > GAP_S:
            gap = round(wall - prev["t"], 1)
            cur["counts"]["gaps"] += 1
            self._line("timeline", {"t": round(wall, 3), "type": "gap", "seconds": gap,
                                    "note": "no VOYO clock samples (clock lost / bridge stopped) - same stream"})
            self._obs({"type": "note", "t": round(wall, 3), "text": f"VOYO clock lost for {gap} s"})
        events = [e.get("type") for e in (s.events or [])]
        changed = prev is None or any(prev.get(k) != rec[k] for k in ("state", "rate"))
        if events or changed or (state == "playing" and wall - self._last_tl >= self.tl_every):
            if events:
                rec["events"] = events
            if s.duration and (prev is None or prev.get("dur") != cur["duration"]):
                rec["dur"] = cur["duration"]
            if s.seekable_end is not None and cur.get("live"):
                rec["edge"] = round(s.seekable_end, 3)
            if self.want_timeline:
                self._line("timeline", rec)
                cur["counts"]["timeline"] += 1
            self._last_tl = wall
        self._last_sample = {"t": wall, "state": state, "rate": s.rate, "dur": cur["duration"]}

    def _sync_obs(self, s, wall: float, sync) -> None:
        if not self.want_sync:
            return
        mono = time.monotonic()
        st = getattr(sync, "auto_state", None) or {}
        if st.get("state") and st.get("state") != self._auto_state:
            self._auto_state = st["state"]
            self._obs({"type": "autosync", "t": round(wall, 3), "state": st["state"], "reason": st.get("reason"),
                       "confidence": st.get("confidence")})
        m = getattr(sync, "mapping", None)
        k = getattr(sync, "K", None)
        video = sync._video_mode(mono) if hasattr(sync, "_video_mode") else True
        if (k is not None and video and m is not None and not s.paused and s.rate > 0
                and wall - self._last_pair >= self.pair_every):
            self._last_pair = wall
            self.cur["counts"]["pairs"] += 1
            self._obs({"type": "pair", "t": round(wall, 3), "pb": round(s.pb, 3),
                       "f1_ms": round((s.pb + k) * 1000), "f1_source": "mapping", "offset": round(k, 3),
                       "confidence": m.confidence, "method": m.method,
                       "quality": "measured" if m.confidence in ("HIGH", "MEDIUM", "MANUAL") else "estimate"})
            self.cur["sync"] = {"confidence": m.confidence, "offset": round(k, 3), "method": m.method,
                                "state": st.get("state")}
        if not getattr(sync, "vod", True) and wall - self._last_dd >= 10:
            dd = sync.live_delay.snapshot(mono)
            if dd.get("samples"):
                self._last_dd = wall
                self._obs({"type": "live_data_delay", "t": round(wall, 3), **{k2: dd[k2] for k2 in (
                    "state", "seconds", "current", "spread", "samples", "clockWarning")}})
                if dd.get("state") == "STABLE":
                    self.cur["live_data_delay"] = {"seconds": dd["seconds"], "spread": dd["spread"]}

    def _session(self, sync) -> None:
        sess = getattr(sync, "session", None) or {}
        key = sess.get("session_key")
        if key is None:
            return
        cur = self.cur.get("session") or {}
        if cur.get("session_key") == key:
            return
        new = {"session_key": key, "meeting": sess.get("meeting_name"), "session_name": sess.get("session_name"),
               "kind": session_kind(sess.get("session_name")), "year": sess.get("year"),
               "date_start": sess.get("date_start"), "associated_at": _iso(self.now())}
        if cur:
            self.cur["session_history"].append(cur)
            self._obs({"type": "note", "t": round(self.now(), 3),
                       "text": f"F1 session association changed {cur.get('session_key')} -> {key}"})
        self.cur["session"] = new
        log.info("VOYO stream recording %s: session %s %s (key %s)", self.cur["stream_instance_id"],
                 new["meeting"], new["session_name"], key)
        self.write_manifest(force=True)

    # ------------------------------------------------------------------ sync events (SyncManager hook)
    def sync_event(self, kind: str, data: dict, sync) -> None:
        """Called by SyncManager: "anchors" (the anchor set changed), "anchor" (one observation: applied
        / pending / kept / used), "stream_start" / "stream_start_reset" (MARK STREAM START, kept apart)."""
        if not self.ok or self.cur is None:
            return
        try:
            iid = self.cur["stream_instance_id"]
            t = round(self.now(), 3)
            if kind == "anchor":
                a = data.get("anchor") or {}
                if a.get("instance_id") and a["instance_id"] != iid:
                    return                            # belongs to another stream instance
                self.cur["counts"]["anchors"] += 1
                obs = {"type": "anchor", "t": t, **a, "anchor_status": a.get("status"), "status": data.get("status"),
                       "quality": "outlier" if a.get("outlier") else data.get("status")}
                if data.get("shift") is not None:
                    obs["shift"] = data["shift"]
                self._obs(obs)
            mark = None
            if kind in ("stream_start", "stream_start_reset"):
                mark = {"type": kind, "t": t, "at": _iso(t), **data}
                if kind == "stream_start":
                    self.cur["stream_start_marks"] += 1
                self._obs(dict(mark))
            self._write_anchors(sync, mark)
            self.write_manifest(force=True)
        except OSError as exc:
            self._fail("write failed", exc)

    def _write_anchors(self, sync, mark: Optional[dict] = None) -> None:
        if not self.want_meta or self.cur is None:
            return
        iid = self.cur["stream_instance_id"]
        mine = lambda a: a.instance_id in ("", iid)            # noqa: E731
        pending = (getattr(sync, "pending", None) or {}).get("anchors") or []
        origin = getattr(sync, "origin", None)
        old = _read_json(self.root / iid / "anchors.json") or {}
        marks = (old.get("stream_start_marks") or []) + ([mark] if mark else [])
        doc = {"schema": SCHEMA, "stream_instance_id": iid,
               # the automatic base anchor (server clock) and the manual MARK STREAM START - kept apart
               "stream_start_wall_time": self.cur.get("stream_start_wall_time"),
               "stream_start_marks": marks,
               "stream_start_mark": None if not origin else {
                   k: origin.get(k) for k in ("video_time", "utc_ms", "conf", "method", "source", "restored")},
               "anchors": [a.to_json() for a in sync.anchors if mine(a)],
               "pending": [a.to_json() for a in pending if mine(a)],
               "history": [a.to_json() for a in sync.history if mine(a)][-30:],
               "mapping": {"offset": sync.mapping.offset, "confidence": sync.mapping.confidence,
                           "method": sync.mapping.method, "deviation": sync.mapping.deviation},
               "session": self.cur.get("session"), "updated_at": _iso(self.now())}
        _write_json(self.root / iid / "anchors.json", doc)

    # ------------------------------------------------------------------ files
    def _open_files(self, iid: str) -> None:
        self._close_files()
        pkg = self.root / iid
        if self.want_timeline:
            self._fh["timeline"] = open(pkg / "timeline.jsonl", "a", encoding="utf-8")
        if self.want_sync:
            self._fh["obs"] = open(pkg / "sync_observations.jsonl", "a", encoding="utf-8")

    def _close_files(self) -> None:
        for fh in self._fh.values():
            try:
                fh.close()
            except OSError:
                pass
        self._fh = {}

    def _line(self, which: str, rec: dict) -> None:
        fh = self._fh.get(which)
        if fh is not None:
            fh.write(json.dumps(rec, separators=(",", ":"), default=str) + "\n")

    def _obs(self, rec: dict) -> None:
        if self.want_sync and self.cur is not None:
            rec.setdefault("t", round(self.now(), 3))
            self._line("obs", rec)
            self.cur["counts"]["observations"] += 1

    def _periodic(self) -> None:
        mono = time.monotonic()
        if mono - self._last_flush >= FLUSH_EVERY_S:
            self._last_flush = mono
            for fh in self._fh.values():
                fh.flush()
        self.write_manifest()
        self.write_index()
        if mono - self._last_retention >= RETENTION_EVERY_S:
            self.apply_retention()

    def write_manifest(self, force: bool = False) -> None:
        if self.cur is None or not self.ok:
            return
        mono = time.monotonic()
        if not force and mono - self._last_manifest < MANIFEST_EVERY_S:
            return
        self._last_manifest = mono
        self.cur["updated_at"] = _iso(self.now())
        for fh in self._fh.values():
            fh.flush()
        _write_json(self.root / self.cur["stream_instance_id"] / "manifest.json", self.cur)

    def finalize(self, reason: str) -> None:
        """Close the open package (a new instance, server stop)."""
        if self.cur is None:
            return
        cur, self.cur = self.cur, None
        cur.update(status="closed", closed_at=_iso(self.now()), close_reason=reason, updated_at=_iso(self.now()))
        try:
            if self.ok:
                self._close_files()
                _write_json(self.root / cur["stream_instance_id"] / "manifest.json", cur)
                self._index_put(cur)
                log.info("VOYO stream recording %s closed (%s): %d samples, %d observations, %d anchors",
                         cur["stream_instance_id"], reason, cur["counts"]["timeline"], cur["counts"]["observations"],
                         cur["counts"]["anchors"])
        except OSError as exc:
            self._fail("finalize failed", exc)

    # ------------------------------------------------------------------ index
    @staticmethod
    def summary(m: dict) -> dict:
        sess = m.get("session") or {}
        pos = m.get("position") or {}
        cap = m.get("capture") or {}
        return {"stream_instance_id": m.get("stream_instance_id"), "status": m.get("status"),
                "title": m.get("title"), "live": m.get("live"), "media_id": m.get("media_id"),
                "detected_at": m.get("detected_at"), "stream_start_wall_time": m.get("stream_start_wall_time"),
                "closed_at": m.get("closed_at"), "updated_at": m.get("updated_at"),
                "session_key": sess.get("session_key"), "meeting": sess.get("meeting"),
                "session_name": sess.get("session_name"), "session_kind": sess.get("kind") or "other",
                "duration": m.get("duration"),
                "watched_seconds": round((pos.get("max") or 0) - (pos.get("min") or 0), 1),
                "sync": m.get("sync"), "live_data_delay": m.get("live_data_delay"),
                "anchors": (m.get("counts") or {}).get("anchors"),
                "capture_segments": len(cap.get("segments") or []), "capture_bytes": cap.get("bytes") or 0,
                "capture_deleted_at": cap.get("deleted_at")}

    def _load_index(self) -> dict:
        idx = _read_json(self.root / INDEX)
        if not isinstance(idx, dict) or not isinstance(idx.get("recordings"), dict):
            idx = self.rebuild_index()
        return idx

    def rebuild_index(self) -> dict:
        recs = {}
        for mf in sorted(self.root.glob("*/manifest.json")):
            m = _read_json(mf)
            if isinstance(m, dict) and m.get("stream_instance_id"):
                recs[m["stream_instance_id"]] = self.summary(m)
        idx = {"schema": SCHEMA, "recordings": recs}
        _write_json(self.root / INDEX, idx)
        return idx

    def _index_put(self, m: dict) -> None:
        idx = self._load_index()
        idx["recordings"][m["stream_instance_id"]] = self.summary(m)
        idx["updated_at"] = _iso(self.now())
        _write_json(self.root / INDEX, idx)

    def write_index(self, force: bool = False) -> None:
        if self.cur is None or not self.ok:
            return
        mono = time.monotonic()
        if not force and mono - self._last_index < 60:
            return
        self._last_index = mono
        self._index_put(self.cur)

    def _recover(self) -> None:
        """Packages left "recording" by a crash / power loss: interrupted (a resumed instance reopens it)."""
        n = 0
        for mf in self.root.glob("*/manifest.json"):
            m = _read_json(mf)
            if isinstance(m, dict) and m.get("status") == "recording":
                m.update(status="interrupted", close_reason="server stopped while recording")
                _write_json(mf, m)
                n += 1
        if n:
            log.info("VOYO stream recordings: %d package(s) marked interrupted (server stopped while recording)", n)
        self.rebuild_index()

    # ------------------------------------------------------------------ list / load (dashboard, offline use)
    def list(self) -> list[dict]:
        if not self.ok:
            return []
        recs = list(self._load_index()["recordings"].values())
        if self.cur is not None:
            recs = [r for r in recs if r["stream_instance_id"] != self.cur["stream_instance_id"]] + \
                [self.summary(self.cur)]
        return sorted(recs, key=lambda r: r.get("detected_at") or "", reverse=True)

    def package_dir(self, iid: str) -> Optional[Path]:
        if not self.ok or not ID_RE.match(iid or ""):
            return None
        p = self.root / iid
        return p if (p / "manifest.json").exists() else None

    def status(self) -> dict:
        return {"enabled": self.enabled, "ok": self.ok, "path": str(self.root) if self.root else None,
                "error": self.error, "free_bytes": self.free_bytes() if self.ok else None,
                "min_free_bytes": self.min_free, "capture": self.capture_enabled,
                "current": self.cur["stream_instance_id"] if self.cur else None}

    def clock_reply(self) -> dict:
        """Returned to the PC's clock bridge with every sample: which package is open, whether the
        PC should run the (opt-in) window capture for it."""
        return {"instance": self.cur["stream_instance_id"] if self.cur else None,
                "capture": bool(self.capture_enabled and self.ok and self.cur is not None),
                "segment_seconds": int(self.rc.get("capture_segment_seconds", 60)),
                "fps": int(self.rc.get("capture_fps", 30)), "crf": int(self.rc.get("capture_crf", 23))}

    # ------------------------------------------------------------------ capture upload
    def capture_target(self, iid: str, name: str, size: Optional[int]) -> tuple[Optional[Path], Optional[str]]:
        if not self.capture_enabled:
            return None, "window capture is off ([voyo.recording] record_video_capture = false)"
        if not self.ok:
            return None, self.error or "VOYO stream recording is off"
        pkg = self.package_dir(iid)
        if pkg is None:
            return None, "unknown stream instance"
        if not CAPTURE_NAME.match(name or ""):
            return None, "bad file name"
        limit = int(self.rc.get("capture_max_segment_bytes", 4 * 1024 ** 3))
        if size is not None and size > limit:
            return None, "segment too large"
        if not self._space_ok(size or 0):
            return None, f"less than min_free_bytes free on {self.root}"
        d = pkg / "capture"
        d.mkdir(exist_ok=True)
        return d / name, None

    def capture_stored(self, iid: str, path: Path, info: dict) -> None:
        """A segment is complete on disk: list it in the package (manifest + capture/capture.jsonl)."""
        rec = {"name": path.name, "bytes": path.stat().st_size, "received_at": _iso(self.now()), **info}
        with open(path.parent / "capture.jsonl", "a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec, separators=(",", ":")) + "\n")
        m = self.cur if self.cur is not None and self.cur["stream_instance_id"] == iid else \
            _read_json(path.parent.parent / "manifest.json")
        if not isinstance(m, dict):
            return
        cap = m.setdefault("capture", {"segments": [], "bytes": 0})
        cap["enabled"] = True
        cap.setdefault("segments", []).append(rec["name"])
        cap["bytes"] = int(cap.get("bytes") or 0) + rec["bytes"]
        if m is self.cur:
            self.write_manifest(force=True)
        else:
            _write_json(path.parent.parent / "manifest.json", m)
            self._index_put(m)

    # ------------------------------------------------------------------ retention (video only)
    def keep_days(self, kind: str) -> float:
        return float(self.rc.get(f"keep_{kind}_days", self.rc.get("keep_other_days", 0)) or 0)

    def apply_retention(self) -> int:
        """Delete captured VIDEO files older than keep_<session kind>_days; metadata stays."""
        self._last_retention = time.monotonic()
        if not self.ok:
            return 0
        removed = 0
        now = self.now()
        for mf in self.root.glob("*/manifest.json"):
            m = _read_json(mf)
            if not isinstance(m, dict) or m.get("status") == "recording":
                continue
            cap_dir = mf.parent / "capture"
            files = [f for f in cap_dir.glob("*") if f.is_file() and CAPTURE_NAME.match(f.name)] \
                if cap_dir.exists() else []
            if not files:
                continue
            kind = (m.get("session") or {}).get("kind") or "other"
            days = self.keep_days(kind)
            if days <= 0:
                continue
            ref = m.get("closed_at") or m.get("updated_at")
            ref_s = _parse_iso(ref) or mf.stat().st_mtime
            if now - ref_s < days * 86400:
                continue
            for f in files:
                try:
                    f.unlink()
                    removed += 1
                except OSError as exc:
                    log.warning("VOYO recordings retention: cannot delete %s: %s", f, exc)
            m.setdefault("capture", {})["deleted_at"] = _iso(now)
            m["capture"]["deleted_reason"] = f"retention: {kind} video kept {days:g} days"
            _write_json(mf, m)
            self._index_put(m)
            log.info("VOYO recordings retention: video of %s (%s) deleted after %g days - metadata kept",
                     m.get("stream_instance_id"), kind, days)
        return removed

    def close(self) -> None:
        self.finalize("server stopped")


def _parse_iso(v: Optional[str]) -> Optional[float]:
    if not v:
        return None
    try:
        import calendar
        base = v.rstrip("Z").split(".")[0]
        return calendar.timegm(time.strptime(base, "%Y-%m-%dT%H:%M:%S"))
    except ValueError:
        return None


def _human(n: Optional[int]) -> str:
    if n is None:
        return "?"
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return str(n)


# ---------------------------------------------------------------------------
# Offline use: load a package without the VOYO window
# ---------------------------------------------------------------------------
def load_package(pkg: Path) -> dict:
    """Everything of one package as Python data (the file contract in main/README.md §9d)."""
    def jsonl(name: str) -> list[dict]:
        out = []
        p = pkg / name
        if p.exists():
            with open(p, encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if line:
                        try:
                            out.append(json.loads(line))
                        except ValueError:
                            continue               # a line cut by a crash / unplugged disk
        return out
    return {"manifest": _read_json(pkg / "manifest.json") or {}, "meta": _read_json(pkg / "meta.json") or {},
            "anchors": _read_json(pkg / "anchors.json") or {}, "timeline": jsonl("timeline.jsonl"),
            "observations": jsonl("sync_observations.jsonl"),
            "capture": jsonl("capture/capture.jsonl") if (pkg / "capture").exists() else []}


def recalibrate(package: dict, agree_s: float = 0.5) -> dict:
    """AUTO SYNC calibration re-run from the saved anchors: the median offset of the applied
    (non-outlier) anchors and how well they agree. offset = F1 epoch s - VOYO position s."""
    anchors = [a for a in (package.get("anchors") or {}).get("anchors") or []
               if a.get("video_time") is not None and not a.get("outlier") and a.get("kind") != "pin"]
    if not anchors:
        return {"offset": None, "n": 0, "spread": None, "state": "UNSYNCED"}
    offs = [float(a["offset"]) for a in anchors]
    mid = median(offs)
    spread = median(abs(o - mid) for o in offs) * 1.4826 if len(offs) > 1 else 0.0
    return {"offset": round(mid, 3), "n": len(offs), "spread": round(spread, 3),
            "state": "LOCKED" if spread <= agree_s else "UNSTABLE"}


def live_delay_history(package: dict) -> list[tuple[float, float]]:
    """(wall epoch s, LIVE DATA DELAY s) of a live package - for comparing streams."""
    return [(o["t"], o["seconds"]) for o in package.get("observations") or []
            if o.get("type") == "live_data_delay" and o.get("seconds") is not None]
