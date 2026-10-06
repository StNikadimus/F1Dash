#!/usr/bin/env bash
# Server VOYO player - the server opens VOYO itself and records it (main/tools/voyo_server_player.py).
#   ./server/voyo-player.sh status     what is installed / configured, the next sessions
#   ./server/voyo-player.sh login      once: sign in to VOYO on the virtual screen (VNC via SSH tunnel)
#   ./server/voyo-player.sh test --minutes 5   open + record now (the dashboard server must run)
#   ./server/voyo-player.sh run        the service (server/systemd/f1-voyo-player.service)
# Same environment as server/launch.sh (server/.env, F1DASH_DATA_DIR, the overlay, the venv).
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(dirname "$HERE")"
if [ -f "$HERE/.env" ]; then set -a; . "$HERE/.env"; set +a; fi
VENV="${F1DASH_VENV:-$REPO/.venv}"
export F1DASH_DATA_DIR="${F1DASH_DATA_DIR:-$REPO/data}"
export F1DASH_CONFIG_OVERLAY="${F1DASH_CONFIG_OVERLAY:-$HERE/config/server.toml}"
export PYTHONUNBUFFERED=1
if [ ! -x "$VENV/bin/python" ]; then
  echo "Python environment $VENV missing - start the server once with server/launch.sh" >&2
  exit 1
fi
cd "$REPO/main"
exec "$VENV/bin/python" tools/voyo_server_player.py "${@:-status}"
