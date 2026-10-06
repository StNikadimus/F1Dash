# server/ — Linux deployment (primary)

This folder holds only Linux deployment files. All code is shared in `../main/`
(the backend, dashboard, phone remote, tools and tests). The server runs the
complete backend:

- F1 TV live timing, with the public F1 SignalR fallback
- LIVE / VOD / AUTO mode and session detection
- timing, race control, weather, telemetry, track and mini-map
- Lights Out resolver, Event Sync, VOYO sync and AUTO SYNC
- F1-feed recording and replay
- the phone remote backend

| Path | What |
|---|---|
| `launch.sh` | starts the server: creates the venv, installs dependencies, runs `main/main.py` with the server overlay |
| `config/server.toml` | overlay on `main/config/config.toml`; only the server-specific values (`0.0.0.0:8080`, no browser, remote VOYO clock allowed) |
| `.env.example` | copy to `.env`: token, start mode, remote token, time zone |
| `systemd/f1-dashboard.service` | systemd unit (installed under `/opt/f1-dashboard`) |
| `systemd/f1dash-ir-bridge.service` | optional IR remote → dashboard bridge (`bridge/`) |
| `docker/` | Dockerfile + docker compose (build context = repo root) |
| `bridge/`, `wdtv/` | IR bridge and WD TV Live client scripts |

## Requirements

- Linux with **Python 3.11 or newer** (it uses `tomllib`). `python3 --version`.
  To use another interpreter: `PYTHON=python3.12 ./server/launch.sh`.
- Python dependencies come from `main/requirements.txt`. `launch.sh` installs
  them into `<repo>/.venv` (or `$F1DASH_VENV`) on every start; the install is a
  no-op once they are present.
- **A clock synchronised by NTP** (`timedatectl` shows `System clock
  synchronized: yes`). LIVE DATA DELAY and the live sync compare F1 timestamps
  with this clock, so a clock that is seconds off shows as a wrong delay. The
  dashboard warns about this when the measured delay is negative.
- Outbound HTTPS to `livetiming.formula1.com`, `api.formula1.com`/F1 TV and
  `api.openf1.org`.

## Start

```bash
git clone https://github.com/StNikadimus/F1Dash.git /opt/f1-dashboard
cd /opt/f1-dashboard
cp server/.env.example server/.env      # optional, then edit it
./server/launch.sh                      # AUTO: LIVE during a session, else VOD
./server/launch.sh --test               # simulator (no F1 TV needed) - quick check
./server/launch.sh --f1-status          # F1 TV sign-in state (no secrets printed)
./server/launch.sh --diagnose 120       # which live topics actually arrive, then exit
```

The dashboard is at `http://<server-ip>:8080/` and the phone remote at
`http://<server-ip>:8080/remote`.

### Environment variables

All of these are optional. `server/.env` is read by `launch.sh`, the systemd
unit and docker compose.

| Variable | Default | Meaning |
|---|---|---|
| `F1DASH_DATA_DIR` | `<repo>/data` (systemd: `/var/lib/f1-dashboard`, docker: `/data`) | all runtime data |
| `F1DASH_LOG_DIR` | `$F1DASH_DATA_DIR/logs` | log file when started by hand |
| `F1DASH_VENV` | `<repo>/.venv` | Python virtual environment |
| `F1DASH_CONFIG_OVERLAY` | `server/config/server.toml` | overlay file(s), `:`-separated |
| `F1TV_TOKEN` | – | F1 TV subscription token (instead of the stored sign-in) |
| `F1DASH_REMOTE_TOKEN` | – | shared secret for the phone remote, the TV agent and the VOYO clock bridge. **Set it on a LAN server** |
| `F1DASH_SOURCE_MODE` | `auto` | `auto` / `live` / `vod` / `test` / `replay` |
| `F1DASH_<SECTION>_<KEY>` | – | overrides one config value, e.g. `F1DASH_SERVER_PORT=8090`, `F1DASH_SYNC_AUTO_SYNC=false` |

