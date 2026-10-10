# server/ — Linux deployment (primary)

> **Setting up a new server from scratch? Follow [`SETUP.md`](SETUP.md)**: every step from a fresh
> Linux install (users, disk, GitHub access, services, F1 TV and VOYO sign-in), nothing left out.

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
| `systemd/f1-voyo-player.service`, `voyo-player.sh`, `setup-voyo-player.sh` | the server VOYO player: opens VOYO and records it around F1 sessions (see below) |
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
# user f1, /opt/f1-dashboard, /var/lib/f1-dashboard: see SETUP.md steps 3-7
sudo cp server/systemd/f1-dashboard.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now f1-dashboard
sudo systemctl restart f1-dashboard        # after a config change (updates: server/update-f1dash.sh)
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

## Recording VOYO on the server (no PC needed)

The server can open VOYO itself and record it, so your PC doesn't need to be on.
`main/tools/voyo_server_player.py`, run as the `f1-voyo-player` service, does this:

1. **Before each F1 session** of `record_sessions`, it starts a virtual screen (Xvfb, no monitor
   needed) with Google Chrome signed in to **your** VOYO account. The session times come from the
   official schedule; the default window is 15 min before the start until 30 min after the end, and
   longer while the live feed says the session still runs.
2. **It plays the stream:** it opens the F1 live page and presses play, sound on and the player's
   own fullscreen, the same buttons you would press.
3. **The F1 data side:** the read-only clock posts the video position to the dashboard server. The
   server writes it as a VOYO stream recording on the disk (`[voyo.recording] path`, channel
   `server_player`), with the F1 session and LIVE DATA DELAY attached.
4. **The video side:** ffmpeg records that screen and its sound into the recording's `capture/` folder.
5. **After the session** it closes VOYO. That package is complete and appears in `GET /api/voyo/recordings`.

The recording is a screen recording of what Chrome displays. The tool never touches VOYO's stream,
keys or DRM. VOYO needs Chrome's Widevine module, which Google Chrome has; if Chrome can't play the
stream, nothing gets recorded. While it records, the server's stream counts as one of your
account's devices/streams, so check VOYO's limit if you also watch on the PC. The recordings are
for your own use.

Your Windows PC is not involved. When it is on and showing VOYO, its window is a separate stream
(channel `viewer`) for the dashboard sync, and the two are never mixed.

### Setup (once)

```bash
sudo ./server/setup-voyo-player.sh            # Google Chrome, Xvfb, PulseAudio, ffmpeg, x11vnc
# sign in to VOYO on the server's virtual screen - as the service user, with the service's data dir:
sudo -u f1 env F1DASH_DATA_DIR=/var/lib/f1-dashboard HOME=/var/lib/f1-dashboard ./server/voyo-player.sh login
```

`login` prints an SSH tunnel command and a one-time VNC password.

1. On your PC, run `ssh -L 5900:127.0.0.1:5900 <user>@<server>`.
2. Open a VNC viewer (e.g. RealVNC Viewer or TigerVNC) to `127.0.0.1:5900`.
3. Sign in to VOYO and open the **F1 live stream page** you want recorded.
4. Press Ctrl+C in the `login` terminal. The page that is open becomes the stream page, saved in
   `voyo_server_player.json`. Instead of steps 3–4 you can set `[voyo.server_player] stream_url` in
   `server/config/server.toml`.

VNC listens only on 127.0.0.1 and only while `login` runs. Your password is typed into VOYO's own
page; the tool doesn't store it, and Chrome keeps its normal sign-in cookie in its profile
(`data/browser-profiles/voyo-server`). Repeat `login` if VOYO ever signs you out; the log says
"no video on the page yet ... signed in?".

```bash
./server/voyo-player.sh status                # Chrome + Widevine, tools, stream page, disk, next sessions
./server/voyo-player.sh test --minutes 3      # open + record now, then prints the recording
sudo cp server/systemd/f1-voyo-player.service /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now f1-voyo-player
journalctl -u f1-voyo-player -f
```

### Settings (`[voyo.server_player]`, `main/config/config.toml`; server values in `server/config/server.toml`)

| Key | Default | |
|---|---|---|
| `enabled` | true on the server | |
| `stream_url` | "" | the F1 live page; "" = learned at `login` |
| `when` | schedule | `schedule` / `always` / `off` |
| `record_sessions` | all seven | `practice1/2/3, sprint_qualifying, sprint, qualifying, race` |
| `lead_minutes`, `trail_minutes` | 15, 30 | |
| `keep_open_while_feed_live` | true | |
| `record_video` | true | false = only the metadata/timeline package |
| `fullscreen_video` | true | |
| `audio` | true | |
| `resolution` | 1920x1080 | |
| `display` | :90 | |
| `cdp_port` | 9224 | |
| `browser` | Google Chrome | |

