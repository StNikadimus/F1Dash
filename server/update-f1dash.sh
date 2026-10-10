#!/usr/bin/env bash
# F1Dash - safe update of the Linux server deployment (also usable in a development checkout).
#
#   sudo /opt/f1-dashboard/server/update-f1dash.sh               # update to origin/main, test, restart if needed
#   sudo /opt/f1-dashboard/server/update-f1dash.sh --dry-run     # only show what would happen - changes nothing
#   sudo /opt/f1-dashboard/server/update-f1dash.sh --help
#
# What it does (server/README.md "Updating the server"):
#   1. checks prerequisites, the systemd unit, the checkout (refuses local changes, other branches, local commits)
#   2. never interrupts a recording: refuses (or --wait-idle) while the VOYO player records / opens VOYO
#   3. backs up the runtime security state (auth/, tls/, state files, .env, unit) to /var/backups/f1-dashboard
#   4. fast-forwards the branch, installs the Python requirements, runs the tests in a THROW-AWAY data dir
#      (a failure puts the previous commit back - the running service is not touched)
#   5. checks the HTTPS certificate (creates / renews it with make-https-cert.sh only when missing / expiring /
#      not for this server's address - never a custom one)
#   6. restarts f1-dashboard only when something that matters changed, then checks it: http, https, the
#      unauthenticated protected endpoints (401), /tv always starting with a fresh approval request.
#      A failed check after a restart rolls the code back automatically when the previous version was healthy.
#
# Exit codes: 0 ok / nothing to do, 2 usage, 3 prerequisites / permissions / configuration, 4 git (local changes,
# other branch, diverged, fetch failed), 5 unsafe now (recording active, disk missing) - nothing changed,
# 6 tests or dependencies failed - code rolled back, service untouched, 7 service unhealthy after the restart
# (rolled back when that was safe - see the message), 8 backup failed - nothing changed.
#
# Secrets: never printed or traced (no `set -x`; .env is only read by the service user's shell, never shown).
set -Eeuo pipefail
set +x
umask 077

PROG="update-f1dash"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(dirname "$HERE")"

# --- settings (environment overrides are for unusual layouts and the test suite) -----------------------
SERVICE="${F1DASH_UPDATE_SERVICE:-f1-dashboard.service}"
PLAYER_SERVICE="${F1DASH_UPDATE_PLAYER_SERVICE:-f1-voyo-player.service}"
BACKUP_ROOT="${F1DASH_UPDATE_BACKUP_DIR:-/var/backups/f1-dashboard}"
LOCK_FILE="${F1DASH_UPDATE_LOCK:-/run/lock/f1dash-update.lock}"
VENV="${F1DASH_VENV:-$REPO/.venv}"
ENV_FILE="$HERE/.env"
TESTS="${F1DASH_UPDATE_TESTS:-tests.test_security tests.test_disk_page tests.test_tv_live tests.test_voyo_server_player tests.test_phone_remote}"
REQUIRE_ROOT="${F1DASH_UPDATE_REQUIRE_ROOT:-1}"
HEALTH_TIMEOUT="${F1DASH_UPDATE_HEALTH_TIMEOUT:-120}"

BRANCH="main"; DRY=0; SKIP_TESTS=0; SKIP_DEPS=0; FORCE_RESTART=0; NO_RESTART=0; WAIT_IDLE=0; KEEP=10
ALLOW_NO_DISK=0; VERIFY_ONLY=0; ROLLBACK=0; ROLLBACK_TO=""

usage() {
  cat <<'EOF'
Usage: sudo server/update-f1dash.sh [options]

Updates the F1 dashboard checkout to origin/<branch> and restarts f1-dashboard safely.

  --dry-run            show every planned action; changes nothing (no fetch, no backup, no restart)
  --branch NAME        branch to follow (default: main); the checkout must be on it
  --wait-idle MINUTES  if a recording is running, wait up to MINUTES for it to end instead of refusing
  --skip-tests         do not run the test suite before restarting (not recommended)
  --skip-deps          do not run pip install (offline; the service's launch.sh still runs it at start)
  --no-restart         update but do not restart (a later run without it restarts)
  --force-restart      restart even when nothing changed (still refused while recording)
  --allow-missing-disk continue although the recording disk is not mounted (recording stays paused)
  --keep N             backups to keep in /var/backups/f1-dashboard (default 10)
  --verify-only        only check the running service (http, https, protected endpoints, /tv approval)
  --rollback [COMMIT]  go back to the commit deployed before the last update (or COMMIT), then restart
  -h, --help           this text

Exit codes: 0 ok, 2 usage, 3 prerequisites, 4 git state, 5 unsafe now (recording / disk),
6 tests or dependencies failed (rolled back), 7 unhealthy after restart, 8 backup failed.
EOF
}

while [ $# -gt 0 ]; do
  case "$1" in
    --dry-run) DRY=1 ;;
    --branch) shift; BRANCH="${1:-}"; [ -n "$BRANCH" ] || { usage >&2; exit 2; } ;;
    --wait-idle) shift; WAIT_IDLE="${1:-}"; [[ "$WAIT_IDLE" =~ ^[0-9]+$ ]] || { echo "--wait-idle needs minutes" >&2; exit 2; } ;;
    --skip-tests) SKIP_TESTS=1 ;;
    --skip-deps) SKIP_DEPS=1 ;;
    --no-restart) NO_RESTART=1 ;;
    --force-restart) FORCE_RESTART=1 ;;
    --allow-missing-disk) ALLOW_NO_DISK=1 ;;
    --keep) shift; KEEP="${1:-}"; [[ "$KEEP" =~ ^[1-9][0-9]*$ ]] || { echo "--keep needs a number >= 1" >&2; exit 2; } ;;
    --verify-only) VERIFY_ONLY=1 ;;
    --rollback) ROLLBACK=1; if [ $# -gt 1 ] && [[ "${2:-}" =~ ^[0-9a-f]{7,40}$ ]]; then shift; ROLLBACK_TO="$1"; fi ;;
    -h|--help) usage; exit 0 ;;
    *) echo "unknown option: $1" >&2; usage >&2; exit 2 ;;
  esac
  shift