## F1 TV sign-in on a headless server

For security the sign-in page only answers on loopback (`127.0.0.1`), so it is
never exposed to the LAN. Use one of these three options:

1. **SSH tunnel (recommended).** From your PC run
   `ssh -L 8080:127.0.0.1:8080 user@server`, then open
   `http://127.0.0.1:8080/f1tv/login` in the PC browser and sign in. The token
   is stored on the server in `$F1DASH_DATA_DIR/auth/f1tv_auth.json`
   (file mode 600).
2. **Copy an existing sign-in.** Copy `data/auth/f1tv_auth.json` from a PC
   where you already signed in into `$F1DASH_DATA_DIR/auth/`.
3. **Token.** Put `F1TV_TOKEN=...` in `server/.env`.

Check with `./server/launch.sh --f1-status`. Without F1 TV, live timing falls
back to the public F1 SignalR feed, and VOD uses the F1 archive / OpenF1.
The dashboard shows which source is active.

## VOYO and AUTO SYNC with a server

VOYO is a DRM-protected browser stream, so it plays on the PC (or TV box)
attached to the TV, not on the server. Set up that machine like this:

1. On the server, set `F1DASH_REMOTE_TOKEN` in `server/.env`, then restart.
2. On the Windows PC, set the same `F1DASH_REMOTE_TOKEN` (a user environment
   variable, or `[remote] token` in `main\config\config.toml`).
3. On the PC, run `launch.bat server http://<server-ip>:8080`.

That PC then shows the dashboard and VOYO (AirParrot `capture3` stays the
default) and runs the VOYO clock bridge. The bridge reads only the
`<video>` element's position, play state and stream identity, then posts
that to `/api/sync/voyo` on the server. The server overlay sets
`[sync] allow_remote_clock = true` so the server accepts those posts; the
token makes sure only your launcher can send them. The server does all
the AUTO SYNC work: stream instances, calibration, LIVE DATA DELAY,
recommended sync and persistence. See `main/README.md` §9c.

## systemd service

```bash
sudo useradd --system --home /opt/f1-dashboard f1          # once (User=f1 in the unit)
sudo chown -R f1: /opt/f1-dashboard
sudo cp server/systemd/f1-dashboard.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now f1-dashboard
sudo systemctl restart f1-dashboard        # after git pull / config change
sudo systemctl status f1-dashboard
journalctl -u f1-dashboard -f              # logs
```

The unit runs `server/launch.sh` from `/opt/f1-dashboard/main`, reads
`server/.env`, and keeps its data in `/var/lib/f1-dashboard`
(`StateDirectory`). To keep your existing data, either copy `data/*` there or
change `F1DASH_DATA_DIR` in the unit.

## Docker

```bash
cd server/docker
docker compose up -d --build
docker compose logs -f
```

The image uses the repo root as its build context. Data lives on the
`../../data:/data` volume (the same `data/` folder as a plain start).

## VOYO stream recordings — choosing the disk

The server writes one package per detected VOYO stream instance: identity, playback timeline, sync
anchors and observations, LIVE DATA DELAY, a manifest and `index.json`, plus the opt-in window
capture. The file contract and load API are in `main/README.md` §9d. These packages are kept
**separate** from the F1 timing recordings in `data/recordings/`. A path inside that folder is
refused.

Set the path in `server/config/server.toml` (`[voyo.recording] path`) or in `server/.env`:

```bash
F1DASH_VOYO_RECORDING_PATH=/mnt/data/f1-voyo-streams     # any disk / mount, absolute = as is
```

How the path is resolved:
- An absolute path is used as is.
- `data/...` goes into `$F1DASH_DATA_DIR`.
- Any other relative path is relative to `main/`.
- The default is `data/voyo_streams`.

At startup the server resolves the path, creates it (if `create_path_if_missing = true`), write-tests it
and logs it:

    VOYO stream recordings: /mnt/usb/f1-voyo (free 812.4 GB)

