"""The /disk page's backend: disk usage, what the recorder is doing now, the recordings with
their sizes, the retention settings (changeable on the page) and deleting recordings.

Retention changed on the page is stored in ``<data>/voyo_recording_settings.json`` and applied on
top of the config files ([voyo.recording] keep_*_days, min_free_bytes) - at start and at once.
"""
from __future__ import annotations

import json
import logging
import shutil
import time
from pathlib import Path
from typing import Optional

from .voyo_recording import CAPTURE_NAME, VoyoStreamRecorder, _read_json, _write_json

log = logging.getLogger("disk")

KINDS = ("practice1", "practice2", "practice3", "sprint_qualifying", "sprint", "qualifying", "race", "other")
KIND_LABELS = {"practice1": "Practice 1", "practice2": "Practice 2", "practice3": "Practice 3",
               "sprint_qualifying": "Sprint Qualifying", "sprint": "Sprint", "qualifying": "Qualifying",
               "race": "Grand Prix (race)", "other": "Other / not identified"}
EDITABLE = tuple(f"keep_{k}_days" for k in KINDS) + ("min_free_bytes",)
SIZE_CACHE_S = 30.0
# the server player's session recorder (tools/voyo_session.py) -> what /disk and /tv show
SESSION_STATES = {
    "DISCOVERING": ("OPENING", "ok", "looking for its recording on the VOYO event page"),
    "OPENING": ("OPENING", "ok", "opening its recording"),
    "VERIFYING": ("OPENING", "ok", "checking that the player plays that recording"),
    "NOT_FOUND": ("NOT FOUND", "bad", "its recording is not on the VOYO event page (looking again every minute)"),
    "AMBIGUOUS": ("AMBIGUOUS", "bad", "several recordings could be it - none is recorded (looking again)"),
    "FAILED": ("FAILED", "bad", "the recording could not be confirmed (trying again)"),
    "RECORDING": ("RECORDING", "warn", "recording"),
}
SAMPLE_FRESH_S = 30.0          # a stream counts as being recorded while samples are this recent
HEARTBEAT_STALE_S = 90.0       # the server player posts its state every 15 s


class Settings:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.values: dict = {}
        d = _read_json(path)
        if isinstance(d, dict):
            self.values = {k: d[k] for k in EDITABLE if k in d}

    def apply(self, rc: dict) -> dict:
        """The config section with the page's values on top."""
        return {**rc, **self.values}

    def update(self, new: dict, recorders: tuple) -> dict:
        clean = {}
        for k, v in (new or {}).items():
            if k not in EDITABLE:
                raise ValueError(f"unknown setting {k}")
            try:
                num = float(v)
            except (TypeError, ValueError):
                raise ValueError(f"{k}: a number is needed") from None
            if num < 0 or (k != "min_free_bytes" and num > 3650):
                raise ValueError(f"{k}: 0 .. 3650 days")
            clean[k] = int(num) if k == "min_free_bytes" else round(num, 2)
        self.values.update(clean)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        _write_json(self.path, self.values)
        for rec in recorders:
            if rec is not None:
                rec.rc.update(clean)
                if "min_free_bytes" in clean:
                    rec.min_free = int(clean["min_free_bytes"])
        log.info("Recording settings changed on the /disk page: %s",
                 ", ".join(f"{k}={v}" for k, v in clean.items()))
        return clean


def retention_table(rc: dict, overridden: dict) -> list[dict]:
    return [{"kind": k, "label": KIND_LABELS[k], "key": f"keep_{k}_days",
             "days": float(rc.get(f"keep_{k}_days", rc.get("keep_other_days", 0)) or 0),
             "changed_on_page": f"keep_{k}_days" in overridden} for k in KINDS]


class Sizes:
    """Bytes per package (video / data), cached - scanning a big disk on every refresh is slow."""

    def __init__(self) -> None:
        self._at = -1e9
        self._data: dict = {}

    def get(self, root: Optional[Path], force: bool = False) -> dict:
        if root is None:
            return {}
        if not force and time.monotonic() - self._at < SIZE_CACHE_S:
            return self._data
        out = {}
        for pkg in root.iterdir() if root.is_dir() else []:
            if not pkg.is_dir() or not (pkg / "manifest.json").exists():
                continue
            video = data = 0
            segs = 0
            for f in pkg.rglob("*"):
                if not f.is_file():
                    continue
                try:
                    n = f.stat().st_size
                except OSError:
                    continue
                if f.parent.name == "capture" and CAPTURE_NAME.match(f.name):
                    video += n
                    segs += 1
                else:
                    data += n
            out[pkg.name] = {"video_bytes": video, "data_bytes": data, "segments": segs}
        self._data, self._at = out, time.monotonic()
        return out