done
[[ "$BRANCH" =~ ^[A-Za-z0-9._/-]+$ ]] || { echo "invalid branch name" >&2; exit 2; }

# --- output --------------------------------------------------------------------------------------------
LOG_FILE=""
_log() { if [ -n "$LOG_FILE" ]; then printf '%s %s\n' "$(date '+%F %T')" "$*" >>"$LOG_FILE" 2>/dev/null || true; fi; }
say()  { printf '[%s] %s\n' "$PROG" "$*"; _log "$*"; }
step() { printf '\n[%s] == %s\n' "$PROG" "$*"; _log "== $*"; }
warn() { printf '[%s] WARNING: %s\n' "$PROG" "$*" >&2; _log "WARNING: $*"; }
die()  { local code="$1"; shift; printf '[%s] ERROR: %s\n' "$PROG" "$*" >&2; _log "ERROR($code): $*"; exit "$code"; }
plan() { if [ "$DRY" = 1 ]; then say "[dry-run] would $*"; fi; }

TMPD="$(mktemp -d)"
cleanup() { rm -rf "$TMPD" 2>/dev/null || true; }
trap cleanup EXIT
trap 'die 1 "unexpected failure at line $LINENO (nothing after this step was done)"' ERR

need() { command -v "$1" >/dev/null 2>&1 || die 3 "'$1' is not installed (sudo apt install $2)"; }

# run a command as another user (no shell string - no quoting / injection issues)
run_as() {
  local u="$1"; shift
  if [ "$(id -un)" = "$u" ]; then "$@"; else sudo -u "$u" -H -- "$@"; fi
}

# ===================================================================== 1. prerequisites and mode
step "Checking prerequisites"
need git git; need curl curl; need tar tar; need flock util-linux; need df coreutils; need stat coreutils
[ -d "$REPO/.git" ] || die 3 "$REPO is not a git checkout"
REPO_OWNER="$(stat -c %U "$REPO")"
G() { run_as "$REPO_OWNER" git -C "$REPO" "$@"; }

MODE="development"
UNIT_TEXT=""
if command -v systemctl >/dev/null 2>&1 && UNIT_TEXT="$(systemctl cat "$SERVICE" 2>/dev/null)"; then
  unit_val() { printf '%s\n' "$UNIT_TEXT" | sed -n "s/^[[:space:]]*$1=//p" | tail -n1; }
  UNIT_WD="$(unit_val WorkingDirectory)"
  if [ -n "$UNIT_WD" ] && [ "$(readlink -f "$UNIT_WD" 2>/dev/null || echo "$UNIT_WD")" = "$(readlink -f "$REPO/main")" ]; then
    MODE="production"
  fi