Video retention uses the same `keep_<session>_days` as the other recordings. A race at 1080p30 is
roughly 3–6 GB.

## The recorder page: `https://<server-ip>/disk`

Open **https://`<server-ip>`/disk** (or `http://<server-ip>:8080/disk`) in a browser. It works on a laptop and on a phone, and uses
the same style as the dashboard. It shows:

| Part | What it shows |
|---|---|
| **State** (top bar + *NOW*) | `RECORDING` (session, how long, video size), `OPENING` (VOYO opened, waiting for the video), `REST · nothing to do · next: <GP> <session>`, `WAITING FOR DISK`, `PLAYER OFF` (the `f1-voyo-player` service isn't running), `WATCHING (PC)`, `DISK ERROR` |
| **Disk** | used / free / total of the recording disk, how much the recordings take, ≈ hours of video still fitting, writable or not, the reserve (`min_free_bytes`) |
| **Keep video** | how many days the video of each session type is kept (Practice 1–3, Sprint Qualifying, Sprint, Qualifying, Grand Prix, other; 0 = forever); you can change it there, see below |
| **Configuration** | recording path, encoder (Quick Sync / CPU), picture size, which sessions, the window before/after |
| **Recordings** | every recording: date, session, source (server / PC), length, video size, kept until, sync quality, status; click one to play its video segments in the browser (one after another), download segments or the data files, or delete only the video or the whole recording |
| **Log** | what the server did in the last **48 hours**: recordings started / closed, the VOYO player opening / closing, disk problems, mode changes, settings changed, warnings and errors; entries older than 48 h are deleted from the file |

- **Password:** the whole page and every `/api/disk/...` call need a `/disk` login (see
  **Security** below). The first visit asks you to create the password with a one-time setup code
  from the server. Deleting, changing the password and the security actions ask for the password
  again if you signed in more than 10 minutes ago. **LOG OUT** ends the session.
- Keep-video days changed on the page are stored in `<data>/voyo_recording_settings.json` and
  override `server/config/server.toml`; the table marks them with **PAGE**. A shorter time deletes
  older video at once, after the page asks you to confirm.
- The log is `<data>/logs/activity.jsonl`. It is fed by the dashboard server and the server VOYO
  player.
- Use **https://`<server-ip>`/disk** (HTTPS, below): on plain http the password travels
  unencrypted and the login page warns you.

## The VOYO account on `/disk`

In the **VOYO ACCOUNT** panel of `/disk` you enter your VOYO e-mail, password and the **F1 stream
page** (the address you watch F1 on). The server VOYO player then signs in by itself:

- when VOYO shows its sign-in form or "Prijava" button (e.g. after VOYO signed it out; at most once
  every 10 min);
- when you press **LOGIN NOW**. It takes about 15 s; the result shows on the page and in the log.

It types the e-mail and password into VOYO's own sign-in form, the same way a password manager
does. Nothing of VOYO's stream or DRM is touched.

- **Storage:** `<data>/auth/voyo_credentials.json` (file 600, folder 700, readable only by the `f1`
  user). The page and API never show the password; the e-mail is masked as `r***@gmail.com`.
  **FORGET** deletes both.
- **Login:** only a signed-in `/disk` session can see or change it.
- **Use https:** on plain http the password travels unencrypted over your home network, and the
  page warns you. Use **https://`<server-ip>`/disk** (HTTPS, below).
- **If VOYO asks for an extra check** (a code, a captcha), the automatic sign-in can't do it. The log
  says so; sign in once by hand with `voyo-player.sh login` (VNC).
- **Not tested on the real site yet:** the automatic sign-in was tested against a page built like a
  normal sign-in form, not against voyo.si itself. If it doesn't find VOYO's form, send me the log
  line `VOYO login FAILED: ...`.

## `/tv`: the stream and the dashboard on one page

Open **https://`<server-ip>`/tv** (or `http://<server-ip>:8080/tv`) on the TV, a laptop or a phone.
It shows the dashboard in full, and **the server's live VOYO stream inside the dashboard's video
slot**. You don't need a PC or a VOYO window.

- **How it works:** while the server VOYO player records a session, ffmpeg's *tee* sends the same
  encoded picture to two places, with no second encode:
  - the recording on the disk;
  - a live HLS stream in 2-second pieces, the newest 8 kept, in `<data>/live/`, each piece marked
    with its capture time.

  `/tv` plays that stream with hls.js (bundled in `server/tv/vendor/`, so no internet is needed).
  It is about 6–10 s behind the server's VOYO picture. When nobody watches, nothing is sent; the
  pieces are just replaced on the server.
- **When nothing is being recorded:** `/tv` shows "NO LIVE STREAM · next: <GP> <session> - the
  stream starts in …", and the dashboard still works.
- **Layouts:** **1** RACE VIEW (dashboard + video), **2** VIDEO (big video), **3** DASHBOARD only,
  **M** sound, **F** fullscreen. Move the mouse to see the buttons. Sound starts muted (browsers
  block autoplay with sound); click once.
- **Approval - every time:** each load of `/tv` (first open, reload, new tab, browser restarted,
  the URL in another browser) shows a new code (e.g. `C6X-S6V`) and waits. Your **trusted phone**
  (its `/remote` page) shows the same code with **APPROVE / DENY**. Only that load of the page is
  approved; reloading asks again. Nothing is remembered: no "trust this browser". See **Security**
  below. `/disk` shows ON AIR / OFF, how many are watching, and the lag.
- **Only on your home network:** don't forward the ports to the internet. Your VOYO subscription
  is for you; check VOYO's terms.
- **Not done yet (next step):** the dashboard's data is synced to live time (SYNC) as on the PC, but
  not yet to this stream's extra few seconds of delay. The stream's pieces carry the capture time,
  and that is what will be used for it.

## HTTPS: `https://<server-ip>/tv` and `/disk`

```bash
sudo ./server/make-https-cert.sh             # self-signed certificate for this server's IP (10 years)
sudo ufw allow 443/tcp
sudo systemctl restart f1-dashboard
```

The server then answers on **https (port 443)** and on http (8080) as before. `server.toml` sets
`https_port = 443`, and the systemd unit may use port 443 (`AmbientCapabilities`).

- The first time, the browser warns that the certificate isn't trusted (it is self-made). Click
  *Advanced → Proceed / Continue*; after that the connection is encrypted.
- Started by hand (not by systemd), port 443 isn't allowed. The log says "HTTPS port 443 not
  usable" and only http runs.
- If the server's IP changes, run the script again.

## Security: `/disk` password, trusted phone, `/tv` approval

Everything is checked **by the server**, on every page, API call, video piece and WebSocket, not
only in the HTML. Without a valid session the server answers `401`.

**First run (once):**

1. On the server: `sudo cat <data>/auth/disk-setup-code` (for systemd: `/var/lib/f1-dashboard/auth/disk-setup-code`;
   the page shows the exact path). The file is readable only by the `f1` user and root. The code
   is never written to the log.
2. Open **https://`<server-ip>`/disk**, enter the code and create a password (at least 10
   characters). The code file is deleted after that.
