#!/usr/bin/env bash
# Forgot the /disk password? Removes it (and every /disk session) - on the server only, as root / the
# service user. On the next start the server writes a new one-time setup code (sudo cat it) and /disk
# shows the first-time setup again. Trusted /remote device and /tv sessions are kept.
#   sudo ./server/reset-disk-password.sh && sudo systemctl restart f1-dashboard
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(dirname "$HERE")"
if [ -f "$HERE/.env" ]; then set -a; . "$HERE/.env"; set +a; fi
F="${F1DASH_DATA_DIR:-$REPO/data}/auth/security.json"
[ -f "$F" ] || { echo "no $F - nothing to reset"; exit 0; }
python3 - "$F" <<'PY'
import json, os, sys
p = sys.argv[1]
st = os.stat(p)                       # keep the owner (the service user f1), not root
d = json.load(open(p))
d["disk"] = {"hash": None}
d["disk_sessions"] = {}
tmp = p + ".tmp"
fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
with os.fdopen(fd, "w") as fh:
    json.dump(d, fh, indent=1)
os.chown(tmp, st.st_uid, st.st_gid)
os.replace(tmp, p)
print("The /disk password was removed. Restart the server, then: sudo cat", os.path.join(os.path.dirname(p), "disk-setup-code"))
PY