fi
if [ "$MODE" = production ]; then
  SVC_USER="$(unit_val User)"; SVC_USER="${SVC_USER:-root}"
  UNIT_EXEC="$(unit_val ExecStart)"
  UNIT_DATA="$(printf '%s\n' "$UNIT_TEXT" | sed -n 's/^[[:space:]]*Environment=.*F1DASH_DATA_DIR=\([^[:space:]"]*\).*/\1/p' | tail -n1)"
  UNIT_MOUNTS="$(unit_val RequiresMountsFor)"
  # the public gateway (Tailscale Funnel) is switched on by a unit drop-in (server/systemd/public-gateway.conf)
  mapfile -t UNIT_PUBLIC_ENV < <(printf '%s\n' "$UNIT_TEXT" | grep -E '^[[:space:]]*Environment=' |
    grep -oE 'F1DASH_PUBLIC_(ENABLED|HOSTNAME|PORT)=[A-Za-z0-9.:-]*' || true)
  if [ "$REQUIRE_ROOT" = 1 ] && [ "$(id -u)" != 0 ]; then
    die 3 "production server: run it as root (sudo $0 ...) - it backs up root-only files and restarts $SERVICE"
  fi
  case "$UNIT_EXEC" in "$REPO"/*) ;; *) die 3 "$SERVICE runs '$UNIT_EXEC', not a script of $REPO - check the unit" ;; esac
  id "$SVC_USER" >/dev/null 2>&1 || die 3 "service user '$SVC_USER' of $SERVICE does not exist"
  need findmnt util-linux
  say "production server: $SERVICE (user $SVC_USER), checkout $REPO (owner $REPO_OWNER)"
else
  SVC_USER="$(id -un)"; UNIT_DATA=""; UNIT_MOUNTS=""; UNIT_PUBLIC_ENV=()
  say "development checkout $REPO (no $SERVICE running from it): only git, dependencies and tests - no backup, certificate or restart"
fi
[ "$DRY" = 1 ] && say "DRY RUN - nothing will be changed"
PY="$VENV/bin/python"
if [ ! -x "$PY" ]; then
  [ "$MODE" = production ] && die 3 "no Python environment at $VENV (start the service once: it creates it)"
  PY="$(command -v python3 || true)"; [ -n "$PY" ] || die 3 "python3 not found"
  warn "no virtual environment at $VENV - using $PY"
fi
VENV_OWNER="$( [ -d "$VENV" ] && stat -c %U "$VENV" || echo "$REPO_OWNER")"

# one update at a time (not in a dry run: it writes nothing)
if [ "$DRY" = 0 ] && [ "$MODE" = production ]; then
  mkdir -p "$(dirname "$LOCK_FILE")" 2>/dev/null || true
  exec 9>"$LOCK_FILE" || die 3 "cannot open the lock file $LOCK_FILE"
  flock -n 9 || die 3 "another update is running (lock $LOCK_FILE)"
fi
if [ "$DRY" = 0 ] && [ "$MODE" = production ]; then
  if ! { mkdir -p "$BACKUP_ROOT" && chmod 700 "$BACKUP_ROOT"; }; then die 8 "cannot create $BACKUP_ROOT"; fi
  LOG_FILE="$BACKUP_ROOT/update.log"; touch "$LOG_FILE"; chmod 600 "$LOG_FILE"
  _log "---- $PROG started by $(id -un) (sudo user ${SUDO_USER:-none}), options: dry=$DRY branch=$BRANCH"
fi

# ===================================================================== 2. configuration (as the service user)
# .env is sourced by the service user's shell only (as launch.sh does) - it is never read or shown here.
probe_config() {
  run_as "$SVC_USER" env -i PATH=/usr/local/bin:/usr/bin:/bin HOME="$TMPD" LANG=C.UTF-8 \
    F1DASH_DATA_DIR="${UNIT_DATA:-}" "${UNIT_PUBLIC_ENV[@]}" bash -c '
      set -a; if [ -r "$1" ]; then . "$1" >/dev/null 2>&1; fi; set +a
      export F1DASH_DATA_DIR="${F1DASH_DATA_DIR:-$2/data}"
      export F1DASH_CONFIG_OVERLAY="${F1DASH_CONFIG_OVERLAY:-$2/server/config/server.toml}"
      cd "$2/main" && exec "$3" - <<'"'"'PY'"'"'
import os, sys
sys.path.insert(0, os.getcwd())
from server.config import DATA_DIR, load_config, resolve_path
c = load_config()
rc = c["voyo"]["recording"]
vals = {"data_dir": DATA_DIR, "port": c["server"]["port"], "https_port": int(c["server"].get("https_port") or 0),
        "tls_cert": resolve_path(str(c["server"].get("tls_cert") or "data/tls/cert.pem")),
        "tls_key": resolve_path(str(c["server"].get("tls_key") or "data/tls/key.pem")),
        "rec_path": resolve_path(str(rc.get("path") or "")), "require_mount": rc.get("require_mount") or "",
        "mount_marker": rc.get("mount_marker") or "", "protect_dashboard": int(bool(c["security"].get("protect_dashboard"))),
        "player_enabled": int(bool(c["voyo"]["server_player"].get("enabled"))),
        "public_enabled": int(bool(c["public"].get("enabled"))), "public_host": c["public"].get("hostname") or "",
        "public_port": c["public"].get("port") or 0}
for k, v in vals.items():
    print(k + chr(9) + str(v).replace(chr(9), chr(32)).replace(chr(10), chr(32)))
PY
    ' _ "$ENV_FILE" "$REPO" "$PY"
}
load_config_values() {
  local out k v
  out="$(probe_config 2>"$TMPD/probe.err")" || { sed 's/^/    /' "$TMPD/probe.err" >&2; die 3 "cannot read the configuration (above)"; }
  while IFS=$'\t' read -r k v; do
    case "$k" in
      data_dir) CFG_DATA="$v" ;; port) CFG_PORT="$v" ;; https_port) CFG_HTTPS="$v" ;; tls_cert) CFG_CERT="$v" ;;
      tls_key) CFG_KEY="$v" ;; rec_path) CFG_REC="$v" ;; require_mount) CFG_MOUNT="$v" ;; mount_marker) CFG_MARKER="$v" ;;
      protect_dashboard) CFG_PROTECT="$v" ;; player_enabled) CFG_PLAYER="$v" ;;
      public_enabled) CFG_PUB="$v" ;; public_host) CFG_PUB_HOST="$v" ;; public_port) CFG_PUB_PORT="$v" ;;
    esac
  done <<<"$out"
  [[ "${CFG_PORT:-}" =~ ^[0-9]+$ ]] || die 3 "no server port in the configuration"
}
step "Reading the configuration"
load_config_values
say "data: $CFG_DATA · http :$CFG_PORT · https :${CFG_HTTPS} · recordings: $CFG_REC${CFG_MOUNT:+ (needs the disk at $CFG_MOUNT)} · server VOYO player: $([ "${CFG_PLAYER:-0}" = 1 ] && echo on || echo off)"
if [ -e "$ENV_FILE" ]; then
  ENV_STAT="$(stat -c '%U:%G %a' "$ENV_FILE")"
  case "${ENV_STAT##* }" in 600|400|640) ;; *) warn "$ENV_FILE has mode ${ENV_STAT##* } - it holds secrets, 600 is recommended (not changed)" ;; esac
  say ".env: $ENV_STAT (kept as it is)"
else
  ENV_STAT=""; warn "no $ENV_FILE - the defaults are used"
fi

# ===================================================================== helpers: HTTP, recording, disk
BODY="$TMPD/body"
http() {   # http METHOD URL [curl args...] -> prints the status code, body in $BODY
  local m="$1" u="$2" c; shift 2
  c="$(curl -sk --max-time 8 -X "$m" -o "$BODY" -w '%{http_code}' "$@" "$u" 2>/dev/null)" || true
  printf '%s' "${c:-000}"
}
BASE="http://127.0.0.1:$CFG_PORT"

recording_reason() {   # prints why a restart would interrupt something now; empty = idle
  local code
  if [ "$MODE" = production ] && systemctl is-active --quiet "$SERVICE" 2>/dev/null; then
    code="$(http GET "$BASE/api/health")"
    if [ "$code" = 200 ] && grep -Eq '"busy": ?true' "$BODY"; then
      sed -n 's/.*"state": \{0,1\}"\([^"]*\)".*/recorder state: \1/p' "$BODY" | head -n1; return
    fi
  fi
  if [ -f "$CFG_DATA/live/index.m3u8" ] && [ -n "$(find "$CFG_DATA/live/index.m3u8" -mmin -1 2>/dev/null || true)" ]; then
    echo "the live stream is being written ($CFG_DATA/live)"; return
  fi
  if pgrep -u "$SVC_USER" -f 'ffmpeg .*(x11grab|kmsgrab|segment)' >/dev/null 2>&1; then
    echo "an ffmpeg capture of $SVC_USER is running"; return
  fi
  if [ -n "${CFG_REC:-}" ] && [ -d "$CFG_REC" ] && [ -n "$(find "$CFG_REC" -maxdepth 3 -type f -mmin -2 -print -quit 2>/dev/null || true)" ]; then
    echo "a recording file in $CFG_REC changed in the last 2 minutes"; return
  fi
}
wait_until_idle() {   # $1 = what we are about to do, $2 = what has been changed already (for the message)
  local why left done_msg="${2:-Nothing was changed.}"
  why="$(recording_reason)"
  [ -z "$why" ] && return 0
  if [ "$DRY" = 1 ]; then warn "busy now ($why) - a real run would refuse or wait (--wait-idle)"; return 0; fi
  if [ "$WAIT_IDLE" -gt 0 ]; then
    left=$((WAIT_IDLE * 60)); say "busy ($why) - waiting up to $WAIT_IDLE min before $1"
    while [ -n "$why" ] && [ "$left" -gt 0 ]; do sleep 60; left=$((left - 60)); why="$(recording_reason)"; done
    [ -z "$why" ] && return 0
  fi
  die 5 "not now: $why - $1 would interrupt it. Run again after the session (or with --wait-idle MINUTES). $done_msg"
}
check_disk() {
  [ "$MODE" = production ] || return 0
  local mnt="${CFG_MOUNT:-}"; [ -n "$mnt" ] || mnt="${UNIT_MOUNTS%% *}"
  [ -n "$mnt" ] || { say "recording disk: no require_mount configured (recordings go to $CFG_REC)"; return 0; }
  if findmnt -rn -M "$mnt" >/dev/null 2>&1 && { [ -z "${CFG_MARKER:-}" ] || [ -e "$mnt/$CFG_MARKER" ]; }; then
    say "recording disk: mounted at $mnt${CFG_MARKER:+ (marker $CFG_MARKER present)}"
    return 0
  fi
  local msg="the recording disk is not mounted at $mnt${CFG_MARKER:+ (or $mnt/$CFG_MARKER is missing)}"
  if [ -n "$UNIT_MOUNTS" ]; then msg="$msg - $SERVICE has RequiresMountsFor=$UNIT_MOUNTS, so it would not start again"; fi
  if [ "$ALLOW_NO_DISK" = 1 ] && [ -z "$UNIT_MOUNTS" ]; then warn "$msg (continuing: --allow-missing-disk; recording waits for the disk)"; return 0; fi
  [ "$DRY" = 1 ] && { warn "$msg - a real run would stop here"; return 0; }
  die 5 "$msg. Mount it (sudo mount -a; findmnt $mnt) and run again. Nothing was changed."
}

