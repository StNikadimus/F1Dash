#!/bin/sh
# ---------------------------------------------------------------------------
# WD TV Live (WDLXTV) -> F1 dashboard IR bridge          *** EXPERIMENTAL ***
#
# Only usable if server/wdtv/probe_ir.sh found a readable key source on your box.
# It reads key presses and forwards them over the LAN:
#     GET http://SERVER/api/remote/key?key=KEY_UP
# It never writes to the IR device and never injects keys into the WD TV.
#
#   evdev source (Linux input events, non-destructive):
#     sh wdtv_ir_bridge.sh --server http://192.168.1.10:8080 --device /dev/input/event0
#   raw source (e.g. /dev/ir) - first learn the codes, then run with the map:
#     sh wdtv_ir_bridge.sh --device /dev/ir --format raw --record 4 --learn
#     sh wdtv_ir_bridge.sh --device /dev/ir --format raw --record 4 --map /conf/f1keys.map \
#                          --server http://192.168.1.10:8080
#
# Map file format for raw mode (one per line):   <hexcode> <KEY_NAME>
#     e.g.   847905ff KEY_UP
# ---------------------------------------------------------------------------

SERVER="http://192.168.1.10:8080"
DEVICE="/dev/input/event0"
FORMAT="evdev"
RECORD=4
MAP=""
TOKEN=""
LEARN=0

while [ $# -gt 0 ]; do
  case "$1" in
    --server) SERVER="$2"; shift ;;
    --device) DEVICE="$2"; shift ;;
    --format) FORMAT="$2"; shift ;;
    --record) RECORD="$2"; shift ;;
    --map) MAP="$2"; shift ;;
    --token) TOKEN="$2"; shift ;;
    --learn) LEARN=1 ;;
    -h|--help) sed -n '2,22p' "$0"; exit 0 ;;
    *) echo "unknown option $1"; exit 1 ;;
  esac
  shift
done

[ -e "$DEVICE" ] || { echo "device $DEVICE not found - run probe_ir.sh first"; exit 1; }

send() {
  name="$1"
  if [ "$LEARN" = 1 ]; then echo "key: $name"; return; fi
  url="$SERVER/api/remote/key?key=$name"
  [ -n "$TOKEN" ] && url="$url&token=$TOKEN"
  wget -q -O /dev/null "$url" >/dev/null 2>&1 &
  echo "$(date +%H:%M:%S) $name"
}

# Linux input key codes -> names (subset relevant to a media remote)
keyname() {
  case "$1" in
    1) echo KEY_ESC ;; 2) echo KEY_1 ;; 3) echo KEY_2 ;; 4) echo KEY_3 ;; 5) echo KEY_4 ;;
    6) echo KEY_5 ;; 7) echo KEY_6 ;; 8) echo KEY_7 ;; 9) echo KEY_8 ;; 10) echo KEY_9 ;;
    11) echo KEY_0 ;; 14) echo KEY_BACKSPACE ;; 23) echo KEY_I ;; 28) echo KEY_ENTER ;;
    57) echo KEY_SPACE ;; 102) echo KEY_HOME ;; 103) echo KEY_UP ;; 104) echo KEY_PAGEUP ;;
    105) echo KEY_LEFT ;; 106) echo KEY_RIGHT ;; 108) echo KEY_DOWN ;; 109) echo KEY_PAGEDOWN ;;
    119) echo KEY_PAUSE ;; 138) echo KEY_HELP ;; 139) echo KEY_MENU ;; 158) echo KEY_BACK ;;
    163) echo KEY_NEXTSONG ;; 164) echo KEY_PLAYPAUSE ;; 165) echo KEY_PREVIOUSSONG ;;
    166) echo KEY_STOPCD ;; 174) echo KEY_EXIT ;; 207) echo KEY_PLAY ;; 352) echo KEY_OK ;;
    358) echo KEY_INFO ;; 407) echo KEY_NEXT ;; 412) echo KEY_PREVIOUS ;;
    398) echo KEY_RED ;; 399) echo KEY_GREEN ;; 400) echo KEY_YELLOW ;; 401) echo KEY_BLUE ;;
    402) echo KEY_CHANNELUP ;; 403) echo KEY_CHANNELDOWN ;;
    *) echo "" ;;
  esac
}

exec 3< "$DEVICE" || exit 1
echo "bridge: $DEVICE ($FORMAT) -> $SERVER  (Ctrl+C to stop)"

if [ "$FORMAT" = "evdev" ]; then
  # struct input_event on 32-bit little-endian MIPS: 8 bytes time, u16 type, u16 code, s32 value
  while :; do
    set -- $(dd bs=16 count=1 <&3 2>/dev/null | hexdump -v -e '16/1 "%02x "')
    [ $# -eq 16 ] || continue
    type=$((0x${10}${9})); code=$((0x${12}${11})); value=$((0x${16}${15}${14}${13}))
    [ "$type" -eq 1 ] || continue            # EV_KEY
    [ "$value" -eq 1 ] || continue           # key press (ignore release / auto-repeat)
    name=$(keyname "$code")
    if [ -z "$name" ]; then echo "unmapped key code $code"; continue; fi
    send "$name"
  done
else
  while :; do
    hex=$(dd bs="$RECORD" count=1 <&3 2>/dev/null | hexdump -v -e '/1 "%02x"')
    [ -n "$hex" ] || continue
    if [ "$LEARN" = 1 ]; then echo "raw record: $hex"; continue; fi
    name=""
    [ -n "$MAP" ] && name=$(grep -i "^$hex " "$MAP" 2>/dev/null | head -n 1 | cut -d' ' -f2)
    if [ -z "$name" ]; then echo "unmapped raw code $hex"; continue; fi
    send "$name"
  done
fi