def disk_info(rec: VoyoStreamRecorder, sizes: dict) -> dict:
    st = rec.status()
    root = rec.root
    info = {"path": str(root) if root else None, "ok": st["ok"], "error": st["error"], "total": None,
            "used": None, "free": None, "percent": None, "recordings_bytes": None, "video_bytes": None,
            "hours_left": None, "min_free_bytes": rec.min_free,
            "require_mount": rec.rc.get("require_mount") or None, "mount_marker": rec.rc.get("mount_marker") or None}
    probe = root if root is not None and root.exists() else (root.parent if root is not None else None)
    while probe is not None and not probe.exists():
        probe = probe.parent if probe != probe.parent else None
    if probe is not None:
        try:
            du = shutil.disk_usage(probe)
            info.update(total=du.total, used=du.used, free=du.free, percent=round(du.used / du.total * 100, 1))
        except OSError:
            pass
    if sizes:
        info["recordings_bytes"] = sum(v["video_bytes"] + v["data_bytes"] for v in sizes.values())
        info["video_bytes"] = sum(v["video_bytes"] for v in sizes.values())
    rate = video_rate(rec)
    if rate and info["free"] is not None:
        info["hours_left"] = round(max(0, info["free"] - rec.min_free) / rate / 3600, 1)
        info["bytes_per_hour"] = round(rate * 3600)
    return info


def video_rate(rec: VoyoStreamRecorder) -> Optional[float]:
    """Bytes per second of the recorded video so far (capture.jsonl: size and PC start/end of each segment)."""
    if rec.root is None:
        return None
    total_b = total_s = 0.0
    for cj in list(rec.root.glob("*/capture/capture.jsonl"))[-40:]:
        try:
            for line in cj.read_text(encoding="utf-8").splitlines():
                r = json.loads(line)
                d = float(r.get("pc_end_epoch") or 0) - float(r.get("pc_start_epoch") or 0)
                if 1 < d < 7200 and r.get("bytes"):
                    total_b += float(r["bytes"])
                    total_s += d
        except (OSError, ValueError):
            continue
    return total_b / total_s if total_s > 60 else None


