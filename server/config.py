"""Configuration loading (TOML file + environment overrides)."""
from __future__ import annotations

import copy
import logging
import os
import tomllib
from pathlib import Path
from typing import Any

log = logging.getLogger("config")

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG_PATH = PROJECT_ROOT / "config" / "config.toml"
DATA_DIR = PROJECT_ROOT / "data"
DASHBOARD_DIR = PROJECT_ROOT / "dashboard"

DEFAULTS: dict[str, Any] = {
    "server": {"host": "0.0.0.0", "port": 8080, "log_level": "INFO"},
    "source": {"mode": "live", "delay_seconds": 0.0},
    "live": {
        "transport": "auto",
        "f1tv_token": "",
        "topics": [
            "Heartbeat", "SessionInfo", "SessionStatus", "SessionData", "ExtrapolatedClock",
            "LapCount", "TrackStatus", "DriverList", "TimingData", "TimingDataF1",
            "TimingAppData", "TimingStats", "RaceControlMessages", "WeatherData",
            "TeamRadio", "TopThree", "PitLaneTimeCollection", "CurrentTyres",
            "LapSeries", "Position.z", "CarData.z",
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
    "dashboard": {
        "interp_delay_ms": 1200, "map_fps": 30, "animations": "full",
        "pulse_period_ms": 2400, "reorder_ms": 450, "auto_cycle_seconds": 20,
        "race_control_max": 60,
    },
    "remote": {"enabled": True, "token": "", "allow_get": True, "keymap": {},
               "keymap_video": {}, "keymap_video_focus": {}},
    "voyo": {"enabled": False, "mode": "window", "url": "https://voyo.si/", "hls_url": "",
             "default_tv_mode": "RACE_VIEW", "check_reachability": True, "check_interval_seconds": 60,
             "fallback_when_unreachable": True},
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


def load_config(path: str | os.PathLike | None = None) -> dict[str, Any]:
    cfg = copy.deepcopy(DEFAULTS)
    cfg_path = Path(path) if path else DEFAULT_CONFIG_PATH
    if cfg_path.exists():
        with open(cfg_path, "rb") as fh:
            _deep_update(cfg, tomllib.load(fh))
        log.info("Loaded configuration from %s", cfg_path)
    else:
        log.warning("Config file %s not found - using defaults", cfg_path)

    # Environment overrides: F1DASH_<SECTION>_<KEY>
    for section, values in cfg.items():
        if not isinstance(values, dict):
            continue
        for key, current in list(values.items()):
            if isinstance(current, dict):
                continue
            env = os.environ.get(f"F1DASH_{section}_{key}".upper())
            if env is not None:
                try:
                    values[key] = _coerce(env, current)
                except ValueError:
                    log.error("Invalid value for F1DASH_%s_%s: %r", section.upper(), key.upper(), env)

    # Convenience: plain F1TV_TOKEN env var
    if os.environ.get("F1TV_TOKEN"):
        cfg["live"]["f1tv_token"] = os.environ["F1TV_TOKEN"]
    return cfg


def resolve_path(p: str | os.PathLike) -> Path:
    path = Path(p)
    return path if path.is_absolute() else (PROJECT_ROOT / path)