# ===================================================================== verification of the running service
VERIFY_FAILS=0
expect() {   # expect NAME CODE(S) METHOD URL [curl args] - CODE(S) like 200 or 401|403
  local name="$1" want="$2" m="$3" u="$4"; shift 4
  local got; got="$(http "$m" "$u" "$@")"
  if [[ "|$want|" == *"|$got|"* ]]; then say "  ok   $name ($got)"; else say "  FAIL $name: got $got, expected $want"; VERIFY_FAILS=$((VERIFY_FAILS + 1)); fi
}
verify_service() {
  VERIFY_FAILS=0
  local forged="AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"   # not a secret: a made-up token
  say "checking the running service on 127.0.0.1:$CFG_PORT"
  expect "health (http)" 200 GET "$BASE/api/health"
  if [ "${CFG_PROTECT:-0}" = 1 ]; then expect "dashboard / (protected: to /tv)" 303 GET "$BASE/"
  else expect "dashboard /" 200 GET "$BASE/"; fi
  expect "/disk without login: no data" 401 GET "$BASE/api/disk/status"
  expect "/disk security without login" 401 GET "$BASE/api/disk/security"
  expect "/tv status without approval" 401 GET "$BASE/api/tv/status"
  expect "/tv live stream without approval" 401 GET "$BASE/tv/live/index.m3u8"
  expect "/tv with a forged session + page secret" 401 GET "$BASE/api/tv/status" -H "Cookie: f1_tv=$forged" -H "X-F1-TV-Page: $forged"
  expect "/tv stream with a forged page secret" 401 GET "$BASE/tv/live/index.m3u8?p=$forged" -H "Cookie: f1_tv=$forged"
  expect "approving /tv from /disk (refused)" "401|403" POST "$BASE/api/disk/security/decide" -H 'Content-Type: application/json' --data '{}'
  local code; code="$(http GET "$BASE/tv" -H "Cookie: f1_tv=$forged")"
  if [ "$code" = 200 ] && grep -q 'ACCESS REQUEST' "$BODY" && grep -q 'class="locked"' "$BODY" && ! grep -q '/?layout' "$BODY"; then
    say "  ok   /tv starts with a fresh approval request, even with a (stale) session cookie ($code)"
  else say "  FAIL /tv did not start with the approval request ($code)"; VERIFY_FAILS=$((VERIFY_FAILS + 1)); fi
  code="$(http POST "$BASE/api/tv/auth/status" -H "Cookie: f1_tvreq=$forged" -H "X-F1-TV-Challenge: $forged")"
  if [ "$code" = 200 ] && grep -Eq '"status": ?"none"' "$BODY"; then say "  ok   a made-up / reused challenge gets nothing ($code)"
  else say "  FAIL /tv challenge check ($code)"; VERIFY_FAILS=$((VERIFY_FAILS + 1)); fi
  if [ "${CFG_HTTPS:-0}" -gt 0 ] 2>/dev/null; then
    if [ -f "$CFG_CERT" ]; then expect "health (https :$CFG_HTTPS)" 200 GET "https://127.0.0.1:$CFG_HTTPS/api/health"
      expect "/tv stream over https without approval" 401 GET "https://127.0.0.1:$CFG_HTTPS/tv/live/index.m3u8"
    else warn "https_port $CFG_HTTPS is set but there is no certificate ($CFG_CERT) - https is off"; fi
  fi
  if [ "${CFG_PUB:-0}" = 1 ]; then verify_public; fi
  return "$VERIFY_FAILS"
}
verify_public() {   # the public gateway (Tailscale Funnel): only /tv and /remote, nothing private
  local P="http://127.0.0.1:${CFG_PUB_PORT:-8090}" HH="Host: ${CFG_PUB_HOST:-}" code
  say "checking the public gateway on 127.0.0.1:${CFG_PUB_PORT:-8090} (as https://${CFG_PUB_HOST:-?} through Funnel)"
  code="$(http GET "$P/tv" -H "$HH")"
  if [ "$code" = 200 ] && grep -q 'ACCESS REQUEST' "$BODY" && ! grep -q '/?layout' "$BODY"; then say "  ok   public /tv: approval screen only ($code)"
  else say "  FAIL public /tv ($code) - is the gateway running? journalctl -u $SERVICE | grep -i 'public gateway'"; VERIFY_FAILS=$((VERIFY_FAILS + 1)); fi
  expect "public /remote" 200 GET "$P/remote" -H "$HH"
  local p
  for p in /disk /disk-static/disk.js /api/disk/status /api/disk/auth/state /api/disk/security /api/health /api/state \
           /api/remote/info /api/voyo/recordings /f1tv/login /static/remote.html; do
    expect "public $p is closed" 404 GET "$P$p" -H "$HH"
  done
  expect "public POST /disk login is closed" 404 POST "$P/api/disk/auth/login" -H "$HH" -H 'Content-Type: application/json' --data '{}'
  expect "public /tv data without approval" 401 GET "$P/api/tv/status" -H "$HH"
  expect "public dashboard without approval (to /tv)" 303 GET "$P/" -H "$HH"
  expect "public ../ trick" 400 GET "$P/tv/../disk" --path-as-is -H "$HH"
  expect "public %-encoded trick" 400 GET "$P/%64isk" -H "$HH"
  expect "public remote token in a URL is refused" 400 GET "$P/remote?token=x" -H "$HH"
  expect "public gateway refuses other hostnames" 421 GET "$P/tv" -H "Host: 192.168.10.140"
}
wait_healthy() {
  local t=0
  while [ "$t" -lt "$HEALTH_TIMEOUT" ]; do
    if systemctl is-active --quiet "$SERVICE" && [ "$(http GET "$BASE/api/health")" = 200 ]; then return 0; fi
    sleep 3; t=$((t + 3))
  done
  return 1
}

