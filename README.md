# F1 TV dashboard

Self-hosted F1 live timing dashboard for a TV screen, synchronised with the official VOYO player.

| Folder | What it is |
|---|---|
| **`main/`** | the shared code: Python backend (`main/server/`), dashboard + phone remote (`main/dashboard/`), tools (VOYO window launcher, VOYO playback-clock bridge), tests, shared config `main/config/config.toml`. **The full manual is `main/README.md`.** |
| **`server/`** | **Linux server deployment (primary)**: `launch.sh`, `config/server.toml` overlay, systemd units, Docker, IR remote bridges, server VOYO player → `server/README.md`; **new server from scratch: `server/SETUP.md`**; the recorder page `/disk` (`server/disk/`), stream + dashboard `/tv` (`server/tv/`); `/disk` password, trusted phone and `/tv` approval → `server/README.md` "Security" |
| **`pc variant/`** | **Windows / local-PC deployment**: `launch.bat` → `pc variant/README.md` |
| `launch.bat` | shortcut to `pc variant\launch.bat` |
| `data/` | runtime data (git-ignored): F1 TV sign-in, VOYO browser profile, sync state, F1-feed recordings, caches |

Typical setups:

* **Linux server + PC/TV:** the server runs the backend 24/7 (`server/launch.sh` or systemd); the PC that
  shows the TV picture runs `launch.bat server http://<server-ip>:8080` (dashboard window, VOYO window and
  the VOYO playback-clock bridge for AUTO SYNC).
* **Everything on one Windows PC:** `launch.bat`.

## Server security in short (details: `server/README.md` → "Security")

* **`/disk`** (recorder page): a password, created once with a one-time setup code that only the server
  shows (`sudo cat /var/lib/f1-dashboard/auth/disk-setup-code`), stored as an Argon2id hash.
* **Trusted phone:** open `/remote` on your phone, then in `/disk` → SECURITY press **USE FOR AUTH** on the
  device with the same code. Only that phone can approve `/tv`.
* **`/tv` needs a fresh approval on every load:** each time `/tv` is opened or reloaded (also a new tab,
  a restarted browser or the URL in another browser) it shows a new code, and the trusted phone must
  approve exactly that request. Nothing is remembered: no cookie, device id, IP address or `/disk` login
  opens `/tv` by itself; the approval belongs to that one page load and is checked by the server for
  every `/tv` API call and video piece.
* Use **https** (`server/make-https-cert.sh`): the self-made certificate makes each browser warn once.

## Optional: `/tv` and `/remote` from anywhere (Tailscale Funnel)

A separate gateway on `127.0.0.1:8090` forwards **only** `/tv`, `/remote` and the exact files / APIs they need
(an allowlist; ambiguous paths, other hosts, `?token=` refused; `/disk`, health, admin and all other routes are
not reachable). From the internet `/tv` still needs the phone's approval on every load, `/remote` controls and
approves only on the trusted phone, and the dashboard is read-only. Off by default; setup, verification and
rollback: `server/README.md` → "Public access".

## Updating the server

```bash
sudo /opt/f1-dashboard/server/update-f1dash.sh --dry-run   # shows what it would do, changes nothing
sudo /opt/f1-dashboard/server/update-f1dash.sh             # backup, update, tests, safe restart, checks
sudo /opt/f1-dashboard/server/update-f1dash.sh --rollback  # back to the version before the last update
```

It refuses local changes in the checkout, never restarts during a recording (`--wait-idle MINUTES` waits),
backs up the security state, `.env` and the certificate to `/var/backups/f1-dashboard/` first, tests the new
version in a throw-away data directory and rolls back automatically when the tests or the post-restart
checks fail. Full description, exit codes and manual rollback: `server/README.md` → "Updating the server".
