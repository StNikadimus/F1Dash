#!/usr/bin/env bash
# Installs what the server VOYO player needs (Debian / Ubuntu):
#   Google Chrome (has the Widevine DRM module VOYO's player needs - distro Chromium usually does not),
#   Xvfb (virtual screen), PulseAudio (sound of that screen), ffmpeg (recording), x11vnc (sign-in).
#   sudo ./server/setup-voyo-player.sh
set -euo pipefail
[ "$(id -u)" = 0 ] || { echo "run with sudo" >&2; exit 1; }
apt-get update
apt-get install -y xvfb x11vnc ffmpeg fonts-liberation wget gnupg ca-certificates
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
echo "Next: ./server/voyo-player.sh login   (sign in to VOYO once)"
