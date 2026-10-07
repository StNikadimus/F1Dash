#!/usr/bin/env bash
# Installs what the server VOYO player needs (Debian / Ubuntu):
#   Google Chrome (has the Widevine DRM module VOYO's player needs - distro Chromium usually does not),
#   Xvfb (virtual screen), PulseAudio (sound of that screen), ffmpeg (recording), x11vnc (sign-in).
#   sudo ./server/setup-voyo-player.sh
set -euo pipefail
[ "$(id -u)" = 0 ] || { echo "run with sudo" >&2; exit 1; }
apt-get update
apt-get install -y xvfb x11vnc ffmpeg fonts-liberation wget gnupg ca-certificates
# Intel Quick Sync (capture_encoder = "vaapi"): VA-API drivers + vainfo (harmless without an Intel GPU)
apt-get install -y vainfo i965-va-driver || true
apt-get install -y intel-media-va-driver-non-free 2>/dev/null || apt-get install -y intel-media-va-driver || true
if id f1 >/dev/null 2>&1; then for g in render video; do getent group $g >/dev/null && usermod -aG $g f1; done; fi
if command -v pipewire-pulse >/dev/null 2>&1 && ! command -v pulseaudio >/dev/null 2>&1; then
  echo "NOTE: this system uses PipeWire. The player starts its own PulseAudio sound server for the"
  echo "      virtual screen; install it only on a headless server:  apt-get install pulseaudio"
  echo "      (or set [voyo.server_player] audio = false - video without sound)."
else
  apt-get install -y pulseaudio
fi
if ! command -v google-chrome >/dev/null 2>&1; then
  install -d -m 0755 /etc/apt/keyrings
  wget -qO- https://dl.google.com/linux/linux_signing_key.pub | gpg --dearmor -o /etc/apt/keyrings/google-chrome.gpg
  echo "deb [arch=amd64 signed-by=/etc/apt/keyrings/google-chrome.gpg] https://dl.google.com/linux/chrome/deb/ stable main" \
    > /etc/apt/sources.list.d/google-chrome.list
  apt-get update
  apt-get install -y google-chrome-stable
fi
echo
google-chrome --version
ls -d /opt/google/chrome/WidevineCdm >/dev/null 2>&1 && echo "Widevine: ok" || echo "Widevine: NOT FOUND"
if ls /dev/dri/renderD* >/dev/null 2>&1; then
  echo "Intel Quick Sync (H.264 encode):"
  vainfo 2>/dev/null | grep -i "H264.*EncSlice" || echo "  not found with the default driver - try: LIBVA_DRIVER_NAME=i965 vainfo"
fi
echo "Next: ./server/voyo-player.sh login   (sign in to VOYO once)"
