#!/bin/sh
# ---------------------------------------------------------------------------
# WD TV Live (WDLXTV firmware) - IR remote capability probe.
#
# Run ON THE WD TV (telnet/ssh as root):   sh /tmp/probe_ir.sh
# Copy it there first, e.g. from a USB stick:  cp /tmp/media/usb/USB1/*/probe_ir.sh /tmp/
#
# It only READS information. It does not change any configuration and does
# not keep any device open after it finishes. The optional listening tests
# (steps 6/7) open the device for 10 seconds; on some kernels a second reader
# of /dev/ir can take button presses away from the WD TV menu during those
# 10 seconds - nothing is changed permanently.
# ---------------------------------------------------------------------------

say() { echo "== $*"; }
LISTEN_SECONDS=10

say "1. System"
uname -a 2>/dev/null
cat /proc/version 2>/dev/null
[ -f /etc/wdlxtv-version ] && cat /etc/wdlxtv-version
echo

say "2. Linux input subsystem (/proc/bus/input/devices)"
if [ -r /proc/bus/input/devices ]; then
  cat /proc/bus/input/devices
else
  echo "   NOT PRESENT - kernel has no input-subsystem devices exposed"
fi
echo
ls -l /dev/input/ 2>/dev/null || echo "   /dev/input does not exist"
echo

say "3. IR / LIRC related device nodes"
ls -l /dev/ir* /dev/lirc* /dev/rc* 2>/dev/null || echo "   none found"
grep -i -E "ir|lirc|input|rc" /proc/devices 2>/dev/null
echo

say "4. Loaded kernel modules containing ir/lirc/input"
lsmod 2>/dev/null | grep -i -E "ir|lirc|input|evdev" || echo "   none / lsmod unavailable"
echo

say "5. WDLXTV helpers"
[ -e /tmp/ir_injection ] && echo "   /tmp/ir_injection present (WDLXTV key INJECTION - write-only, sends keys TO the WD TV)" \
                          || echo "   /tmp/ir_injection not present"
for t in hexdump od wget nc curl timeout; do
  if command -v $t >/dev/null 2>&1; then echo "   $t: yes"; else echo "   $t: no"; fi
done
ps 2>/dev/null | grep -i -E "dmaosd|lirc|irrecv" | grep -v grep
echo

listen() {
  dev="$1"; out="/tmp/probe_$(basename "$dev").txt"
  echo "   Listening on $dev for ${LISTEN_SECONDS}s - PRESS SOME REMOTE BUTTONS NOW (e.g. UP, DOWN, OK)..."
  raw="$out.bin"
  cat "$dev" > "$raw" 2>/dev/null &          # cat writes unbuffered, nothing is lost on kill
  pid=$!
  sleep $LISTEN_SECONDS
  kill $pid 2>/dev/null
  sleep 1
  bytes=$(wc -c < "$raw" 2>/dev/null)
  hexdump -v -e '16/1 "%02x " "\n"' "$raw" > "$out" 2>/dev/null
  echo "   captured ${bytes:-0} byte(s) -> $out"
  head -n 12 "$out" 2>/dev/null
  [ "${bytes:-0}" -gt 0 ] && return 0 || return 1
}

say "6. Listening test on /dev/input/event* (non-destructive for evdev)"
FOUND_EV=""
for ev in /dev/input/event*; do
  [ -e "$ev" ] || continue
  if listen "$ev"; then FOUND_EV="$ev"; fi
done
[ -z "$FOUND_EV" ] && echo "   no evdev device produced data"
echo

say "7. Listening test on /dev/ir (proprietary Sigma Designs IR driver, if present)"
FOUND_IR=""
if [ -e /dev/ir ]; then
  if listen /dev/ir; then FOUND_IR="/dev/ir"; fi
  echo "   NOTE: if the WD TV menu stopped reacting while this test ran, /dev/ir"
  echo "   has a single reader queue and a bridge would steal presses from the WD TV."
else
  echo "   /dev/ir not present"
fi
echo

say "VERDICT"
if [ -n "$FOUND_EV" ]; then
  echo "   The remote generates Linux INPUT EVENTS on $FOUND_EV."
  echo "   -> use:  wdtv_ir_bridge.sh --device $FOUND_EV --format evdev"
elif [ -n "$FOUND_IR" ]; then
  echo "   The remote is readable as RAW records on /dev/ir (not Linux input events)."
  echo "   -> learn the codes:  wdtv_ir_bridge.sh --device /dev/ir --format raw --learn"
else
  echo "   No readable IR key source found. The WD TV firmware does not expose the"
  echo "   remote to user space on this box. Use a USB/GPIO IR receiver on the"
  echo "   dashboard server instead (see server/wdtv/README.md, option C)."
fi