3. On your phone open **http(s)://`<server-ip>`/remote**. The top bar shows **THIS DEVICE**, a name
   and a short code (e.g. `VUT-XCP`); **RENAME** gives it a name you recognise.
4. In `/disk` → **SECURITY** the connected `/remote` devices are listed with the same codes. Press
   **USE FOR AUTH** on your phone. Its `/remote` now shows **TRUSTED (approves /tv)**.
5. Open `/tv` on the TV and approve it on the phone - and again every time `/tv` is loaded.

**How it works:**

| | |
|---|---|
| `/disk` password | stored only as an **Argon2id** hash (`argon2-cffi`) in `<data>/auth/security.json` (600, folder 700); never logged, never sent to the browser |
| `/disk` login | a random session in an HttpOnly cookie (`SameSite=Strict`, `Secure` on https), 12 h; changes need a CSRF token; 5 wrong passwords lock that address out for 5 min (30 for everybody per 10 min) |
| `/remote` devices | each browser gets a random device id in an HttpOnly cookie (400 days), not the IP. *Connected* is not *trusted*: only the one device chosen in `/disk` is trusted |
| `/tv` challenge | made for **each load** of `/tv`: random id + human code, expires after 2 min (`tv_request_seconds`), max 6 per minute per address. The browser gets a random secret in an HttpOnly cookie, the page a second one it keeps only in memory; both are needed to pick up the result. Loading `/tv` again, or asking again, cancels the browser's previous challenge. Closing or leaving a page that still waits withdraws its challenge (best effort, `sendBeacon` to `/api/tv/logout` with the page's own challenge secret; otherwise it just expires), so the phone no longer shows it. A **denied** page stops asking: no retry and no ASK AGAIN until the page is loaded again |
| Who approves | **only** the device chosen with USE FOR AUTH, over its `/remote` connection, for exactly that challenge id. Not `/disk`, not another `/remote` device, not the TV itself. An approval is used once (atomically); a second pick-up, a copy or an expired one gets nothing |
| `/tv` page session | after the approval: a browser-session cookie (HttpOnly, `SameSite=Strict`, `Secure` on https) **plus** a page secret held only in that page's JavaScript memory and sent as a header (`X-F1-TV-Page`; Safari's own video player: `?p=`). Every `/tv` API and video piece needs both. Kept only in the server's memory: a restart, `tv_page_hours` (12 h), **REVOKE** / **REVOKE ALL /TV** in `/disk`, LOG OUT, leaving the page, and **every new load of `/tv` in that browser** end it |
| WebSockets | refused from other sites (Origin check); the trusted device comes from its cookie, not from anything the page sends |
| Other sites | POST/PUT/DELETE from another site are refused for the whole server (cross-site request forgery) |
| Security log | `/disk` → LOG shows logins, failed logins, trust changes, approvals; passwords, hashes, tokens and cookies are never written |