if [ "$VERIFY_ONLY" = 1 ]; then
  step "Verifying the running service"
  if [ "$MODE" = production ] && ! systemctl is-active --quiet "$SERVICE"; then die 7 "$SERVICE is not running"; fi
  if verify_service; then say "all checks passed"; exit 0; else die 7 "$VERIFY_FAILS check(s) failed"; fi
fi

# ===================================================================== 3. the checkout
step "Checking the checkout"
G rev-parse --is-inside-work-tree >/dev/null 2>&1 || die 4 "git cannot read $REPO as $REPO_OWNER"
CUR_BRANCH="$(G symbolic-ref --short -q HEAD || true)"
OLD="$(G rev-parse HEAD)"
if [ "$ROLLBACK" = 0 ]; then
  [ -n "$CUR_BRANCH" ] || die 4 "the checkout is not on a branch (detached at ${OLD:0:12}, e.g. after a manual rollback). Go back: sudo -H -u $REPO_OWNER git -C $REPO checkout $BRANCH"
  [ "$CUR_BRANCH" = "$BRANCH" ] || die 4 "the checkout is on '$CUR_BRANCH', not '$BRANCH' (use --branch $CUR_BRANCH, or check out $BRANCH)"
fi
DIRTY="$(G status --porcelain --untracked-files=no)"
if [ -n "$DIRTY" ]; then
  printf '%s\n' "$DIRTY" | sed 's/^/    /' >&2
  die 4 "local changes in tracked files (above) - commit, stash or discard them yourself; nothing was changed"
fi
UNTRACKED="$(G status --porcelain | sed -n 's/^?? //p')"
[ -n "$UNTRACKED" ] && warn "untracked files (kept as they are): $(printf '%s' "$UNTRACKED" | tr '\n' ' ')"
say "on $CUR_BRANCH at ${OLD:0:12}, no local changes"

PENDING_FILE="$BACKUP_ROOT/restart-pending"
LAST_FILE="$BACKUP_ROOT/last-deploy"

if [ "$ROLLBACK" = 1 ]; then
  [ "$MODE" = production ] || die 3 "--rollback is for the production server"
  if [ -z "$ROLLBACK_TO" ]; then
    [ -r "$LAST_FILE" ] || die 3 "no $LAST_FILE - name the commit: --rollback <commit>"
    ROLLBACK_TO="$(sed -n 's/^previous=//p' "$LAST_FILE" | head -n1)"
  fi
  NEW="$(G rev-parse --verify -q "${ROLLBACK_TO}^{commit}")" || die 4 "unknown commit $ROLLBACK_TO"
  say "rollback: ${OLD:0:12} -> ${NEW:0:12}"
  UPDATE=1
else
  # ================================================================= 4. what is new
  step "Looking for updates on origin/$BRANCH"
  if [ "$DRY" = 1 ]; then
    NEW="$(G ls-remote --exit-code origin "refs/heads/$BRANCH" 2>/dev/null | cut -f1)" || die 4 "cannot reach origin (git ls-remote) - network / GitHub access (server/SETUP.md step 5)"
  else
    G fetch --quiet origin "+refs/heads/$BRANCH:refs/remotes/origin/$BRANCH" || die 4 "git fetch failed - network / GitHub access (server/SETUP.md step 5)"
    NEW="$(G rev-parse "refs/remotes/origin/$BRANCH")"
  fi
  UPDATE=0
  if [ "$NEW" = "$OLD" ]; then
    say "already up to date (${OLD:0:12})"
  elif G cat-file -e "$NEW^{commit}" 2>/dev/null && G merge-base --is-ancestor "$OLD" "$NEW"; then
    UPDATE=1
  elif G cat-file -e "$NEW^{commit}" 2>/dev/null && G merge-base --is-ancestor "$NEW" "$OLD"; then
    die 4 "the checkout has commits that are not on origin/$BRANCH - push or remove them yourself"
  elif G cat-file -e "$NEW^{commit}" 2>/dev/null; then
    die 4 "the checkout and origin/$BRANCH have diverged - resolve it by hand"
  else
    UPDATE=1      # dry run: the new commit is not downloaded yet
  fi
