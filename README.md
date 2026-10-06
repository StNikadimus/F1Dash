# F1 TV dashboard

Self-hosted F1 live timing dashboard for a TV screen, synchronised with the official VOYO player.

| Folder | What it is |
|---|---|
| **`main/`** | the shared code: Python backend (`main/server/`), dashboard + phone remote (`main/dashboard/`), tools (VOYO window launcher, VOYO playback-clock bridge), tests, shared config `main/config/config.toml`. **The full manual is `main/README.md`.** |
| **`server/`** | **Linux server deployment (primary)**: `launch.sh`, `config/server.toml` overlay, systemd units, Docker, IR remote bridges, server VOYO player → `server/README.md`; **new server from scratch: `server/SETUP.md`** |
| **`pc variant/`** | **Windows / local-PC deployment**: `launch.bat` → `pc variant/README.md` |
| `launch.bat` | shortcut to `pc variant\launch.bat` |
| `data/` | runtime data (git-ignored): F1 TV sign-in, VOYO browser profile, sync state, F1-feed recordings, caches |

Typical setups:

* **Linux server + PC/TV:** the server runs the backend 24/7 (`server/launch.sh` or systemd); the PC that
  shows the TV picture runs `launch.bat server http://<server-ip>:8080` (dashboard window, VOYO window and
  the VOYO playback-clock bridge for AUTO SYNC).
* **Everything on one Windows PC:** `launch.bat`.