If the path is not usable, the server logs `VOYO stream recording DISABLED: <reason>` (also shown
in `GET /api/voyo/recordings`) and **writes nowhere else**. The dashboard keeps working.

If a write fails later (disk full, USB disk unplugged), recording stops and the error is logged.
The server re-checks the path every 60 s and continues the open package once it is writable again.
`min_free_bytes` (default 2 GiB) stops writing before the disk fills.

### External USB hard drive (configured: `/mnt/f1disk`)

`server/config/server.toml` already points the recordings at the disk:

```toml
[voyo.recording]
path = "/mnt/f1disk/voyo_streams"
require_mount = "/mnt/f1disk"
```

Until a disk is mounted at `/mnt/f1disk`, the log shows `VOYO stream recording DISABLED: no disk
mounted at /mnt/f1disk ...`. The dashboard works normally and **nothing is written to the system
disk**. The path is checked again every 60 s while VOYO samples arrive. Once the disk is mounted,
recording starts on its own and creates `voyo_streams/` on the disk; no restart is needed.

Mounting it (once):

```bash
lsblk -f                                   # find the disk, e.g. /dev/sdb1, and its UUID + type
sudo mkdir -p /mnt/f1disk
# /etc/fstab - nofail: the server still boots without the disk
UUID=<uuid>  /mnt/f1disk  ext4  defaults,nofail,x-systemd.device-timeout=10  0  2
#   NTFS:  UUID=<uuid>  /mnt/f1disk  ntfs3  defaults,nofail,uid=f1,gid=f1  0  0
#   exFAT: UUID=<uuid>  /mnt/f1disk  exfat  defaults,nofail,uid=f1,gid=f1  0  0
sudo systemctl daemon-reload && sudo mount /mnt/f1disk
sudo chown f1: /mnt/f1disk                 # ext4 only (NTFS/exFAT: uid=f1 above)
```

Here `f1` is the service user from `systemd/f1-dashboard.service`; when started by hand, it is your
own user. To use another mount point, change both lines in `server.toml`, or set
`F1DASH_VOYO_RECORDING_PATH` and `F1DASH_VOYO_RECORDING_REQUIRE_MOUNT`.

### Window capture (opt-in)

Set `record_video_capture = true` (or `F1DASH_VOYO_RECORDING_RECORD_VIDEO_CAPTURE=true`). The server
then asks the PC that shows VOYO to record that window with ffmpeg (the PC needs ffmpeg; see
`pc variant/README.md`). The PC uploads finished segments to `<path>/<stream_instance_id>/capture/`.

- It is a screen recording of the window. It never reads the protected stream, and the result is
  black if the browser or OS blanks protected video.
- Video is deleted after `keep_<session>_days`; the metadata of the package stays.
- A segment of 60 s at 1080p30 is about 30–60 MB, so plan on several GB per race.

## Where things are stored

`$F1DASH_DATA_DIR` defaults to `<repo>/data`:

| What | Where |
|---|---|
| Logs | systemd: `journalctl -u f1-dashboard`; docker: `docker compose logs`; by hand: `data/logs/server.log` |
| F1 TV sign-in | `data/auth/f1tv_auth.json` (never commit it; git-ignored) |
| Sync state | `data/sync_calibration.json`, which holds per-video calibrations, sessions, Event Sync points, MARK STREAM START, and the `autosync` section (stream instances, learned live latency) |
| VOYO stream recordings | `[voyo.recording] path` (default `data/voyo_streams/`): one folder per stream instance + `index.json` |
| Recordings | `data/recordings/*.jsonl.gz`, the F1 timing feed. They are recorded automatically in LIVE mode (`[live] record = true`) and replayed with `./server/launch.sh --replay [file]` |
| Caches | `data/` (OpenF1 / archive caches, track maps) |

Recording covers the F1 data feed only. VOYO video is DRM-protected and is
not recorded.