fi

CHANGED=""
NEEDS_RESTART=0
if [ "$UPDATE" = 1 ]; then
  if G cat-file -e "$NEW^{commit}" 2>/dev/null; then
    say "${OLD:0:12} -> ${NEW:0:12}:"
    G log --oneline --no-decorate "$OLD..$NEW" 2>/dev/null | head -n 30 | sed 's/^/    /' || true
    [ "$ROLLBACK" = 1 ] && G log --oneline --no-decorate "$NEW..$OLD" 2>/dev/null | head -n 30 | sed 's/^/    undo /' || true
    CHANGED="$(G diff --name-only "$OLD" "$NEW")"
    # documentation-only changes need no restart
    if printf '%s\n' "$CHANGED" | grep -Ev '(\.md$|^pc variant/|^docs/)' | grep -q .; then NEEDS_RESTART=1; fi
  else
    say "${OLD:0:12} -> ${NEW:0:12} (not downloaded in a dry run - the list of changes comes with a real run)"
    NEEDS_RESTART=1
  fi
fi
if [ "$MODE" = production ] && [ -r "$PENDING_FILE" ] && [ "$(cat "$PENDING_FILE")" = "$OLD" ]; then
  say "a restart for ${OLD:0:12} is still pending from an earlier run"; NEEDS_RESTART=1
fi
[ "$FORCE_RESTART" = 1 ] && NEEDS_RESTART=1

# ===================================================================== 5. safety: recording, disk, unit
if [ "$MODE" = production ]; then
  step "Safety checks"
  PRE_ACTIVE=0; systemctl is-active --quiet "$SERVICE" && PRE_ACTIVE=1
  PLAYER_ACTIVE=0; systemctl is-active --quiet "$PLAYER_SERVICE" 2>/dev/null && PLAYER_ACTIVE=1
  PRE_HEALTHY=0
  if [ "$PRE_ACTIVE" = 1 ] && [ "$(http GET "$BASE/api/health")" = 200 ]; then PRE_HEALTHY=1; fi
  say "$SERVICE: $([ "$PRE_ACTIVE" = 1 ] && echo running || echo NOT running)$([ "$PRE_HEALTHY" = 1 ] && echo ', healthy')" \
      "· $PLAYER_SERVICE: $([ "$PLAYER_ACTIVE" = 1 ] && echo running || echo not running)"
  if [ "$UPDATE" = 1 ] || [ "$NEEDS_RESTART" = 1 ]; then
    check_disk
    wait_until_idle "updating and restarting $SERVICE (and $PLAYER_SERVICE)"
    say "no recording running"
  fi
  # the installed unit vs the repository's (reported, never overwritten)
  REPO_UNIT="$REPO/server/systemd/$SERVICE"
  if [ -f "$REPO_UNIT" ]; then
    norm() { sed -e 's/[[:space:]]*#.*$//' -e '/^[[:space:]]*$/d' -e '/^RequiresMountsFor=/d' | sort; }
    if ! diff <(norm <"$REPO_UNIT") <(printf '%s\n' "$UNIT_TEXT" | norm) >"$TMPD/unit.diff" 2>&1; then
      warn "the installed $SERVICE differs from server/systemd/$SERVICE (kept as it is - review: systemctl cat $SERVICE):"
      sed -n 's/^</    repo only:      /p; s/^>/    installed only: /p' "$TMPD/unit.diff" >&2
    else
      say "systemd unit matches the repository"
    fi
  fi
fi

# ===================================================================== 6. HTTPS certificate (checked; created / renewed only when needed)
CERT_ACTION="none"
if [ "$MODE" = production ] && [ "${CFG_HTTPS:-0}" -gt 0 ] 2>/dev/null; then
  step "HTTPS certificate"
  OWN_CERT="$CFG_DATA/tls/cert.pem"
  if ! command -v openssl >/dev/null 2>&1; then warn "openssl missing - certificate not checked"
  elif [ ! -s "$CFG_CERT" ] || [ ! -s "$CFG_KEY" ]; then
    if [ "$CFG_CERT" = "$OWN_CERT" ]; then CERT_ACTION="create"; say "no certificate yet - will create one (make-https-cert.sh)"
    else warn "the configured certificate $CFG_CERT is missing - not created automatically (custom path)"; fi
  else
    CERT_WHY=""
    openssl x509 -in "$CFG_CERT" -noout -checkend $((30 * 86400)) >/dev/null 2>&1 || CERT_WHY="it expires within 30 days"
    SAN="$(openssl x509 -in "$CFG_CERT" -noout -ext subjectAltName 2>/dev/null | tr ',' '\n' | sed -n 's/^[[:space:]]*IP Address://p')"
    for ip in $(hostname -I 2>/dev/null); do
      case "$ip" in *:*|127.*) continue ;; esac
      printf '%s\n' "$SAN" | grep -qx "$ip" || CERT_WHY="${CERT_WHY:+$CERT_WHY, }it is not valid for $ip"
      break      # the main address
    done
    SUBJ="$(openssl x509 -in "$CFG_CERT" -noout -subject 2>/dev/null | sed 's/^subject= *//')"
    ISS="$(openssl x509 -in "$CFG_CERT" -noout -issuer 2>/dev/null | sed 's/^issuer= *//')"
    if [ -z "$CERT_WHY" ]; then
      say "certificate ok: $(openssl x509 -in "$CFG_CERT" -noout -enddate 2>/dev/null | sed 's/notAfter=/valid until /'), SAN IPs: $(printf '%s' "$SAN" | tr '\n' ' ')"
    elif [ "$CFG_CERT" != "$OWN_CERT" ] || [ "$SUBJ" != "$ISS" ]; then
      warn "certificate problem ($CERT_WHY) - it is not this server's self-made certificate, so it is NOT replaced"
    else
      CERT_ACTION="renew"; say "certificate will be renewed: $CERT_WHY (browsers then show the warning once more)"
    fi
    KSTAT="$(stat -c '%U %a' "$CFG_KEY")"
    [ "${KSTAT##* }" = 600 ] || warn "$CFG_KEY has mode ${KSTAT##* } (600 expected)"
  fi
  [ "$CERT_ACTION" != none ] && NEEDS_RESTART=1