Only hashes of the session tokens are stored on the disk (TV page sessions not at all). Settings are
in `[security]` of `main/config/config.toml` (session lengths, lockout, request time). Older versions
kept approved TVs for 30 days; those stored sessions are deleted on the first start of this version.

**Why every load:** a cookie, a device id, an IP address or a `/disk` login in the same browser never
opens `/tv` - the approval belongs to one page load, and the page's secret dies with the page.
Two tabs in one browser: opening the second ends the first one's approval (one approved TV page per
browser). Several TVs at once: each has its own code; the phone shows them one after another - compare
the code and approve only the one you expect.

**Forgot the `/disk` password:**

```bash
sudo ./server/reset-disk-password.sh          # removes the password and all /disk sessions
sudo systemctl restart f1-dashboard           # makes a new setup code; then do step 1-2 again
```

The trusted phone stays. **Lost the phone:** sign in to `/disk`, press **REMOVE TRUST** (or
**FORGET**) on it and **REVOKE ALL /TV**, then trust the new phone.

**What is NOT protected / limits:**

- The plain dashboard `/` and its data are still open to anyone on your network, as before (the
  PC variant needs that); `/tv` protects the live stream and the TV page. Set
  `[security] protect_dashboard = true` to require an approved `/tv` page or a `/disk` login for it
  too. (The dashboard inside `/tv` is an iframe, which cannot send the page header: for `/` the TV
  page's session cookie is enough while that page's approval lasts.)
- `/remote` control itself still uses the remote token (`F1DASH_REMOTE_TOKEN`), as before.
  The dashboard's phone-remote QR code includes that token only on the server itself or for a
  browser with an approved `/tv` page or a `/disk` login; elsewhere you add `?token=...` yourself.
- Over plain **http** (port 8080) passwords and cookies travel unencrypted on your network. Use
  https. The self-made certificate gives a browser warning the first time; check that you are on
  your server's IP.
- Whoever has your phone (unlocked) or copies its browser cookies can approve `/tv`. Compare the
  code on the phone with the one on the TV before APPROVE.
- Someone who can run JavaScript in the approved TV page itself (e.g. a malicious browser extension
  on the TV) acts as that page while it is open - as with any web login.
- A server restart ends every TV page (the TVs show a new code); `/disk` logins are kept.
- `/api/health` tells programs on the server itself (127.0.0.1) whether a recording is running (for
  the update script); from the network it shows only what it showed before.
- The HTML/JS files of the pages (`/disk-static`, `/tv-static`) are public, but contain no data.
- Don't forward the ports to the internet. For access from outside use the Funnel gateway (**Public access** below), which exposes only `/tv` and `/remote`.

## Public access: `/tv` and `/remote` through Tailscale Funnel

Lets a TV, phone or laptop **anywhere** open `https://<server>.<tailnet>.ts.net:8443/tv` (or `/remote`) in a
normal browser - no Tailscale app on that device, no router port forwarding. `/disk` and everything else stay
on your home network. **Off by default.**

