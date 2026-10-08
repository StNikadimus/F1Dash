#!/usr/bin/env bash
# A self-signed HTTPS certificate for this server, so https://<server-ip>/tv and /disk work (the browser
# shows a warning once - "Advanced -> Proceed"; after that the connection is encrypted).
#   sudo ./server/make-https-cert.sh                    # IP addresses found automatically
#   sudo ./server/make-https-cert.sh 192.168.1.60       # or name them
# Writes <data>/tls/cert.pem + key.pem (F1DASH_DATA_DIR from server/.env, else <repo>/data), owned by f1.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(dirname "$HERE")"
if [ -f "$HERE/.env" ]; then set -a; . "$HERE/.env"; set +a; fi
DATA="${F1DASH_DATA_DIR:-$REPO/data}"
IPS="${*:-$(hostname -I 2>/dev/null || true)}"
SAN="DNS:localhost,DNS:$(hostname),IP:127.0.0.1"
for ip in $IPS; do case "$ip" in *:*) ;; *) SAN="$SAN,IP:$ip";; esac; done
mkdir -p "$DATA/tls"
openssl req -x509 -newkey rsa:2048 -nodes -days 3650 -subj "/CN=f1-server" -addext "subjectAltName=$SAN" \
  -keyout "$DATA/tls/key.pem" -out "$DATA/tls/cert.pem" 2>/dev/null
chmod 600 "$DATA/tls/key.pem"
if id f1 >/dev/null 2>&1; then chown -R f1:f1 "$DATA/tls"; fi
echo "Certificate for: $SAN"
echo "Written: $DATA/tls/cert.pem, $DATA/tls/key.pem  - restart: sudo systemctl restart f1-dashboard"