fi

# ===================================================================== dry run ends here
if [ "$DRY" = 1 ]; then
  step "Plan"
  if [ "$UPDATE" = 1 ]; then
    if [ "$MODE" = production ]; then plan "back up $CFG_DATA/{auth,tls,state files}, .env and the unit to $BACKUP_ROOT/<time>-${OLD:0:8}/"; fi
    plan "fast-forward $BRANCH ${OLD:0:12} -> ${NEW:0:12}"
    [ "$SKIP_DEPS" = 0 ] && plan "pip install -r main/requirements.txt into $VENV"
    [ "$SKIP_TESTS" = 0 ] && plan "run the tests ($TESTS) in a temporary data directory; on failure go back to ${OLD:0:12}"
  else
    say "[dry-run] no code update"
  fi
  [ "$CERT_ACTION" != none ] && plan "$CERT_ACTION the HTTPS certificate with server/make-https-cert.sh (after a backup)"
  if [ "$MODE" = production ]; then
    if [ "$NEEDS_RESTART" = 1 ] && [ "$NO_RESTART" = 0 ]; then plan "restart $SERVICE (then $PLAYER_SERVICE if it runs), then verify http/https and the /tv + /disk protections"
    else say "[dry-run] no restart needed"; fi
  fi
  say "[dry-run] done - nothing was changed"
  exit 0
fi

if [ "$UPDATE" = 0 ] && [ "$NEEDS_RESTART" = 0 ]; then
  if [ "$MODE" = production ] && [ "${PRE_HEALTHY:-0}" = 1 ]; then
    step "Nothing to update - checking the running service"
    verify_service || warn "$VERIFY_FAILS check(s) failed on the running service (nothing was changed) - see above"
  fi
  say "nothing to do"
  exit 0
fi

# ===================================================================== 7. backup
BK=""
if [ "$MODE" = production ]; then
  step "Backup"
  BK="$BACKUP_ROOT/$(date +%Y%m%d-%H%M%S)-${OLD:0:8}"
  ITEMS=()
  for i in auth tls; do [ -e "$CFG_DATA/$i" ] && ITEMS+=("$i"); done
  while IFS= read -r f; do ITEMS+=("$f"); done < <(cd "$CFG_DATA" 2>/dev/null && find . -maxdepth 1 -type f \( -name '*.json' -o -name '*.jsonl' -o -name '*.toml' \) -printf '%f\n' | sort)
  USED_KB=0
  if [ "${#ITEMS[@]}" -gt 0 ]; then USED_KB="$( (cd "$CFG_DATA" && du -sk -- "${ITEMS[@]}" 2>/dev/null) | awk '{s+=$1} END {print s+0}')"; fi
  NEED_KB=$(( USED_KB * 2 + 20480 ))
  AVAIL_KB="$(df -Pk "$BACKUP_ROOT" | awk 'NR==2 {print $4}')"
  [ "${AVAIL_KB:-0}" -gt "$NEED_KB" ] || die 8 "not enough space for the backup in $BACKUP_ROOT (${AVAIL_KB} KiB free, ~${NEED_KB} KiB needed) - nothing was changed"
  mkdir -m 700 "$BK" || die 8 "cannot create $BK"
  if [ "${#ITEMS[@]}" -gt 0 ]; then
    tar -czf "$BK/runtime.tgz" -C "$CFG_DATA" -- "${ITEMS[@]}" || die 8 "backup of $CFG_DATA failed - nothing was changed"
    tar -tzf "$BK/runtime.tgz" >/dev/null || die 8 "the backup archive cannot be read - nothing was changed"
  fi
  [ -e "$ENV_FILE" ] && { cp -p "$ENV_FILE" "$BK/env" || die 8 "backup of .env failed"; }
  printf '%s\n' "$UNIT_TEXT" >"$BK/systemd-unit.txt"
  printf 'date=%s\nbranch=%s\nprevious=%s\nnew=%s\ndata_dir=%s\nitems=%s\n' "$(date -Is)" "$BRANCH" "$OLD" "$NEW" "$CFG_DATA" "${ITEMS[*]:-}" >"$BK/info"
  chmod -R go-rwx "$BK"
  say "backup: $BK (runtime.tgz: ${ITEMS[*]:-nothing}; .env; unit) - recordings, caches and the browser profile are not copied"
  # keep the newest $KEEP backups
  mapfile -t OLD_BK < <(find "$BACKUP_ROOT" -mindepth 1 -maxdepth 1 -type d -regextype posix-extended -regex '.*/[0-9]{8}-[0-9]{6}-[0-9a-f]{8}' | sort | head -n -"$KEEP")
  for d in "${OLD_BK[@]}"; do rm -rf -- "$d"; done
fi


record_deploy() {   # only after a successful deploy: what --rollback goes back to
  [ "$UPDATE" = 1 ] || return 0
  printf 'previous=%s\nnew=%s\ndate=%s\nbackup=%s\n' "$OLD" "$NEW" "$(date -Is)" "$BK" >"$LAST_FILE"
  chmod 600 "$LAST_FILE"
}
code_back() {   # put the previous commit back (only ever OLD, which was deployed and clean)
  say "putting ${OLD:0:12} back"
  G reset --keep "$OLD" >/dev/null || warn "git reset to ${OLD:0:12} failed - check: sudo -H -u $REPO_OWNER git -C $REPO status"
  if [ "$SKIP_DEPS" = 0 ]; then run_as "$VENV_OWNER" "$PY" -m pip install -q --disable-pip-version-check -r "$REPO/main/requirements.txt" >/dev/null 2>&1 || warn "pip install of the previous requirements failed"; fi

}

