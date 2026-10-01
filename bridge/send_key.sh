#!/bin/sh
# Send one remote key to the dashboard.  Usage:  ./send_key.sh KEY_UP [server] [token]
# Works with curl or (busybox) wget.
KEY="${1:?usage: send_key.sh KEY_NAME [http://server:8080] [token]}"
SERVER="${2:-${F1DASH_SERVER:-http://127.0.0.1:8080}}"
TOKEN="${3:-${F1DASH_TOKEN:-}}"
URL="$SERVER/api/remote/key?key=$KEY"
[ -n "$TOKEN" ] && URL="$URL&token=$TOKEN"
if command -v curl >/dev/null 2>&1; then
  curl -s "$URL"; echo
else
  wget -q -O - "$URL"; echo
fi
