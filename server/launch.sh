#!/usr/bin/env bash
# F1 dashboard - Linux server (the primary deployment).
#
#   ./server/launch.sh                 # mode from the config (AUTO: LIVE while a session is on, else VOD)
#   ./server/launch.sh --live|--vod|--test|--replay [file]
#   ./server/launch.sh --f1-status     # F1 TV sign-in state (no secrets)
#   ./server/launch.sh --diagnose 120  # which live topics actually arrive, then exit
#
# Runs main/main.py (the shared code) with server/config/server.toml on top of
# main/config/config.toml. Environment (all optional; server/.env is read if present):
#   F1DASH_DATA_DIR       runtime data: sign-in, sync state, recordings, caches  (default: <repo>/data)
#   F1DASH_LOG_DIR        log directory when run by hand                          (default: $F1DASH_DATA_DIR/logs)
#   F1DASH_VENV           Python virtual environment                              (default: <repo>/.venv)
#   F1DASH_CONFIG_OVERLAY overlay file(s), ':'-separated                         (default: server/config/server.toml)
#   PYTHON                interpreter used to create the venv (3.11+)             (default: python3)
#   F1TV_TOKEN            F1 TV subscription token (instead of the stored sign-in)
#   any F1DASH_<SECTION>_<KEY> overrides one config value, e.g. F1DASH_SERVER_PORT=8090
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(dirname "$HERE")"
if [ -f "$HERE/.env" ]; then set -a; . "$HERE/.env"; set +a; fi
PY="${PYTHON:-python3}"
VENV="${F1DASH_VENV:-$REPO/.venv}"
export F1DASH_DATA_DIR="${F1DASH_DATA_DIR:-$REPO/data}"
export F1DASH_CONFIG_OVERLAY="${F1DASH_CONFIG_OVERLAY:-$HERE/config/server.toml}"
export PYTHONUNBUFFERED=1

if [ ! -x "$VENV/bin/python" ]; then
  "$PY" - <<'PYCHECK' || { echo "Python 3.11 or newer is required (set PYTHON=python3.12 ...)" >&2; exit 1; }
import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)
PYCHECK
  echo "Creating the Python environment in $VENV ..."
  "$PY" -m venv "$VENV"
fi
"$VENV/bin/python" -m pip install -q --disable-pip-version-check -r "$REPO/main/requirements.txt"
mkdir -p "$F1DASH_DATA_DIR"
cd "$REPO/main"
if [ -z "${INVOCATION_ID:-}" ] && [ -t 1 ]; then
  # started by hand: also keep a log file (systemd / docker log to the journal / docker logs)
  LOG_DIR="${F1DASH_LOG_DIR:-$F1DASH_DATA_DIR/logs}"
  mkdir -p "$LOG_DIR"
  echo "Log: $LOG_DIR/server.log   Data: $F1DASH_DATA_DIR"
  "$VENV/bin/python" main.py "$@" 2>&1 | tee -a "$LOG_DIR/server.log"
  exit "${PIPESTATUS[0]}"
fi
exec "$VENV/bin/python" main.py "$@"