# ===================================================================== 8. code + dependencies + tests
if [ "$UPDATE" = 1 ]; then
  step "Updating the code"
  if [ "$ROLLBACK" = 1 ]; then G reset --keep "$NEW" >/dev/null || die 4 "git reset to ${NEW:0:12} failed"
  else G merge --ff-only --quiet "$NEW" || die 4 "fast-forward to ${NEW:0:12} failed - nothing was changed"; fi
  say "now at $(G log -1 --format='%h %s')"

  if [ "$SKIP_DEPS" = 0 ]; then
    step "Python requirements"
    if ! run_as "$VENV_OWNER" "$PY" -m pip install -q --disable-pip-version-check -r "$REPO/main/requirements.txt" >"$TMPD/pip.log" 2>&1; then
      tail -n 20 "$TMPD/pip.log" | sed 's/^/    /' >&2
      code_back
      die 6 "pip install failed (above) - the code is back at ${OLD:0:12}; $SERVICE was not touched"
    fi
    say "requirements installed in $VENV"
  fi

  if [ "$SKIP_TESTS" = 0 ]; then
    step "Tests (isolated: a temporary data directory, never $CFG_DATA)"
    TDATA="$(run_as "$SVC_USER" mktemp -d)"
    read -r -a TEST_LIST <<<"$TESTS"
    if run_as "$SVC_USER" env -i PATH=/usr/local/bin:/usr/bin:/bin HOME="$TDATA" TMPDIR="$TDATA" LANG=C.UTF-8 \
         F1DASH_DATA_DIR="$TDATA/data" F1DASH_CONFIG_OVERLAY="" \
         bash -c 'cd "$1/main" && shift && exec timeout 1800 "$@"' _ "$REPO" "$PY" -m unittest "${TEST_LIST[@]}" >"$TMPD/tests.log" 2>&1
    then TRC=0; else TRC=$?; fi
    run_as "$SVC_USER" rm -rf -- "$TDATA" || true
    grep -E '^(Ran |OK|FAILED)' "$TMPD/tests.log" | sed 's/^/    /' || true
    if [ "$TRC" != 0 ]; then
      grep -E '^(FAIL|ERROR):' "$TMPD/tests.log" | head -n 20 | sed 's/^/    /' >&2 || true
      [ -n "$BK" ] && cp "$TMPD/tests.log" "$BK/tests.log" 2>/dev/null || true
      code_back
      die 6 "tests failed - the code is back at ${OLD:0:12}; $SERVICE was not touched${BK:+ (log: $BK/tests.log)}"
    fi
    say "tests passed"
  fi
  load_config_values      # the new version's view of the configuration
fi

if [ "$MODE" != production ]; then
  say "development checkout updated - start it as you usually do"
  exit 0
fi

# ===================================================================== 9. certificate (after the backup)
if [ "$CERT_ACTION" != none ]; then
  step "HTTPS certificate: $CERT_ACTION"
  if ! "$HERE/make-https-cert.sh" >"$TMPD/cert.log" 2>&1; then
    sed 's/^/    /' "$TMPD/cert.log" >&2
    warn "make-https-cert.sh failed - the previous certificate (if any) is in the backup; continuing with http"
  else
    sed 's/^/    /' "$TMPD/cert.log"
  fi
fi

# ===================================================================== 10. restart + verification
if [ "$NEEDS_RESTART" = 0 ]; then
  say "only documentation changed - no restart needed"
  record_deploy; rm -f "$PENDING_FILE"; exit 0
fi
if [ "$NO_RESTART" = 1 ]; then
  printf '%s\n' "$(G rev-parse HEAD)" >"$PENDING_FILE"; record_deploy
  say "--no-restart: the new code runs after the next restart (run this script again, or: sudo systemctl restart $SERVICE)"
  exit 0
fi
if [ -n "$(recording_reason)" ]; then         # a session may have started meanwhile
  printf '%s\n' "$(G rev-parse HEAD)" >"$PENDING_FILE"; record_deploy
  wait_until_idle "restarting $SERVICE" "The code is updated and tested but NOT running yet: the next run of this script restarts it."
fi

step "Restarting $SERVICE"
systemctl restart "$SERVICE" || true
if wait_healthy && verify_service; then
  rm -f "$PENDING_FILE"; record_deploy
  if [ "${PLAYER_ACTIVE:-0}" = 1 ]; then
    # the player unit only Wants= the dashboard (a dashboard restart no longer stops a recording): it is
    # restarted here, explicitly, so it runs the new code too - only now, while nothing is recorded
    systemctl restart "$PLAYER_SERVICE" || true
    sleep 3
    if systemctl is-active --quiet "$PLAYER_SERVICE"; then say "$PLAYER_SERVICE restarted - runs the new code"
    else warn "$PLAYER_SERVICE is not running - sudo systemctl start $PLAYER_SERVICE"; fi
  fi
  step "Done"
  say "$SERVICE runs $(G log -1 --format='%h %s')${BK:+ · backup $BK}"
  say "rollback if needed: sudo $HERE/update-f1dash.sh --rollback"
  [ "$CERT_ACTION" != none ] && say "new certificate: each browser (PC, phone, TV) shows the certificate warning once more - accept it for this server's IP"
  exit 0
fi

journalctl -u "$SERVICE" -n 30 --no-pager 2>/dev/null | sed 's/^/    /' >&2 || true
if [ "$UPDATE" = 1 ] && [ "${PRE_HEALTHY:-0}" = 1 ]; then
  warn "the new version is not healthy - rolling back to ${OLD:0:12} (the runtime data was not changed by this script)"
  code_back
  systemctl restart "$SERVICE" || true
  if wait_healthy && verify_service; then
    die 7 "update failed its checks and was rolled back: $SERVICE runs ${OLD:0:12} again. Backup: ${BK:-none}"
  fi
  die 7 "rollback did not make $SERVICE healthy either - look at: journalctl -u $SERVICE -n 100. Runtime backup: ${BK:-none} (restore: sudo systemctl stop $SERVICE && sudo tar -xzf $BK/runtime.tgz -C $CFG_DATA && sudo chown -R $SVC_USER: $CFG_DATA/auth && sudo systemctl start $SERVICE)"
fi
die 7 "$SERVICE is not healthy after the restart and was not rolled back automatically (it was not healthy before, or nothing in the code changed). Look at: journalctl -u $SERVICE -n 100. Backup: ${BK:-none}"