def current_state(rec: VoyoStreamRecorder, player_rec: Optional[VoyoStreamRecorder], player_enabled: bool,
                  heartbeat: Optional[dict], hb_age: Optional[float]) -> dict:
    """What the recording side is doing now: RECORDING / OPENING / REST / WAITING FOR DISK / PLAYER OFF /
    DISK ERROR / OFF, with a sentence for the page."""
    now = time.time()
    st = rec.status()
    nxt = (heartbeat or {}).get("next") if hb_age is not None and hb_age < HEARTBEAT_STALE_S else None
    nxt_txt = f" · next: {nxt['meeting']} {nxt['session_name']}" if nxt else ""

    def fresh(r):
        return r is not None and r.cur is not None and now - (r.last_sample_wall or 0) < SAMPLE_FRESH_S

    if not st["enabled"]:
        return {"state": "OFF", "level": "idle", "detail": "VOYO stream recording is switched off in the configuration"}
    if st["error"]:
        err = st["error"]
        if "no disk mounted" in err or "is missing" in err or "does not exist" in err:
            return {"state": "WAITING FOR DISK", "level": "warn", "detail": err}
        return {"state": "DISK ERROR", "level": "bad", "detail": err}
    if st.get("space_ok") is False:
        return {"state": "DISK FULL", "level": "bad",
                "detail": f"less than min_free_bytes free on {st.get('path')} - no video is recorded until there is "
                          "room again (delete old recordings on /disk)"}
    hb_fresh = hb_age is not None and hb_age < HEARTBEAT_STALE_S
    rs = (heartbeat or {}).get("recording") if hb_fresh and player_enabled else None
    rs = rs if isinstance(rs, dict) else None
    if fresh(player_rec):
        c = player_rec.cur
        sess = c.get("session") or {}
        cap = c.get("capture") or {}
        ep = c.get("episode") or {}
        label = (rs or {}).get("target", {}).get("label") if rs else None
        detail = label or f"{sess.get('meeting') or ''} {sess.get('session_name') or ''}".strip() or (c.get("title") or "")
        if ep.get("id"):
            detail += f" · VOYO episode {ep['id']}" + (f" ({str(ep.get('format')).upper()})" if ep.get("format") else "")
        problem = (rs or {}).get("problem")
        return {"state": "RECORDING", "level": "warn" if problem else "rec",
                "detail": detail + (f" · PROBLEM: {problem}" if problem else ""),
                "instance": c["stream_instance_id"], "since": c.get("detected_at"),
                "elapsed_s": round(now - float(c.get("detected_at_epoch") or now)),
                "segments": len(cap.get("segments") or []), "video_bytes": cap.get("bytes") or 0,
                "channel": "server_player", "episode_id": ep.get("id"), "problem": problem,
                "recording_s": (rs or {}).get("recording_s"), "recoveries": (rs or {}).get("recoveries")}
    if rs and rs.get("state") in SESSION_STATES:
        name, level, text = SESSION_STATES[rs["state"]]
        tgt = (rs.get("target") or {}).get("label") or "the session"
        why = rs.get("problem") or rs.get("selection") or rs.get("verified") or ""
        if rs["state"] == "RECORDING":           # the player records, but no sample reached this server lately
            why = "no video position from the player for over 30 s" + (f" - {rs['problem']}" if rs.get("problem") else "")
        return {"state": name, "level": level, "detail": f"{tgt}: {text}" + (f" - {why}" if why else ""),
                "episode_id": rs.get("episode_id"), "candidates": rs.get("candidates") or [],
                "problem": rs.get("problem"), "channel": "server_player"}
    if fresh(rec):
        c = rec.cur
        return {"state": "WATCHING (PC)", "level": "ok",
                "detail": f"the VOYO window on the PC / TV - {c.get('title') or c.get('asset') or ''}"
                          f" (timeline + sync recorded{', video too' if rec.capture_enabled else ''}){nxt_txt}",
                "instance": c["stream_instance_id"], "channel": "viewer"}
    if player_enabled:
        if hb_age is None or hb_age > HEARTBEAT_STALE_S:
            return {"state": "PLAYER OFF", "level": "warn",
                    "detail": "the server VOYO player is not running (sudo systemctl start f1-voyo-player)"
                              if hb_age is None or hb_age > 600 else
                              f"no news from the server VOYO player for {int(hb_age)} s"}
        hb = heartbeat or {}
        if hb.get("state") == "open":
            return {"state": "OPENING", "level": "ok",
                    "detail": f"VOYO opened for {hb.get('session') or 'the session'} - waiting for the video "
                              f"({hb.get('note') or 'starting'})"}
        if hb.get("problem"):
            return {"state": "REST", "level": "warn", "detail": f"{hb['problem']}{nxt_txt}"}
        return {"state": "REST", "level": "idle", "detail": f"nothing to do{nxt_txt}",
                "next": nxt}
    return {"state": "REST", "level": "idle", "detail": "nothing to do (the server VOYO player is not enabled)"}


def delete_package(rec: VoyoStreamRecorder, others: tuple, iid: str, what: str) -> str:
    """what = "video": only capture/ (the timeline / sync data stay) | "all": the whole package."""
    pkg = rec.package_dir(iid)
    if pkg is None:
        raise KeyError("unknown recording")
    for r in (rec, *others):
        if r is not None and r.cur is not None and r.cur["stream_instance_id"] == iid:
            raise PermissionError("this recording is still being written - wait until it is closed")
    if what == "video":
        n = 0
        cap = pkg / "capture"
        for f in cap.glob("*") if cap.exists() else []:
            if f.is_file() and CAPTURE_NAME.match(f.name):
                f.unlink()
                n += 1
        m = _read_json(pkg / "manifest.json") or {}
        m.setdefault("capture", {}).update(deleted_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                                           deleted_reason="deleted on the /disk page")
        _write_json(pkg / "manifest.json", m)
        rec._index_put(m)
        log.info("Recording %s: video deleted on the /disk page (%d segment(s)); its data kept", iid, n)
        return f"video deleted ({n} segment(s))"
    if what == "all":
        shutil.rmtree(pkg)
        idx = _read_json(rec.root / "index.json") or {}
        (idx.get("recordings") or {}).pop(iid, None)
        _write_json(rec.root / "index.json", idx)
        log.info("Recording %s deleted on the /disk page", iid)
        return "recording deleted"
    raise ValueError("what = video | all")
