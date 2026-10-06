"""Configuration loading (TOML file + environment overrides)."""
from __future__ import annotations

import copy
import logging
import os
import tomllib
from pathlib import Path
from typing import Any

log = logging.getLogger("config")

# main/ = the shared code (server package, dashboard, tools, tests, default config). The runtime
# data (F1 TV sign-in, VOYO browser profile, sync state, F1-feed recordings, caches) lives OUTSIDE
# the code in <repository>/data - the same place as before the main/ + server/ + "pc variant/"
# split, so nothing is lost - or wherever F1DASH_DATA_DIR points (the Linux service).
PROJECT_ROOT = Path(__file__).resolve().parent.parent
REPO_ROOT = PROJECT_ROOT.parent
DEFAULT_CONFIG_PATH = PROJECT_ROOT / "config" / "config.toml"
DATA_DIR = Path(os.environ["F1DASH_DATA_DIR"]).expanduser() if os.environ.get("F1DASH_DATA_DIR") \
    else REPO_ROOT / "data"
DASHBOARD_DIR = PROJECT_ROOT / "dashboard"

DEFAULTS: dict[str, Any] = {
    "server": {"host": "0.0.0.0", "port": 8080, "log_level": "INFO"},
    "source": {"mode": "auto", "delay_seconds": 0.0},
    "f1_tv": {"subscription": True, "open_browser": True, "safety_car_position_keys": [],
              "auth_file": "data/auth/f1tv_auth.json"},
    "live": {
        "transport": "auto",
        "f1tv_token": "",
        "topics": [
            "Heartbeat", "SessionInfo", "SessionStatus", "SessionData", "ExtrapolatedClock",
            "LapCount", "TrackStatus", "DriverList", "TimingData", "TimingDataF1",
            "TimingAppData", "TimingStats", "RaceControlMessages", "WeatherData",
            "TeamRadio", "TopThree", "PitLaneTimeCollection", "CurrentTyres",
            "LapSeries", "Position.z", "CarData.z",
            "TyreStintSeries", "AudioStreams", "ContentStreams", "TlaRcm", "RcmSeries",
            "PitStopSeries", "PitStop", "DriverRaceInfo", "OvertakeSeries",
            "ChampionshipPrediction", "WeatherDataSeries",
        ],
        "reconnect_min": 2.0,
        "reconnect_max": 60.0,
        "silence_timeout": 60.0,
        "record": True,
        "archive_follow": True,
        "archive_poll_seconds": 3.0,
    },
    "replay": {"source": "data/recordings/sample-2026-japan-race.json.gz", "speed": 1.0,
               "start_offset": "auto", "loop": True},
    "test": {"circuit": "jp-1962", "laps": 50, "time_scale": 1.0},
    "tracks": {
        "api_url": "https://api.multiviewer.app/api/v1/circuits/{circuit_key}/{year}",
        "learn_pitlane": True,
        "learn_outline": True,
    },
    # weather report popup (server/weather.py)
    "weather": {"enabled": True, "every_laps": 15, "display_seconds": 15, "forecast_cache_seconds": 600,
                "horizon_hours": 3, "test_auto": False,
                "thresholds": {"drizzle_mm_h": 0.1, "light_mm_h": 0.5, "medium_mm_h": 2.5, "heavy_mm_h": 7.6},
                "radar": {"enabled": True, "radius_km": 100, "grid_points": 9, "animation": True,
                          "history_minutes": 30, "forecast_minutes": 30, "refresh_seconds": 180,
                          "max_age_seconds": 900, "max_zoom": 7}},
    "dashboard": {
        "interp_delay_ms": 1200, "map_fps": 30, "animations": "full",
        "pulse_period_ms": 2400, "reorder_ms": 450, "auto_cycle_seconds": 20,
        "race_control_max": 60,
        "chase_gap_seconds": 1.5,
    },
    "remote": {"enabled": True, "token": "", "allow_get": True, "keymap": {},
               "keymap_video": {}, "keymap_video_focus": {}},
    "voyo": {"enabled": False, "mode": "window", "url": "https://voyo.si/", "hls_url": "",
             "default_tv_mode": "RACE_VIEW", "check_reachability": True, "check_interval_seconds": 60,
             "fallback_when_unreachable": True, "capture_compat": "no-gpu",
             "recording": {"enabled": True, "record_metadata": True, "record_timeline": True, "record_sync": True,
                           "timeline_interval_seconds": 1.0, "pair_interval_seconds": 10.0,
                           "path": "data/voyo_streams", "create_path_if_missing": True, "require_mount": "",
                           "min_free_bytes": 2 * 1024 ** 3, "record_video_capture": False, "ffmpeg": "ffmpeg",
                           "capture_fps": 30, "capture_crf": 23, "capture_segment_seconds": 60,
                           "capture_audio_device": "", "capture_max_segment_bytes": 4 * 1024 ** 3,
                           "keep_practice1_days": 7, "keep_practice2_days": 7, "keep_practice3_days": 7,
                           "keep_sprint_qualifying_days": 14, "keep_sprint_days": 14,
                           "keep_qualifying_days": 14, "keep_race_days": 30, "keep_other_days": 7},
             "server_player": {"enabled": False, "stream_url": "", "when": "schedule",
                               "record_sessions": ["practice1", "practice2", "practice3", "sprint_qualifying",
                                                   "sprint", "qualifying", "race"],
                               "lead_minutes": 15, "trail_minutes": 30, "keep_open_while_feed_live": True,
                               "record_video": True, "fullscreen_video": True, "browser": "", "display": ":90",
                               "resolution": "1920x1080", "cdp_port": 9224,
                               "profile": "data/browser-profiles/voyo-server", "audio": True, "vnc_port": 5900}},
    "sync": {"enabled": True, "mode": "AUTO", "buffer_seconds": 120.0, "broadcast_delay_seconds": 5.0,
             "adjustment_step": 0.25, "auto_drift_correction": True, "voyo_playback_clock": True,
             "drift_slew_seconds_per_second": 0.05, "mark_reaction_seconds": 0.2,
             "mark_window_seconds": 40.0, "position_lookahead_ms": 2500.0, "marks_used": 5, "max_hold_seconds": 1800.0,
             "cdp_port": 9223, "clock_poll_hz": 5.0, "allow_remote_clock": False,
             "broadcast_lead_seconds": "", "lead_uncertainty_seconds": 1200.0, "anchor_error_seconds": 0.25,
             "anchors_agree_seconds": 0.5, "drift_warning_seconds": 2.0, "outlier_seconds": 1.0},
    "vod": {"session_key": "auto", "openf1_url": "https://api.openf1.org/v1", "bare_gp_title_is_race": True,
            "race_min_video_seconds": 8100.0, "assume_recent_season_days": 21.0, "preload_data": False},
}