```
browser (internet) --https--> Tailscale Funnel (TLS, :8443) --> tailscaled on the server
   --http--> 127.0.0.1:8090  PUBLIC GATEWAY (main/server/public_gateway.py, allowlist)  --> the F1 app
LAN: 192.168.10.140:8080 (http) and :443 (https) are unchanged and do not go through the gateway.
```

**Why a gateway and not Funnel's own path rules:** `/tv` shows the dashboard, which lives at `/`, and a `/`
mount in `tailscale serve`/`funnel` is a catch-all (it matches every path that no longer mount matches -
tailscale's `getServeHandler`). So Funnel sends everything to the gateway, and the gateway forwards only
an explicit allowlist. Funnel itself is configured with one rule: everything on :8443 -> `127.0.0.1:8090`.

### What is reachable from the internet (all else: 404, never reaches the application)

| Path | Method | Why | Who gets data |
|---|---|---|---|
| `/tv` | GET | the TV page: the approval screen first | anyone (no data in it) |
| `/tv-static/tv.css`, `tv.js`, `vendor/hls.light.min.js` | GET | its code | anyone (code only) |
| `/api/tv/auth/request`, `/api/tv/auth/status` | POST | the per-load challenge | anyone, rate-limited; the result only for that page |
| `/api/tv/status`, `/api/tv/logout` | GET / POST | the approved page's state / end | approved page: session cookie **and** page secret |
| `/tv/live/index.m3u8`, `/tv/live/live_NNNNN.ts` | GET | the live stream | approved page: session cookie **and** page secret |
| `/` | GET | the dashboard inside `/tv`'s iframe | approved `/tv` page or the trusted phone, else → `/tv` |
| `/static/` `style.css` `tv.css` `app.js` `components/{voyo_player.js,voyo_player.css,f1time.js,qrcode.js,pitlane.js,team_radio.js}` | GET | the dashboard's code | anyone (code only) |
| `/api/track/layouts`, `/api/media/catalog` | GET | read by the dashboard / remote | approved `/tv` page or the trusted phone |
| `/api/radio/audio/<16 hex>` | GET | TEAM RADIO playback: a clip of the session shown now, fetched from the F1 archive | approved `/tv` page or the trusted phone |
| `/remote` | GET | the phone remote page | anyone - shows only "not trusted" + its own code |
| `/ws` (WebSocket) | - | dashboard socket; `?client=remote` for the remote | dashboard: approved page, **read-only**; remote: **only the trusted phone** (state, control, approvals); any other device learns only its own name/code |

**Not public** (examples): `/disk`, `/disk-static/*`, `/api/disk/*` (login, setup, security, recordings,
VOYO account), `/api/health`, `/api/state`, `/api/sync*`, `/api/mode`, `/api/remote/*` (incl. the token
QR info), `/api/voyo/*`, `/api/radio/clips`, `/api/radio/transcript`, `/f1tv/*`, `/api/diagnostics`, `/tv/`, `/static/remote.html`, the PC launcher /
clock bridge / capture endpoints.

### The rules the gateway enforces

* **Exact paths only** - a path with any `%`-encoding, `//`, `.`/`..` segments, `\`, `;`, control or
  non-ASCII characters, or over 200 characters is refused (400), never normalised. Case matters.
* **Methods per route** (no HEAD/OPTIONS/PUT/DELETE/TRACE ...); WebSocket only on `/ws`.
* **Query parameters per route** (`/?layout=`, `/tv?next=/`, `/api/media/catalog?year=`, `/ws?client=`,
  `/tv/live/*?p=`); anything else - in particular `?token=` - is refused (400). The remote token is never
  used, shown or accepted on the public side.
* **Host:** only the configured `*.ts.net` name (any port) - else 421.
* **Visitor address** from `X-Forwarded-For` (set by tailscaled, client copies removed by it); a loopback,
  missing or invalid one becomes `public` - a visitor can never look like the server itself (the things the
  app allows only to 127.0.0.1 stay local).
* **https** for the app (Funnel terminated TLS): cookies are `Secure`, `HttpOnly`, `SameSite` as on the LAN,
  host-only (`*.ts.net` cookies are separate from `192.168.10.140` ones).
* **Rate limits:** 600 requests/min, 6 open WebSockets and 30 WebSocket connects/min per visitor, 40 public WebSockets in all; TV challenges
  6/min per visitor and 30 per 10 min for all public visitors; new `/remote` identities 5/hour per visitor
  and 40/hour in all; renames 5 per 10 min.
* **Headers:** `Content-Security-Policy` (scripts only from the server - `/remote`'s inline script by its
  SHA-256; no plugins; frames only the dashboard in `/tv`; images also RainViewer radar tiles), `nosniff`,
  `no-referrer`, `X-Frame-Options`, HSTS, `Cross-Origin-Opener/Resource-Policy`, `Permissions-Policy`,
  `no-store` for pages and APIs, no `Server` header. Errors are a bare `{"ok":false,"error":"not found"}`.
* **Logging:** refusals are logged with the path only (never the query string - it may carry a page secret).
* **Fail closed:** `[public] enabled` with an empty / invalid hostname, a privileged port or the dashboard's
  own port → no gateway at all (log: `PUBLIC GATEWAY OFF`). Port 8090 busy → no gateway. Gateway not running
  → Funnel answers 502. The gateway listens on 127.0.0.1 only.

`/tv` keeps all its rules over Funnel: a new approval on the trusted phone for **every** load, the per-page
secret on every API call and video piece, single-use challenges, nothing before the approval. `/disk` can
never approve. The phone approves TVs on the LAN and on the internet alike (one list of challenges).

### Setting it up (by hand, on the server - nothing here is automatic)

0. **Prerequisites:** Tailscale on the server, signed in (`tailscale status`), version **1.52 or newer**
   (`tailscale version`; the `--bg`/`--https` syntax). In the Tailscale admin console: **DNS → MagicDNS** and
   **HTTPS Certificates** on. **Funnel needs the `funnel` node attribute** in the tailnet policy - this is
   the one policy change, and it is required: Funnel cannot be used without it. Grant it **only to this
   server** (e.g. tag the server `tag:f1dash` and use `"nodeAttrs": [{"target": ["tag:f1dash"], "attr": ["funnel"]}]`),
   not to `autogroup:member`. (`tailscale funnel` prints a link to the admin console if it is missing.)
1. **Update the code** (a release with this feature): `sudo /opt/f1-dashboard/server/update-f1dash.sh`.
2. **Your hostname:** `tailscale status --json | grep -m1 '"DNSName"'` → e.g. `f1server.tail1234.ts.net.`
   (use it without the final dot).
3. **Switch the gateway on** (a systemd drop-in; the repository and `server/.env` stay untouched):
   ```bash
   sudo mkdir -p /etc/systemd/system/f1-dashboard.service.d
   sudo cp /opt/f1-dashboard/server/systemd/public-gateway.conf /etc/systemd/system/f1-dashboard.service.d/
   sudo nano /etc/systemd/system/f1-dashboard.service.d/public-gateway.conf     # set F1DASH_PUBLIC_HOSTNAME
   sudo systemctl daemon-reload
   sudo /opt/f1-dashboard/server/update-f1dash.sh --force-restart             # refuses during a recording
   journalctl -u f1-dashboard -n 50 --no-pager | grep -i "public gateway"     # "Public gateway for https://..."
   ```
4. **Check it locally, before anything is public:** `sudo /opt/f1-dashboard/server/update-f1dash.sh --verify-only`
   - its "public gateway" block must be all `ok` (`/tv` approval screen, `/disk` & co. 404, tricks 400, other
   hosts 421).
5. **Open Funnel** (port 8443, so nothing that reaches port 443 changes):
   ```bash
   sudo tailscale funnel --bg --https=8443 http://127.0.0.1:8090
   tailscale funnel status          # https://<host>:8443 (Funnel on) |-- / proxy http://127.0.0.1:8090
   ```
   Only this one rule. Do not add `tailscale serve`/`funnel` rules for 8080 or 443.
6. **Check from the internet** (a phone on mobile data with Wi-Fi off, or any computer outside):
   ```bash
   H=https://<host>:8443
   curl -s -o /dev/null -w '%{http_code}\n' $H/tv                 # 200 (approval screen)
   curl -s -o /dev/null -w '%{http_code}\n' $H/disk               # 404
   curl -s -o /dev/null -w '%{http_code}\n' $H/api/disk/status    # 404
   curl -s -o /dev/null -w '%{http_code}\n' $H/api/health         # 404
   curl -s -o /dev/null -w '%{http_code}\n' $H/api/tv/status      # 401
   curl -s -o /dev/null -w '%{http_code}\n' $H/                   # 303 (to /tv)
   curl -s -o /dev/null -w '%{http_code}\n' --path-as-is $H/tv/../disk   # 400
   curl -s -o /dev/null -w '%{http_code}\n' "$H/remote?token=x"   # 400
   ```
7. **The phone** (one time): open `https://<host>:8443/remote` on it - it shows *THIS DEVICE IS NOT THE
   TRUSTED PHONE* and a code. At home, in `/disk` → SECURITY press **USE FOR AUTH** on that code; the phone
   reloads into the full remote. Bookmark that URL. (There is one trusted device: the phone's LAN identity
   `https://192.168.10.140/remote` is then no longer the approver - use the `ts.net` address on the phone at
   home too, or switch back in `/disk`.)
8. **The TV:** open `https://<host>:8443/tv`, approve the code on the phone - every time it is loaded.

### Turning it off / rollback

```bash
sudo tailscale funnel --https=8443 off                         # 1. nothing public any more (immediate)
tailscale funnel status                                        #    "No serve config"
sudo rm /etc/systemd/system/f1-dashboard.service.d/public-gateway.conf   # 2. no gateway
sudo systemctl daemon-reload && sudo /opt/f1-dashboard/server/update-f1dash.sh --force-restart
sudo /opt/f1-dashboard/server/update-f1dash.sh --rollback      # 3. (only if needed) the previous code
```

Then make the phone's LAN identity the approver again in `/disk` (USE FOR AUTH) if you switched it. Step 1
alone already closes everything from the internet; the LAN works the same with or without the gateway.

### Limits and what was tested

* The `*.ts.net` name is **public knowledge** (Tailscale's certificates are in Certificate Transparency logs);
  nothing relies on it being secret. Anyone on the internet can load `/tv` and `/remote` and create
  approval requests (rate-limited) - your phone may show requests you did not expect: **DENY** them, and
  turn Funnel off (step 1) if it keeps happening.
* From the internet the dashboard is **read-only**: MODE / SYNC / circuit choices in the dashboard do
  nothing there; the trusted phone's remote controls it as on the LAN. VOYO embedding and the CDN hls.js
  inside the dashboard are blocked by the CSP (the `/tv` live stream uses the bundled hls.js).
* The dashboard inside `/tv` is an iframe, which cannot send the page secret: for `/` and its two read APIs
  the approved page's session cookie (or the trusted phone's device cookie) is enough while that approval
  lasts. The stream and the `/tv` APIs need the secret.
* Per-visitor limits use the address tailscaled reports; visitors behind one address (CGNAT, a company
  proxy) share them.
* `/remote`'s CSP hash is computed at start: after `remote.html` changes the server must restart (the update
  script does).
* Automated (`main/tests/test_public_gateway.py`, temporary data directories): allowlist, path / host / method
  / query tricks, private routes, WebSockets, per-load approval, stale / forged / replayed secrets, untrusted
  devices, rate limits, cookies / headers, LAN unchanged. Also run by hand here: the real server with the
  gateway on 127.0.0.1, `curl --path-as-is` and raw-socket probes, and three headless browsers through a
  local TLS proxy that imitates Funnel for a `*.ts.net` name (approve, reload, dashboard iframe, WebSockets,
  live files, no CSP violations). **Not testable here:** real Tailscale Funnel - steps 4-8 above are the
  check on the server.

## Updating the server: `server/update-f1dash.sh`

```bash
sudo /opt/f1-dashboard/server/update-f1dash.sh --dry-run     # what would happen - changes nothing
sudo /opt/f1-dashboard/server/update-f1dash.sh               # update, test, restart, verify
sudo /opt/f1-dashboard/server/update-f1dash.sh --wait-idle 180   # during a race weekend: wait for the recording to end
sudo /opt/f1-dashboard/server/update-f1dash.sh --verify-only # only check the running server
sudo /opt/f1-dashboard/server/update-f1dash.sh --rollback    # back to the commit before the last update
```

**Needs:** root (sudo), `git curl tar flock openssl` (Ubuntu has them), the service user's GitHub
access (SETUP.md step 5), the venv `/opt/f1-dashboard/.venv` (created by the first start).

**What it does, in this order** (each step stops the update with a clear message if something is off):

1. Finds the deployment: `f1-dashboard.service` running from this checkout = production. Anywhere
   else (a development checkout) it only updates git, the requirements and runs the tests.
2. Reads the configuration as the service user (`server/.env` is never printed; secrets are never
   traced or put on a command line).
3. Checks the checkout: on `main` (or `--branch`), **no local changes** (it refuses - it never
   discards anything), no local commits, origin reachable.
4. **Never interrupts a recording:** while the VOYO player records or opens VOYO (or the live stream /
   an ffmpeg capture / recording files are active) it refuses (exit 5) - or waits with `--wait-idle`.
   Restarting `f1-dashboard` also restarts `f1-voyo-player` (it `Requires=` the dashboard).
   It also refuses when the recording disk (`require_mount`, e.g. `/mnt/f1disk` with `.f1disk`) is
   not mounted.
5. Reports differences between the installed systemd unit and `server/systemd/` (never overwrites it).
6. Checks the HTTPS certificate: creates it with `make-https-cert.sh` when missing, renews it only when
   it expires within 30 days or is not valid for the server's IP; a certificate that is not this
   server's self-made one is never replaced.
7. **Backup** (before any change) to `/var/backups/f1-dashboard/<time>-<commit>/` (folder 700, files
   600, newest 10 kept): `runtime.tgz` with `<data>/auth` (security state, F1 TV + VOYO sign-in),
   `<data>/tls` and the state files (`*.json`), a copy of `.env`, the installed unit. Not copied:
   recordings, caches, the browser profile. `/var/backups/f1-dashboard/last-deploy` records the
   previous commit; `update.log` the run.
8. Fast-forwards to `origin/main`, `pip install -r main/requirements.txt` into the venv.
9. Runs the tests (`test_security`, `/disk`, `/tv`, VOYO player, phone remote) as the service user in
   a **temporary data directory** - never the real one. A failure puts the previous commit back
   (exit 6); the service was not touched.
10. Restarts `f1-dashboard` only if something that matters changed (not for documentation only), then
    checks: http and https answer, `/disk` and `/tv` APIs and the live stream answer **401** without
    login / approval, forged cookies / page secrets get nothing, approving from `/disk` is refused,
    and `/tv` starts with a fresh approval request even with a stale session cookie. (A real approval
    needs your phone, so the "approval cannot be reused" part is covered by the tests in step 9.)
11. If those checks fail and the previous version was healthy, it **rolls the code back** and restarts
    the previous version (exit 7). Runtime data is never deleted or reset by the script.

Running it again with nothing new changes nothing (no backup, no restart; it only re-checks the
running service). Exit codes: 0 ok, 2 usage, 3 prerequisites, 4 git state, 5 unsafe now (recording /
disk), 6 tests or requirements failed (rolled back), 7 unhealthy after the restart, 8 backup failed.

**Rollback by hand** (if you need the runtime state of a backup too):

```bash
sudo /opt/f1-dashboard/server/update-f1dash.sh --rollback          # code: back to last-deploy's previous commit
sudo systemctl stop f1-dashboard
sudo tar -xzf /var/backups/f1-dashboard/<time>-<commit>/runtime.tgz -C /var/lib/f1-dashboard
sudo chown -R f1:f1 /var/lib/f1-dashboard/auth /var/lib/f1-dashboard/tls
sudo systemctl start f1-dashboard
```

**Manual steps it cannot do for you:** after a new certificate, every browser (PC, phone, TV) shows
the certificate warning once more - accept it for your server's IP; approving `/tv` on the phone.
`--rollback` moves `main` back; the next normal run updates it again.

## Where things are stored

`$F1DASH_DATA_DIR` defaults to `<repo>/data`:

| What | Where |
|---|---|
| Logs | systemd: `journalctl -u f1-dashboard`; docker: `docker compose logs`; by hand: `data/logs/server.log` |
| F1 TV sign-in | `data/auth/f1tv_auth.json` (never commit it; git-ignored) |
| Sync state | `data/sync_calibration.json`, which holds per-video calibrations, sessions, Event Sync points, MARK STREAM START, and the `autosync` section (stream instances, learned live latency) |
| Activity log (the /disk page, 48 h) | `data/logs/activity.jsonl` |
| VOYO e-mail / password (/disk) | `data/auth/voyo_credentials.json` (600) |
| Live stream pieces (/tv) | `data/live/` (only the newest 8, ~10 MB) |
| HTTPS certificate | `data/tls/cert.pem`, `key.pem` |
| `/disk` password hash, trusted phone, sessions | `data/auth/security.json` (600); first-run code `data/auth/disk-setup-code` |
| VOYO stream recordings | `[voyo.recording] path` (default `data/voyo_streams/`): one folder per stream instance + `index.json` |
| Recordings | `data/recordings/*.jsonl.gz`, the F1 timing feed. They are recorded automatically in LIVE mode (`[live] record = true`) and replayed with `./server/launch.sh --replay [file]` |
| Caches | `data/` (OpenF1 / archive caches, track maps) |

Recording covers the F1 data feed only. VOYO video is DRM-protected and is
not recorded.