def _deep_update(base: dict, upd: dict) -> dict:
    for k, v in upd.items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            _deep_update(base[k], v)
        else:
            base[k] = v
    return base


def _coerce(value: str, like: Any) -> Any:
    if isinstance(like, bool):
        return value.strip().lower() in ("1", "true", "yes", "on")
    if isinstance(like, int):
        return int(value)
    if isinstance(like, float):
        return float(value)
    if isinstance(like, list):
        return [v.strip() for v in value.split(",") if v.strip()]
    return value


def load_config(path: str | os.PathLike | None = None, overlays: list | None = None) -> dict[str, Any]:
    """The shared main/config/config.toml (or ``path``), then the deployment's overlay files
    (``overlays`` + F1DASH_CONFIG_OVERLAY, separated by os.pathsep): only the keys they set
    change - server/config/server.toml for the Linux server, nothing for the Windows PC."""
    cfg = copy.deepcopy(DEFAULTS)
    cfg_path = Path(path) if path else DEFAULT_CONFIG_PATH
    if cfg_path.exists():
        with open(cfg_path, "rb") as fh:
            _deep_update(cfg, tomllib.load(fh))
        log.info("Loaded configuration from %s", cfg_path)
    else:
        log.warning("Config file %s not found - using defaults", cfg_path)
    env_ov = [p for p in (os.environ.get("F1DASH_CONFIG_OVERLAY") or "").split(os.pathsep) if p.strip()]
    for ov in list(overlays or []) + env_ov:
        ov_path = Path(ov).expanduser()
        if ov_path.exists():
            with open(ov_path, "rb") as fh:
                _deep_update(cfg, tomllib.load(fh))
            log.info("Configuration overlay %s applied", ov_path)
        else:
            log.warning("Configuration overlay %s not found - ignored", ov_path)

    # Environment overrides: F1DASH_<SECTION>_<KEY>, and for a sub-section ([voyo.recording])
    # F1DASH_<SECTION>_<SUB>_<KEY>, e.g. F1DASH_VOYO_RECORDING_PATH
    def _env_overrides(values: dict, prefix: str, depth: int) -> None:
        for key, current in list(values.items()):
            if isinstance(current, dict):
                if depth == 0:
                    _env_overrides(current, f"{prefix}_{key}", 1)
                continue
            name = f"{prefix}_{key}".upper()
            env = os.environ.get(name)
            if env is not None:
                try:
                    values[key] = _coerce(env, current)
                except ValueError:
                    log.error("Invalid value for %s: %r", name, env)

    for section, values in cfg.items():
        if isinstance(values, dict):
            _env_overrides(values, f"F1DASH_{section}", 0)

    # Convenience: plain F1TV_TOKEN env var
    if os.environ.get("F1TV_TOKEN"):
        cfg["live"]["f1tv_token"] = os.environ["F1TV_TOKEN"]
    return cfg


def resolve_path(p: str | os.PathLike) -> Path:
    """Absolute paths as they are; "data/..." in the data directory (see DATA_DIR); anything else
    relative to main/."""
    path = Path(p).expanduser()
    if path.is_absolute():
        return path
    if path.parts and path.parts[0] == "data":
        return DATA_DIR.joinpath(*path.parts[1:])
    return PROJECT_ROOT / path
