# F1 Timing Wall – self-hosted F1 live timing dashboard for a 1920×1080 TV

A Python server that consumes the official F1 live-timing feed, normalizes it
and pushes it over one WebSocket to a single-page TV dashboard: live track map
with real circuit geometry, leaderboard with tyres and FIA penalties, race
control messages, driver telemetry, weather, flags – plus a simulator (TEST
MODE), session replay and an optional remote-control bridge.

> Unofficial personal project. Not associated with Formula 1. The live-timing
> feed is undocumented; use it for private, non-commercial viewing only.

```
 F1 SignalR feed ─┐                    ┌──────────────┐    ┌─ normalizer ─┐                ┌─ TV browser (1920×1080)
 Simulator ───────┼─► raw topics ─►    │   Timeline   │ ─► │              ├─► Hub ─ WS ────┼─ tablet / desktop
 Replay file ─────┘  (F1 event time)   │ state at  X  │    └─ race control┘                │
                                       └──────▲───────┘                                    │
 VOYO <video>.currentTime ─► SyncEngine ─ X ──┘ (video clock · fixed delay · live)         │
 Remote (IR bridge / HTTP / keyboard / WS) ─► RemoteController ─► UI state ─────────────────┘
```

## 1. Project structure

```
f1-dashboard/                  # the repository
├── main/                      # SHARED code - used by both deployments (this manual)
│   ├── main.py                # entry point:  python main.py [--auto|--live|--vod|--test|--replay] [--overlay X.toml]
│   ├── requirements.txt
│   ├── config/config.toml     # shared settings (every key also as env var F1DASH_<SECTION>_<KEY>)
│   ├── server/                # the Python backend package
│   │   ├── app.py             # Starlette HTTP + WebSocket app, remote API
│   │   ├── engine.py          # source -> timeline -> state at the sync target -> hub, learning
│   │   ├── timeline.py        # timestamped F1 event buffer: complete state at any time X (§9b)
│   │   ├── sync.py            # sync manager: VOYO clock <-> F1 time, anchors, confidence (§9b)
│   │   ├── autosync.py        # AUTO SYNC: VOYO stream instances, LIVE DATA DELAY, states (§9c)
│   │   ├── lights_out.py      # the one canonical LIGHTS OUT (+ lights_out_probe.py: public SignalR fallback)
│   │   ├── openf1.py          # OpenF1 client (cached), reference events, session detection
│   │   ├── feedstate.py / normalizer.py / models.py / race_control.py / telemetry.py / track.py
│   │   ├── remote.py / video.py / hub.py / weather.py / radar.py / mode.py / f1tv_auth.py
│   │   ├── recorder.py        # records every live F1-feed session for replay
│   │   ├── team_radio.py      # TEAM RADIO: clip parsing, archive audio fetch, AI transcripts (§9e)
│   │   └── sources/           # f1_live.py (SignalR Core + legacy), archive_follow, replay, vod, simulator
│   ├── dashboard/             # index.html, style.css, app.js, tv.css, remote.html (phone remote)
│   ├── tools/                 # tv_launcher.py (dashboard + VOYO window), voyo_clock.py (+ probe .js),
│   │                          # fetch_tracks.py, probe_feed.py, transcribe_radio.py (optional, §9e)
│   └── tests/
├── server/                    # LINUX SERVER deployment (primary) - see server/README.md
│   ├── launch.sh              # venv + dependencies + main/main.py with config/server.toml
│   ├── config/server.toml     # overlay: only what differs from main/config/config.toml
│   ├── systemd/               # f1-dashboard.service, f1dash-ir-bridge.service
│   ├── docker/                # Dockerfile, docker-compose.yml (alternative to systemd)
│   ├── bridge/ wdtv/          # IR remote bridges (evdev / WD TV Live)
│   └── .env.example
├── pc variant/                # WINDOWS / local-PC deployment - see "pc variant/README.md"
│   ├── launch.bat             # dashboard + VOYO window + TV agent (+ local server or the Linux server)
│   └── start-windows.bat
├── launch.bat                 # shortcut to "pc variant\launch.bat"
└── data/                      # runtime data, git-ignored: F1 TV sign-in, VOYO browser profile,
                               # sync state, F1-feed recordings, caches (or F1DASH_DATA_DIR)
```

## 2. Install and start

Requires **Python 3.11+** (uses `tomllib`).

* **Linux server (primary):** `./server/launch.sh` - creates `.venv`, installs `main/requirements.txt`,
  starts the backend; systemd / Docker / logs / data locations: **server/README.md**.
* **Windows PC:** double-click `launch.bat` (or `pc variant\launch.bat`); details in
  **pc variant/README.md**.
* By hand (any OS, from `main/`):

```bash
cd main
python main.py                 # AUTO (config default): LIVE while an F1 session is on, else VOD
python main.py --live          # start in LIVE (manual override)
python main.py --test          # TEST MODE simulator
python main.py --replay        # replay the bundled real 2026 Japanese GP sample
python main.py --vod           # follow a VOYO recording (session from the VOYO title, §9b)
python -m unittest discover tests        # self-test
```

### LIVE / VOD mode selector (no restart)

Top right of the dashboard (in RACE VIEW: the session panel under the leaderboard, in VIDEO
FOCUS: the bottom bar): **MODE [AUTO] [LIVE] [VOD]**. The selected one is filled; below it
`DETECTED: LIVE` / `DETECTED: VOD` is what the automatic detection says, and `MANUAL` when you
override it. Click a button, press **E** (keyboard, also forwarded from the VOYO window) or
**MENU** on the IR remote (AUTO → LIVE → VOD, applied 1.5 s after the last press), use the
phone remote (`/remote`, MODE row) or `POST /api/mode {"mode": "VOD"}` (`GET /api/mode` = state).
Remote commands: `CYCLE_MODE`, `SET_MODE:AUTO|LIVE|VOD`, `MODE_AUTO`, `MODE_LIVE`, `MODE_VOD`.

| | |
|---|---|
| `selected_mode` | what you chose: AUTO, LIVE or VOD (start: `[source] mode`, default `auto`; `--live` / `--vod` start with that override) |
| `detected_mode` | the automatic detection, always running: **LIVE** while an F1 session is on (official schedule, 90 min before its start until 60 min after its end - the same rule `tools/tv_launcher.py` used at start - or the live feed's own SessionStatus says it runs), otherwise **VOD**; re-checked every minute |
| `effective_mode` | what runs: the selection, or in AUTO the detection |

Switching stops the current data source and engine and starts the other one in the same
server process: LIVE = the F1 live timing feed (F1 TV sign-in or anonymous, all topics),
VOD = the recording pipeline (session from the VOYO title, VOYO playback clock as the time
reference, the VOYO video layer is turned on). The dashboards get the new mode at once and drop
everything of the old source. A manual LIVE / VOD stays until you choose AUTO again - the
detection never switches it back. LIVE without a session running shows
`NO LIVE SESSION` and keeps waiting on the feed; it connects as soon as F1 starts one.
`--test` / `--replay` are developer sources: there AUTO means that source (detected TEST /
REPLAY), LIVE / VOD still switch. The selection is not saved: every start uses `[source] mode`.
Useful flags: `--port 8080`, `--delay 45` (fixed delay instead of the VOYO video clock, §9b),
`--speed 4` (replay/test speed-up).

## 4. Open the dashboard

`http://<server-ip>:8080/` in the TV's browser (full screen / kiosk mode), e.g.
Chromium kiosk on a Raspberry Pi or mini PC connected to the TV:

```bash
chromium --kiosk --noerrdialogs --disable-infobars http://<server-ip>:8080/
```

The layout is designed for exactly 1920×1080 and scaled to any screen; 1280×720,
desktop and landscape tablets get the same layout scaled, portrait tablets get a
stacked layout. (The WD TV Live itself has no suitable browser.)

## 5. TEST MODE and replay

* `python main.py --test` – a simulated race on a real circuit outline with 22
  **fictional** drivers: cars moving (smoothly interpolated), overtakes, laps,
  sector times, pit stops with tyre changes, a local yellow (one marshal
  sector), VSC, Safety Car, red flag, investigations, time penalties, a drive
  through (+ served), black-and-white flag, deleted lap time, DSQ, post-race
  investigation, rain, telemetry. The script repeats every ~10 simulated minutes.
  An orange **TEST MODE** badge and map watermark are always visible.
  Options in `[test]`: `circuit` (any file in `data/test_tracks/`, e.g. `it-1922`
  = Monza, `at-1969` = Red Bull Ring), `laps`, `time_scale` (`--speed 4`).
  The test pit lane and the 20 test marshal sectors are synthetic test
  geometry and are labelled as such.
* `python main.py --replay` – replays the bundled **real** 2026 Japanese GP race
  recording (timing, tyres, race control, SC period, weather). That third-party
  recording contains no Position/CarData, so the map shows no cars – exactly
  what you see live when F1 withholds positions. By default the replay
  starts 10 s before the session actually starts (`[replay] start_offset =
  "auto"`; recordings usually begin 20+ minutes before lights out – use
  `start_offset = 0` to see the pre-race phase). Speed up with `--speed 4`;
  jump to a later point with e.g. `F1DASH_REPLAY_START_OFFSET=3300`.
* `python main.py --replay data/recordings/<file>.jsonl.gz` – your own live recordings.
* `python main.py --replay latest` or `--replay 2026/2026-03-29_Japanese_Grand_Prix/2026-03-29_Race/`
  – downloads a finished session from the official F1 archive (free, includes
  car positions and telemetry after the session) into `data/archive_cache/`.

## 6. Configure the F1 data source

`config/config.toml`, section `[live]`:

* Endpoint: `wss://livetiming.formula1.com/signalrcore` (SignalR Core, JSON
  protocol, `Subscribe` invocation, messages arrive as `feed` invocations); the
  legacy `wss://livetiming.formula1.com/signalr` (SignalR 1.5) is used as
  automatic fallback (`transport = "auto" | "core" | "legacy"`).
* Topics subscribed (`[live] topics`): the core set (Heartbeat, SessionInfo,
  SessionStatus, SessionData, ExtrapolatedClock, LapCount, TrackStatus,
  DriverList, TimingData, TimingDataF1, TimingAppData, TimingStats,
  RaceControlMessages, WeatherData, TeamRadio, TopThree, PitLaneTimeCollection,
  CurrentTyres, LapSeries, Position.z, CarData.z) plus TyreStintSeries,
  AudioStreams, ContentStreams, TlaRcm, RcmSeries, PitStopSeries, PitStop,
  DriverRaceInfo, OvertakeSeries, ChampionshipPrediction, WeatherDataSeries.
  Every topic that arrives – also one not asked for – is used where the
  dashboard knows it, recorded and listed in the diagnostics. If F1 refuses the
  subscription, the next attempt uses the core set.

### 6a. F1 TV sign-in (`[f1_tv]`, default on)

```toml
[f1_tv]
subscription = true     # false = never sign in, anonymous public feed only
open_browser = true
safety_car_position_keys = []
```

* **First start** (no stored sign-in): the server opens
  `http://127.0.0.1:8080/f1tv/login` in the default browser – a page served by
  the dashboard itself (no third-party site). (1) It opens the official
  `account.formula1.com` sign-in, where you sign in as usual. (2) Drag its
  **F1 Dashboard sign-in** button to the bookmarks bar once. (3) Click that
  bookmark on the signed-in formula1.com tab: it posts the session's
  *subscription token* (from the `login-session` cookie) to 127.0.0.1 – no
  password, cookie or token is typed into the terminal. Browsers that block
  bookmarklets: the page also takes the `login-session` value pasted from
  DevTools. Meanwhile the dashboard runs on the anonymous feed and reconnects
  authenticated as soon as the sign-in arrives.
* Stored: only the token, in `data/auth/f1tv_auth.json` (mode 0600, git-ignored;
  `data/auth/signin_key` = the bookmark's per-installation key). Never logged,
  never sent to a dashboard / WebSocket, never in recordings.
* **Later starts** reuse it (no browser). Expired, rejected by F1 (HTTP 401/403
  on negotiate or the WebSocket), or deleted → the sign-in page opens again
  (at most every 10 min) and the anonymous feed is used meanwhile.
  `python main.py --f1-login` forces a new sign-in, `--f1-logout` deletes it,
  `--f1-status` shows state / product / expiry (no secret).
* Status everywhere: **AUTHENTICATED** or **ANONYMOUS** (log, `/api/diagnostics`,
  dashboard data-source line). "Authentication: SUCCESS" only after F1 accepted
  a connection with the token.
* Nothing is assumed from the subscription tier: which topics F1 actually
  streams to your account is measured. `python main.py --diagnose 120`
  connects for 120 s and prints ✓ / ✗ per topic, how many drivers deliver
  fresh `Position.z` / `CarData.z`, which CarData channels appear, safety-car
  position availability, track geometry, tyres, race control, weather. The same
  report: `http://localhost:8080/api/diagnostics?format=text` and in the log
  45 s after connecting, then every 10 min / when the set of topics changes.
* Docker: the sign-in needs a browser on the same machine and a loopback
  request – run it once outside Docker (or set `F1DASH_F1_TV_SUBSCRIPTION=false`).
* The legacy manual token (`live.f1tv_token` / `F1TV_TOKEN`) still works and
  takes precedence.

What the authenticated data adds, when F1 actually sends it:

* **Position.z** – per car X / Y / Z (local track coordinates, not GPS), status,
  sample time; stale after 5 s (faded on the map where it was last seen, hidden
  after 15 s – never moved on a guess). Keys that are not on the driver list are
  *non-driver objects*: listed in the diagnostics, never drawn as cars.
* **Safety car position** – only if a key listed in
  `safety_car_position_keys` is actually in Position.z (exposed as
  `state.map.safety_car` = `{available, x, y, z, age_ms, fresh}` and drawn as an
  "SC" box). Otherwise `available: false` with the reason (e.g. "Position.z has
  1 non-driver object (241) – none is configured as the safety car"). Add a key
  only after seeing it in the diagnostics and knowing what it is.
* **CarData.z** – speed, RPM, gear, throttle, brake (on/off), DRS (pre-2026);
  every other channel F1 sends is passed on raw (`channels`); ERS is not in the
  feed → `null`. WebSocket `tel` = one object per car with `age_ms` / `fresh`;
  stale after 5 s, values hidden after 30 s.
* **TyreStintSeries** – stints (compound, new/used, tyre age, laps, stint number)
  when TimingAppData has none for a car.
* Track status: `state` (GREEN / YELLOW / DOUBLE_YELLOW / SAFETY_CAR / VSC /
  VSC_ENDING / RED_FLAG / CHEQUERED, unknown official codes as
  `TRACK_STATUS_<code>`), `timestamp`, `pit_exit` / `pit_entry` (only from
  literal race-control messages), `red_flag_restart`.
* Race control messages: `tags` (investigation, penalty, deleted_lap,
  track_limits, unsafe_release, pit_lane, safety_car, red_flag, …, only from the
  message itself), `importance`, F1's own `Mode` / `Status`.

* Session detection is automatic: `SessionInfo` decides meeting, session type
  (Practice / Qualifying / Sprint Qualifying / Sprint / Race), circuit and
  track map; a new session resets the state without a restart. When nothing is
  live, the last session stays on screen and the next scheduled session
  (from the season `Index.json`) is shown on the map.
* `record = true` stores every live session in `data/recordings/` for replay.
* Synchronising with the TV picture: with the VOYO window the dashboard follows
  the video player's own clock (§9b). Without it, `[source] delay_seconds`
  (or `[sync] mode = "DELAY"`) shows the F1 state of N seconds ago – measured
  on F1's event timestamps, not on when the packets arrived.
* Reconnects: exponential back-off 2 s → 60 s with jitter, silence watchdog
  (`silence_timeout`, 60 s), fresh snapshot after every reconnect. The TV shows
  **LIVE DATA DISCONNECTED · RECONNECTING…** with attempt and countdown and
  keeps the last data visible (dimmed). The snapshot replaces the feed topics
  but keeps what was derived from the updates before the gap (lap history,
  knocked-out part, since when a car is in the pit / stopped); it is dated on
  F1's clock, so a delayed / video-synced board never shows it early.
* Connection state in the mode badge: **LIVE**, **LIVE · CONNECTING**,
  **LIVE · RECONNECTING**, **LIVE · DELAYED** (socket open, no message from F1
  for 25 s – F1 sends a heartbeat every 15 s; banner + dimmed board and clock),
  **LIVE · DISCONNECTED** (this screen lost the dashboard server),
  **LIVE · FINISHED**. Connected without a SessionInfo: **SESSION UNKNOWN**.
* Live F1 time = this computer's clock corrected by the measured offset of
  the feed timestamps (latency + PC clock error), so a wrong PC clock moves
  neither the session clock nor the pit / garage / DNF timers at the live edge.
  Messages delivered late (the feed does that by up to ~2 s) are applied in F1
  time order – an older clock / track status post never undoes a newer one.
  A delayed board (`delay_seconds`, VOYO) still counts on the PC clock: keep
  it synced (the log warns above 2 s).
* `LapSeries` is subscribed as well (the lap tracker's second line-crossing
  signal; added automatically if an older `config.toml` omits it).

## 7. Which values the feed really provides

| Dashboard field | Source | Availability |
|---|---|---|
| Session, lap X/Y, clock | SessionInfo, SessionStatus, LapCount, ExtrapolatedClock | free |
| Track status GREEN/YELLOW/SC/VSC/RED | TrackStatus | free |
| Yellow **per marshal sector** | RaceControlMessages (`Scope=Sector`, `Sector=n`) mapped on MultiViewer marshal sectors | free |
| Positions, gaps, intervals, last/best lap, sectors (purple/green), speed trap, in pit / pit out, retired, Q knock-out | TimingData / TimingDataF1 | free |
| Tyre compound, new/used, age, stint history | TimingAppData (+ CurrentTyres) | free |
| Investigations, penalties, served, DSQ, black/white flag, deleted laps | RaceControlMessages text | free |
| Overtake ENABLED/DISABLED (2026, session-wide), DRS ENABLED/DISABLED (≤2025) | RaceControlMessages | free |
| Weather (air, track, humidity, pressure, wind, rain) | WeatherData | free |
| Car positions on the map | Position.z (~4 Hz) | live socket: entitled connections only (§7a); otherwise public archive stream if readable; else N/A |
| Speed, RPM, gear, throttle % | CarData.z channels 2, 0, 3, 4 | same as Position.z |
| Brake | CarData.z channel 5 | same as Position.z; **ON/OFF only** – no brake pressure % exists in the feed |
| DRS state | CarData.z channel 45 | seasons ≤ 2025 only; DRS was abolished for 2026 → **N/A** |
| ERS / battery %, deployment / harvesting | – | **not in any public topic → always N/A** |
| Per-driver Overtake mode | – | **not in the feed → always N/A** |
| Safety-car position | – | the SC is not in Position.z → **N/A** (SC status is shown) |
| DNF / IN PIT next to the driver | `Retired`, or `Stopped` with no sector/lap data for 60 s → **DNF**; `InPit` for > 75 s (or DNF in the pit) → **IN PIT** (garage) and the car is not drawn on the map. An `InPit` flag that stays set while the car keeps setting sectors (lost "left the pit" message, seen in real 2026 data) is ignored | derived from TimingData / TimingDataF1, kept with the checkpoints (correct after seeking) |
| Race: CHASING / POSITION / TYRE AGE (in place of S1-S3, race and sprint only) | CHASING = consecutive completed laps within `[dashboard] chase_gap_seconds` (1.5 s) of the SAME car ahead (`IntervalToPositionAhead` at each of the car's line crossings; ends on a bigger gap, another car ahead, a pit lap or a SC / VSC / red-flag lap; `—` before the first lap). POSITION = grid (`TimingAppData` GridPos) minus current position. TYRE AGE = `TotalLaps` of the current set | derived in the server (`server/chase.py`), restored with checkpoints / seeks |
| Pit lane geometry | reconstructed from real positions of complete pit-lane passes (`InPit` / `PitOut` / `PitLaneTimeCollection`) of the **last finished race at this circuit in the F1 archive** | cached per circuit; else loaded from the archive as soon as the circuit is known – no pit stop of the current session needed (§8) |

## 7a. Car positions without F1 TV – what the code of other projects shows

Checked against the actual source (September 2026) of the two projects that
advertise live maps, and the libraries they use:

| Project | How it gets Position.z / CarData.z live | Auth | Evidence it works live without F1 TV |
|---|---|---|---|
| [hung-ng/boxbox](https://github.com/hung-ng/boxbox) (Rust) | `src/source/live.rs`: POST `/signalrcore/negotiate?negotiateVersion=1` → `connectionToken` → `wss://…/signalrcore?id=…`, JSON handshake, `Subscribe` incl. `Position.z`, decode base64 + raw-deflate (`src/state/mod.rs`). No proxy, no cache. | none (no Authorization header anywhere) | **No.** Its own `docs/architecture.md` (§"Key patterns & gotchas", commit 5f5dbb0, 2026-07-24): *"`Position.z` can go silent on the live feed (observed Hungary FP1, session green and running). The server accepts a Subscribe containing `Position.z` and returns no error, then never sends the topic … Probed `Position.z` alone, `Position` (uncompressed) and `CarData.z`: all silent … Net effect: tower updates live, map has no cars. Suspected entitlement gating."* The commit added a "waiting for car positions…" hint to the map for exactly this case. Its map with cars works in **replay** (static archive). |
| [mricero/F1-Telemetry-Dashboard](https://github.com/mricero/F1-Telemetry-Dashboard) (Streamlit) | `data/live_adapter.py`: `livef1.adapters.RealF1Client` (default) or `fastf1.livetiming.SignalRClient(…, no_auth=False)` | LiveF1: none. FastF1 path: **F1 TV login** | **No.** LiveF1's client connects to the *legacy* endpoint (`livef1/utils/constants.py:4` `SIGNALR_ENDPOINT = "/signalr/"`, headers only `User-agent: BestHTTP`), which boxbox reports now answers 401. The FastF1 path uses `access_token_factory=get_auth_token` (`fastf1/livetiming/client.py:170`), which prints *"This feature requires an active F1TV Access/Pro/Premium subscription"* (`fastf1/internals/f1auth.py:146`; FastF1 docs `livetiming.rst:12`). The project's live test `scripts/live_smoke.py` is marked "only meaningful during a race weekend" and no live result is recorded; its map/telemetry demos use FastF1 historical data. |

Conclusion: neither project has a method to receive live GPS positions without
an F1 TV entitlement. boxbox uses the identical anonymous SignalR Core
connection as this dashboard and documents that `Position.z`/`CarData.z` stay
silent. FastF1 states that data *after* the session needs no authentication –
which is what replay mode uses.

What this dashboard does about it (all real data, nothing simulated):

1. Always subscribes to `Position.z`/`CarData.z` on the anonymous socket – if F1
   ever sends them anonymously, they are used at once (primary source).
2. `archive_follow`: while a session runs and the socket sends no positions for
   30 s, it polls `https://livetiming.formula1.com/static/<SessionInfo.Path>Position.z.jsonStream`
   (and `CarData.z`) with HTTP Range requests and feeds each new real sample.
   Whether F1 publishes these files *during* a session (`ArchiveStatus:
   "Generating"`) and with what delay is not documented and could not be
   tested from the development environment – the log says either
   *"Archive stream Position.z is readable during the session … using it"* or
   *"not (yet) published (HTTP 403/404)"*. When used, the map legend shows
   "Positions: public F1 archive stream · N s behind".
3. Optional F1 TV token as fallback for real-time positions.
4. Otherwise: map with real geometry, no cars, clear N/A message; telemetry N/A.

Verify it on your own connection during any session:

```bash
python tools/probe_feed.py --seconds 120            # anonymous: counts per topic + archive check
F1TV_TOKEN=... python tools/probe_feed.py --token    # comparison with a token
```

## 8. Track geometry

Real circuit geometry comes from the MultiViewer circuit API (the dataset
FastF1 uses), in the same coordinate system as `Position.z`, including rotation,
corner numbers and marshal sectors. It is chosen by `SessionInfo.Meeting.Circuit.Key`
(every circuit on the calendar, no hard-coding), downloaded on first use and
cached in `data/tracks/`. When there is no outline for the current season yet (early in a
weekend), the circuit's outline of the previous / the season before is used (log: *No
MultiViewer outline of circuit X for Y - using its Z outline*). Pre-download a season
(recommended before a weekend):

```bash
python tools/fetch_tracks.py --year 2026 --pitlane
```

When MultiViewer is not reachable, the outline comes from a **known layout fitted onto the
real car positions** (`server/track_match.py`): the circuit's outline from the bundled
[bacinger/f1-circuits](https://github.com/bacinger/f1-circuits) collection
(`server/reference_tracks/`, MIT) is rotated / mirrored / scaled / shifted (coarse search + ICP)
onto the positions of the cars on track, and used only if it fits all around the lap
(median ≤ 7 m, 90 % ≤ 16 m, ≥ 85 % of the outline covered, scale 0.85–1.18; on a real lap of
the 2025 Azerbaijan GP: median 2.9 m, scale 1.001). A layout that does not fit (another
configuration) or too little data → not used. Only if no known layout fits, the outline is
learned from one lap of positions – and only kept when it is a closed loop of 2.5–8 km
without gaps (a hole in the position stream used to draw part of the track as missing / a
straight line). The map legend says which source is shown.

**Circuit identity comes from the session metadata only:** `SessionInfo.Meeting.Circuit.Key`
(F1's own circuit id, e.g. Baku 144, Sakhir 63, Suzuka 46) for the cache and MultiViewer, and
the circuit short name / location / meeting name for the bundled known layout. The circuit is
**never guessed from the shape of the car positions** – when the metadata names no known
layout, nothing is substituted (choose it yourself, below).

**Every drawn outline is checked against the cars.** Once ~1500 positions of cars on track
are in, every minute: if more than 5 % of them are > 40 m away from the drawn track (part of it
missing, or another layout – also when it came from MultiViewer), the outline is replaced by the
known layout *of this circuit* (from its name) when that fits (legend: "auto-corrected"). On a
real Baku lap: a half outline is detected (33 % of positions off) and replaced.

**Choose the circuit yourself:** **CIRCUIT…** in the map legend → pick one of the 40
known layouts (or *Automatic*). It is saved per circuit (`data/tracks/track_choice.json`) and wins
over every other source; it is fitted onto the car positions (without positions yet it is shown
as it is, north up, without cars). API: `GET /api/track/layouts`, `POST /api/track/choice`
`{"layout": "bh-2002"}` (or `"auto"`).

**Track map wrong?** Click **MAP WRONG?** in the map legend (or press **W**), then once more
within 8 s. That deletes the cached outline of *this* circuit (`mv_*`, `ref_*`, `learned_*`
in `data/tracks/`), never uses the source that was shown for it again
(`data/tracks/track_reports.json`; when all sources were rejected the list starts over) and
builds the outline again. **The pit lane – its cache and its learning – is not touched.**

The pit lane is not part of that dataset (no source publishes it as a line). It is static
circuit geometry, so it is **loaded as soon as the circuit is known – it never waits for a pit
stop of the session you watch** (`server/pitlane_seed.py`, `server/pitlane.py`), cache-first:

1. session opened → circuit (`circuit_key`, never the GP name, the media id or the session)
   → `data/tracks/pitlane_geometry_<circuit_key>.json`;
2. a **verified** pit lane in the cache (this season, or an earlier one not superseded by a
   different layout) → drawn at once, nothing is recomputed or downloaded;
3. otherwise it is **built from the F1 archive** right away: the official live-timing archive
   (`livetiming.formula1.com/static/<year>/Index.json`) is searched for finished sessions with
   the same `Meeting.Circuit.Key` (this season and the two before; races first, then sprints,
   qualifying, practice; newest first; the running session excluded). The `Position.z`,
   `TimingDataF1` and `PitLaneTimeCollection` streams of the best one (a race: 20–60 normal pit
   stops of many drivers) go through the reconstruction below; up to 3 sessions are tried.
   Legend: `Pit lane loading…` → `Pit lane ✓`. Why: in practice almost every pit visit is a
   garage stay (> 120 s → rejected below), so a pit lane learned only from the current session
   was usually never drawn in FP – and is never drawn from one driver;
4. nothing reliable (archive unreachable, no finished session here, too few agreeing passes)
   → **`Pit lane geometry unavailable`** in the legend / log / diagnostics, with the reason – no
   lane is invented. Then (only then) the current session's own complete passes are collected
   as before (≥ 3 agreeing passes for HIGH);
5. a **known (verified) pit lane is never replaced by live samples**: each live complete pass
   is only compared with it (≤ 8 m median deviation = agrees; log *Live pit pass … matches* /
   *does NOT match*; diagnostics `validation: n of m live passes agree`).

Reconstruction of passes (archive or live):

1. **complete pit-lane passes** are collected – pit entry line (`InPit` rising) →
   pit lane → pit exit line (`InPit` falling / `PitOut`), cross-checked with the official
   pit-lane time (`PitLaneTimeCollection`; in the 2026 feed the `InPit` edge is often
   missing, then entry = exit − official time). A stop alone, a few points at the entry,
   a garage visit (> 120 s or a path that turns back), a red flag, holes in the position
   data or positions that stay on the main track are **rejected** (reason in the debug view).
   The pass is extended back/forward to where the car leaves/rejoins the racing line, so the
   map shows where the lane branches off.
4. geometry: F1 positions are already local metric X/Y (decimetres, the same system as the
   track outline – not GPS lat/lon), so no projection is needed. Filter spikes → resample (5 m) →
   align all passes on a reference (normals) → **trimmed median** centerline → drop outlier
   passes → Gaussian smoothing (σ 10 m, ends fixed) → Catmull-Rom (2.5 m).
5. confidence: **HIGH** (≥ 3 passes that agree within 2.5 m, entry and exit on the track) →
   stored as *verified*; **MEDIUM** (1–2 good passes) → drawn and stored as *provisional*,
   improved by later passes; **LOW** → not drawn, not stored, learning continues.
6. seasons: a new season's passes are compared with the cache. Same lane → the season is
   added. A different lane (layout change) → stored as a new variant; the old one is not
   overwritten and stays valid for its own seasons. `algo_version` in the file invalidates
   caches of an older reconstruction.

VOD: the whole session is searched once when its data is loaded (in the background).
Live / replay: the archive seed runs at circuit identification; live passes then only validate
(or, when no lane is available, are collected until one is verified). TEST mode uses its
synthetic test pit lane (no download).

**Map validation** (log at load, when the pit lane arrives and every 5 min; also in
`/api/diagnostics?format=text`):

```
Track: Azerbaijan Grand Prix - Baku (circuit_key 144, season 2026, layout az-2016)
Track geometry: OK - MultiViewer 2026 (202 points)
Pit lane geometry: OK - F1 archive: Azerbaijan Grand Prix Race 2025 - HIGH, 4 passes
Position mapping: OK - median 3.1 m from the track
Drivers mapped: 20/20 with a fresh position (20 drivers)
Safety car position: not available - not provided by the feed (Position.z carries cars only)
```

All of track, pit lane, cars and a safety car marker use the **same transform**: F1 `Position.z`
X/Y (decimetres, local circuit coordinates) → rotated by the circuit's MultiViewer rotation →
scaled/centred into the map canvas. A car > 50 m from both track and pit lane is listed by
number (a wrong map is visible, not hidden). The safety car is drawn only if its position is
actually in `Position.z` under a configured key (`[f1_tv] safety_car_position_keys`); otherwise
the SC *status* is shown with "position not provided by the feed".

**Messages never cover the map:** the map panel is split into the map area (canvas only) and a
footer below it – line 1: important race-control status (red flag, SC/VSC, yellow sectors),
line 2: compact legend (`Track ✓ source`, `Pit lane ✓ / loading… / ✗ unavailable`, MAP WRONG?,
CIRCUIT…). Details (sources, reconstruction) are in tooltips, the **G** debug view and the
diagnostics, not on the map.

Map: the pit lane is drawn as a smaller road that branches off and rejoins the track, with
the entry/exit lines. Where it runs so close to the main straight that the two roads would
merge on the small map, it is moved a few pixels **to its own side** (capped, fading out
towards entry/exit – the shape and position stay the real ones). Cars in the pit lane are
drawn on it (not on the main straight). Legend: `Pit lane: cached (HIGH · 5 passes)`,
`learning… 2 passes`, `reconstructed (MEDIUM · 1 pass · provisional)`.
**G** = show pit lane reconstruction debug: circuit + season, cache status (verified /
provisional, variant, seasons, passes), what happened now, confidence and spread, entry / exit
coordinates, the pit entry / exit lines, the cars drawn in the pit lane vs the timing `InPit`
flag, rejected passes with the reason, notes (position jumps bridged, glitches dropped), and on
the map the raw positions of every pass (cyan used, red rejected), the new (yellow) and the
cached (magenta) centerline and the entry / exit rings.

Real data (2025 Azerbaijan GP, `tests/fixtures/openf1_baku2025_pit.txt`, tested): the pit
lane runs 11-12 m beside the racing line (≈ 4 px on the map, so it is drawn apart); F1's
map-matched positions jump 24 m sideways at the pit entry within 0.22 s and once replay 23 m
backwards - both are handled; every in-pit position of a real pass is drawn on the pit lane
(≤ 1 px), cars on the main straight never are.
`python tools/fetch_tracks.py --year 2026 --pitlane` fills the cache for a whole season.

## 8a. Weather report popup

During a race (and sprint), after every 15 completed laps (`[weather] every_laps`) a **WEATHER REPORT**
panel appears for 15 s (`display_seconds`; click to close). **U** / remote `WEATHER_REPORT` / the
phone remote shows it at any time. It uses the standard overlay box of the dashboard (centred; in
RACE VIEW over the leaderboard, never under the VOYO window).

* **CURRENT** - official F1 `WeatherData`: air / track temperature (with the trend of the last
  ~15 min of the session), humidity, wind (m/s from F1, shown in km/h) and direction, rainfall.
* **FORECAST** - the circuit's coordinates (bundled `f1-locations.json`) to **Open-Meteo**
  (ECMWF IFS, GFS and ICON models - each one a source) and **MET Norway**, no keys. Each source
  gives the first rain, its end and its peak rate; agreeing sources -> HIGH / MEDIUM confidence,
  disagreeing ones -> "Rain possible laps 18-25" with LOW confidence. Rates -> NONE / DRIZZLE /
  LIGHT / MEDIUM / HEAVY with the documented `[weather.thresholds]` (0.1 / 0.5 / 2.5 / 7.6 mm/h).
  Times become laps only as an estimate (`~lap 22`) from the leader's recent lap times - never
  presented as an F1 prediction; the forecast is hourly, so it is never more precise than that.
  Fetched only for a report, cached 10 min. A recording (replay / VOD) gets no forecast.
* **RACE IMPACT** - fixed rules on that data (slippery track / intermediate conditions possible /
  drying line / track temperature up or down / no significant change). No LLM: the project has
  none, and the text never contains a value that is not in the data.
* Missing data stays **N/A** (never 0 °C / 0 % / NONE); without a forecast the current conditions
  are still shown ("Weather forecast unavailable"). No popup when there is no weather data at all.
* **RADAR** (`server/radar.py`, `[weather.radar]`): a map centred on the circuit (~100 km radius),
  the circuit marked above the precipitation (ring + the real outline from the bundled layout,
  magnified so it stays visible + name), range rings, north, a movement arrow, PAST -> NOW ->
  FORECAST animation. Imagery: **RainViewer** radar tiles (observed radar composite, 10-min frames,
  nowcast frames when RainViewer lists them). Numbers - intensity at the circuit, movement
  (APPROACHING / MOVING AWAY / OVER CIRCUIT / FORMING / DISSIPATING / STATIONARY) and **RAIN ETA**
  (`~8 MIN / LAP 34`, `NOW`, `UNCERTAIN`, `RAIN POSSIBLE`): the **Open-Meteo 15-min precipitation**
  grid (9 x 9 points; the same thresholds). ETA = mean of (a) the first forecast step with >= LIGHT
  at the circuit and (b) the nearest rain's distance / approach speed when both agree within
  20 min, otherwise the one there is (lower confidence) or UNCERTAIN; laps from the leader's pace.
  Fetched only for a report, kept 3 min; a failed refresh keeps data < 15 min old, labelled
  "updated N min ago"; older -> RADAR UNAVAILABLE (the rest of the report stays). No radar for a
  recording.
* TEST mode: no automatic popup (`test_auto = true` enables it); radar test states
  `WEATHER_REPORT:norain|approaching|over|away|heavyrain|radaroff`; simulated forecasts with
  `WEATHER_REPORT:dry|drizzle|light|medium|heavy|stopping|noforecast|disagree` (phone remote:
  "TEST mode: simulated weather"). Log lines: `[WEATHER] Current: …`, `[WEATHER] Forecast: …`,
  `[WEATHER] Report generated for lap 15`.

## 8b. Motion and race-event animations

Short, state-driven animations (CSS transform / opacity, GPU-friendly; the dashboard stays fully
interactive): micro 150 ms, normal 300 ms, events 600-900 ms, a flag takeover up to 1.1 s.

* **Leaderboard:** rows glide to their new place (`[dashboard] reorder_ms`). A gain in a race =
  overtake: green flash, an accent line sweeping under the row and a `▲1` chip for 0.9 s; the
  passed car gets a faint red `▼1` (0.7 s); in practice / qualifying gains are blue. Several changes
  in one update move together; a bulk re-order (seek, reconnect, new session: > 40 % of the rows) only
  moves, without emphasis. The order is always exactly the server's.
* **Other cells:** pit IN / OUT pops (0.7 s), a new compound fades in (0.6 s), a new overall fastest
  lap glows purple (1.1 s), a new personal-best / overall-best sector pulses (0.65 s).
* **Mini-map flag layers** (under the track, pit lane, cars and SC marker; a flag pill in the map's
  title row): RED - deep red tint + red glow pulsing every 1.5 s (the red pulse also on race control);
  SAFETY CAR - gold tint + glow every 1.8 s; VSC - faint tint, thin glow every 2.6 s; DOUBLE YELLOW -
  hazard strip at the top + soft border pulse; YELLOW - thin strip; CHEQUERED - a black / white chequer
  fades in under the map (1.1 s). Every change gets one short takeover (stronger for RED). A RED FLAG /
  SAFETY CAR DEPLOYED / CHEQUERED FLAG race-control message (once per message) triggers it too.
* **Safety car marker:** a soft halo - only on a real SC position (never invented).
* **Reduced motion:** the system setting `prefers-reduced-motion` or `[dashboard] animations =
  "reduced"`: no travel and no pulses, short fades only; `"off"`: no animation at all.
* **TEST mode:** `SIM_EVENT:<green|yellow|dy|vsc|sc|red|chequered|overtake|pit|fastest>` (phone
  remote: "TEST mode: simulate race event") makes the simulator send the real messages; the
  simulator also cycles yellow / VSC / double yellow / SC / red flag every 620 s on its own.

## 9. Remote control and keyboard

Everything goes through one abstraction: *input → key name → whitelisted
command → shared UI state*, so keyboard, IR bridges, HTTP and WebSocket clients
all behave the same and all screens stay in sync. Mapping: `[remote.keymap]`.

| Key (keyboard / remote) | Command |
|---|---|
| ↑ / ↓ | MOVE_UP / MOVE_DOWN (select driver in the leaderboard) |
| ← / → | CHANGE_VIEW prev / next |
| Enter / OK | OPEN_TELEMETRY |
| Esc / Backspace / BACK | CLOSE_PANEL |
| I / INFO | OPEN_RACE_CONTROL |
| Space / `T` | CYCLE_TV_MODE (§9a) |
| PLAY-PAUSE / `P` | VIDEO_PLAY_PAUSE (§9a) |
| `A` | TOGGLE_AUTO_CYCLE (rotate views every `auto_cycle_seconds`) |
| `Y` / SYNC button | SYNC menu: countdown, exact time, automatic, estimate; qualifying / practice: session clock, phase markers (§9b) |
| `L` / BLUE | SYNC_START – lights out / session clock starts on the video (§9b) |
| `S` / RED | SYNC_MARK – the selected car (or the leader) crosses the line on the video (§9b) |
| `C` · `K` · `X` | lap on TV matches (confirm) · pin the shown time · clear the sync |
| `O` · `N` | after a *possible sync drift* warning: keep the old sync · use the new anchor |
| `+` (`=`) / CH+ · `−` / CH− | SYNC_PLUS / SYNC_MINUS – ±0.25 s (`adjustment_step`) |
| `R` / GREEN | SYNC_RESYNC – force resync |
| `D` / YELLOW | SYNC menu (same as `Y`) |
| 1 2 3 4 5 | Overview / Telemetry / Strategy / Race Control / Weather |
| H | help overlay |
| mouse click on a row | SELECT_DRIVER |

### Phone remote (`/remote`)

The phone needs no app and nothing from this project: open **`http://<PC-IP>:8080/remote`** in
Safari / Chrome (same Wi-Fi). The address is printed at start (`PHONE REMOTE: http://…/remote`), in
`/api/diagnostics?format=text`, in `GET /api/remote/info`, and as a **QR code at the top of the HELP
panel** (key **H** / HELP) - scan it with the phone camera. With Tailscale running, the
`http://100.x.y.z:8080/remote` address is listed too (detected, nothing configured).

* It is another client of the dashboard's own WebSocket (`/ws?client=remote`): every button sends the
  same command as the IR remote (D-pad keys through the same keymaps; VIEW = `CHANGE_VIEW`, TV LAYOUT =
  `SET_TV_MODE`, WEATHER REPORT = `WEATHER_REPORT`, MODE = the dashboard's mode selector, VOYO SYNC =
  `SYNC_MINUS` / `SYNC_MARK` / `SYNC_PLUS` / `SYNC_START` / `SYNC_RESYNC` / `SYNC_MENU`, plus the
  SYNC menu's actions under ADVANCED SYNC, video play / mute, driver selection). Several phones can
  connect; the desktop and all phones show the same state - the server's.
* The phone receives only the remote state: view, TV layout, mode, session / lap / flag / pit exit,
  the selected driver and the order, sync status (≤ 1 per second). No positions, telemetry or
  credentials.
* Connection dot CONNECTED / DISCONNECTED, automatic reconnect; while disconnected the buttons are
  dimmed and a tap says "not sent". SYNC ± and ▲ ▼ repeat while held. Portrait and landscape.
* `[remote] token`: when set, the phone needs `?token=…` (the QR code contains it).
* Network: `[server] host = "0.0.0.0"` (default) listens on the LAN. **Windows Firewall:** when
  Windows asks on the first start, allow *Python* on **Private networks**; or add the rule
  `New-NetFirewallRule -DisplayName "F1 Dashboard" -Direction Inbound -Protocol TCP -LocalPort 8080 -Profile Private -Action Allow`
  (PowerShell as administrator). Nothing else is opened.
* TEST mode: the TEST MODE TOOLS section sends simulated weather reports / race events.

HTTP API (for any other remote):

```bash
curl "http://server:8080/api/remote/key?key=KEY_UP"
curl -X POST http://server:8080/api/remote/key -d '{"key":"KEY_OK"}'
curl -X POST http://server:8080/api/remote/command -d '{"command":"CHANGE_VIEW","arg":"strategy"}'
```

Set `[remote] token` to require `X-Remote-Token` / `?token=`.

**WD TV Live remote:** see `server/wdtv/README.md`. Short version: WDLXTV only offers
key *injection* (`/tmp/ir_injection`); reading the WD TV's own IR receiver is
not documented, so `server/wdtv/probe_ir.sh` tests your box and the bridge is only
used if the probe finds a readable key source. The remote itself is a standard
NEC remote and works reliably with any Linux IR receiver + `ir-keytable` +
`server/bridge/evdev_bridge.py` (keymap in `server/bridge/keymaps/wdtv_live.toml`).

Exact test whether the remote generates Linux input events (on a Linux box with an IR receiver):

```bash
sudo ir-keytable                         # receiver present?  -> /sys/class/rc/rc0 (/dev/input/eventN)
sudo ir-keytable -c -p nec -t            # press buttons      -> "scancode = 0x8479.." lines
sudo ir-keytable -c -p nec -w server/bridge/keymaps/wdtv_live.toml
sudo evtest /dev/input/eventN            # press UP           -> "EV_KEY ... (KEY_UP), value 1"
```

and on the WD TV itself: `sh /tmp/probe_ir.sh` (see `server/wdtv/README.md`).

## 9a. VOYO video on the same screen (TV modes)

Three TV modes (Space / `T` / remote TV-mode key cycles them):

| Mode | Screen |
|---|---|
| `FULL_DASHBOARD` | the normal dashboard, no video |
| `RACE_VIEW` (default when video is available) | video 1280×720 top-left · compact leaderboard right · map, telemetry, race control and session/flag/weather panel along the bottom |
| `VIDEO_FOCUS` | video 1920×990 · 90 px bar with lap, flag status, leader, selected driver, clock |

Views 1–5 keep working (in RACE_VIEW strategy / race control / weather replace
map + telemetry). Without a configured or reachable video source every mode
falls back to `FULL_DASHBOARD` automatically; the F1 data pipeline never
depends on the video layer.

### What VOYO allows – what was checked (September 2026)

* **Terms of use** ("Splošni pogoji uporabe storitve VOYO", valid from 1. 9. 2026):
  personal, non-commercial use within one household (III.5); up to 5 devices,
  **2 simultaneous streams** (III.5/III.6); watching "v izbranem brskalniku na
  Voyo.si", in the VOYO apps and on supported smart TVs (III.6); content must not
  be copied, reproduced, publicly communicated (III.1), stored or passed on
  (III.18).
* **No official embed / iframe player, no player API, no public HLS/DASH URL**:
  none is documented on voyo.si, in the FAQ or the terms, and no public
  integration exists. A stream URL extracted from the web player would be
  neither documented nor allowed (III.1, III.18) and is not used.
* **Framing headers** could not be read from the development environment
  (voyo.si blocked there). The server therefore checks them on your network:
  in `mode = "embed"` it reads `X-Frame-Options` / `CSP frame-ancestors` and only
  embeds if framing is permitted – it never bypasses them. Even if framing were
  permitted, logging in inside an iframe needs third-party cookies, so `embed`
  is not expected to work for VOYO.

**Result: direct embedding is not possible in an official way.** What works
and is fully allowed is VOYO's *own* website player in its own browser window –
placed exactly over the dashboard's video slot:

### How to use it (window mode – recommended)

**Windows, one click:** double-click **`launch.bat`** (or `launch.bat test` /
`launch.bat replay`). It starts the dashboard server in the background, the
dashboard, the VOYO window and the TV agent. **Closing that console window –
or the dashboard window, or the VOYO window – closes everything together**
(both browser windows and the server it started) and the taskbar comes back.
The server log is in `data\server.log`. Details of the individual parts:


1. On the PC connected to the TV, install Microsoft Edge or Google Chrome
   (Widevine DRM; Chromium on Linux/Raspberry Pi normally cannot play VOYO).
2. In `config/config.toml`: `[voyo] enabled = true` (mode `window` is the default).
3. Start the dashboard server, then on the TV PC:

   ```bash
   python tools/tv_launcher.py --server http://<server-ip>:8080            # RACE_VIEW geometry
   python tools/tv_launcher.py --server http://<server-ip>:8080 --layout focus
   ```

   It opens the dashboard full screen and VOYO (`--app` window, own browser
   profile in `data/browser-profiles/voyo`), then **keeps running as the TV
   agent** (leave the console window open, Ctrl+C stops it):
   * the VOYO window is kept **always on top** of the dashboard while a video TV
     mode is shown – you can click the dashboard to give it the keyboard
     without hiding the video;
   * it **follows the TV mode**: RACE_VIEW → 1280×720 slot, VIDEO_FOCUS →
     1920×990, FULL_DASHBOARD → VOYO sent **behind** the dashboard (never
     minimized: a minimized window is a hidden page, and the browser pauses
     muted / video-only playback there – some players pause themselves), back
     on top on the next mode change. Positions are computed for your screen
     resolution and Windows scaling; the window's title bar is pushed just above
     the slot (`--titlebar 32`, use a different value if a strip of it remains
     visible or the video is cut, `0` to keep it);
   * while the **VOYO window has the keyboard focus**, the keys
     `T, H, I, A, 1–5, ↑, ↓, S, R, D, =, −` are forwarded to the dashboard (e.g.
     T switches the TV mode, S sets a sync mark). ←/→, Space, F, M etc. stay with VOYO's player. The keys are only
     captured while VOYO is in the foreground – every other program keeps them –
     and **not while a text field of the VOYO page has the focus** (login, PIN,
     search: the keys go to VOYO). Change the list with `--hotkeys`.
   * the **Windows taskbar is hidden** while the agent runs (Windows would show
     it whenever the VOYO window is active) and shown again when the agent
     stops (Ctrl+C or closing its console). `--keep-taskbar` disables this;
     `python tools/tv_launcher.py --restore-taskbar` brings it back if the agent
     was killed.
   * it reads VOYO's **playback clock** for the sync (§9b); `--no-clock` disables it.
     The console also shows when the VOYO page is hidden or VOYO's player reports
     its own media error (login / subscription / region / browser DRM support –
     those are VOYO's checks, the dashboard never touches them).
   * the server's VOYO reachability check is only informational in window mode:
     a request from the server (no browser login, no cookies) may be refused by
     the site while the VOYO window plays normally, so it never hides the window.
   The first time, log in to VOYO in its window and open the F1 live stream; the
   login is kept. If the windows are already open, run `python tools/tv_launcher.py --attach`.
4. Play/pause, volume and seeking are done in VOYO's player itself.
5. You can also control everything with the IR remote, or from your phone:
   **`http://<server-ip>:8080/remote`**.

Windows: full support. Linux/X11: placement and always-on-top via `wmctrl`,
no key forwarding. macOS: launcher only.

Alternative: in the VOYO page use the browser's **Picture-in-Picture** (Edge/
Chrome media controls, if VOYO's player permits it) and drag the PiP window over
the slot. The dashboard cannot start PiP for another site's player.

**Mirroring to a TV (AirParrot, Miracast, OBS …) shows the VOYO video black** while the
PC monitor shows it and the sound plays: Chromium hands hardware-decoded video to a GPU
overlay plane that the monitor scans out but desktop capture does not contain (a short
ALT+TAB makes it appear because the video is then composited normally). Modes:
`launch.bat capture1` (no video overlays; GPU and hardware decoding stay), if still black
`capture2` (+ no hardware video decode), last resort `capture3` (no GPU). Close the VOYO
window completely first – the flags apply on a fresh start.
**Default: `[voyo] capture_compat = "no-gpu"` (= `capture3`)** in `config/config.toml`, so a
plain `launch.bat` already starts the VOYO window with `--disable-gpu`: on the reference
setup AirParrot showed the video only in this mode (`capture1` / `capture2` stayed black).
`launch.bat capture1` / `capture2` still select the other modes for one start,
`launch.bat capture0` the browser default; change the config value to switch permanently. Only the VOYO window's rendering
changes; VOYO's player, login and DRM are untouched. If the video stays black even with
`capture3`, the stream is protected from capture by design and no setting here changes that.

Note: the VOYO window uses one of your 2 simultaneous VOYO streams.

### Other video modes (only for officially permitted sources)

* `mode = "embed"` – iframe of `voyo.url`, used only when the automatic header
  check allows framing.
* `mode = "hls"` + `hls_url` – `<video>` element (native HLS or hls.js from
  jsDelivr) with play/pause, mute, volume, seek (only if the stream is seekable),
  fullscreen, loading state and **VIDEO OFFLINE · reconnecting** with back-off.
  Only for a stream URL a provider officially offers for external players –
  VOYO offers none. Nothing is recorded or re-streamed.

### Remote in the TV modes (`[remote.keymap_video]`, `[remote.keymap_video_focus]`)

| Key | FULL_DASHBOARD | RACE_VIEW / VIDEO_FOCUS | video focused |
|---|---|---|---|
| ↑ / ↓ | select driver | select driver | volume (hls) |
| ← / → | previous / next view | previous / next view | seek ±10 s (hls, if seekable) |
| OK | telemetry | **focus video** | play/pause (hls) |
| BACK | overview | leave VIDEO_FOCUS → overview | unfocus video |
| PLAY/PAUSE, `P` | – | video play/pause (hls) | play/pause |
| Space, `T` | cycle TV mode | cycle TV mode | cycle TV mode |
| INFO, `I` | race control | race control | race control |
| `A` | auto-rotate views | | |
| `V` / `M` / `F` | focus video / mute / fullscreen | | |

In window/embed mode the video keys show a hint that the control is inside
VOYO's own player – the dashboard cannot and does not remote-control another
site's player.

## 9b. VOYO ↔ F1 sync (video clock, timeline, calibration)

Goal: the dashboard shows exactly the F1 state of the moment that is on the
VOYO picture – leaderboard, gaps, tyres, pit, penalties, investigations,
flags / SC / VSC, race control, weather, session clock, car positions and
telemetry all together, not "live data minus a guess".

### Findings: which times exist (checked in this project's feed client and the bundled recording)

| Clock | Where it comes from | Used for |
|---|---|---|
| **A · F1 event time** | `Position.z` → every sample's own `Timestamp`; `CarData.z` → every sample's `Utc`; every SignalR `feed` message → its 3rd argument (set by F1's server when the update was generated); archive/replay → `.jsonStream` line offset anchored on `Heartbeat.Utc`. `RaceControlMessages[].Utc` (1 s resolution), `ExtrapolatedClock.Utc`, `Heartbeat.Utc` are further event times *inside* the data. | **the canonical timeline** |
| **B · receive time** | wall clock when the server got the message | diagnostics only (`F1 RECEIVE LATENCY` = median of B − A) |
| **C · VOYO playback time** | `HTMLVideoElement.currentTime` of VOYO's player | where the video is |

Not event-timed: the Subscribe **snapshot** (no timestamp – stamped
"1.5 s before it arrived"), and the lap/sector *values* themselves (`TimingData`
carries no per-field timestamp; the message timestamp is its event time).
Session time is shown from `ExtrapolatedClock` (`Remaining` at `Utc`),
evaluated at the target time.

### Timeline: "the complete F1 state at time X" (`server/timeline.py`)

Every message is split into events on clock A (a `Position.z` message becomes
one event per sample) and kept with both A and B. The shown state is the
state **at the target time X**: moving forward applies the next events, moving
back (video seek, SYNC −, resync) restores a checkpoint (every ≤ 5 s) and
replays up to X. `buffer_seconds = 120` of history stays available behind the
shown state; while the video is paused newer data is kept as well (up to
`max_hold_seconds`). Events that arrive after their time has already been
shown (e.g. archive positions) are applied immediately, never dropped. Car
positions are streamed 2.5 s ahead of X (`position_lookahead_ms`, only for
interpolation) so the map is drawn at exactly X instead of lagging behind it.

### Is `currentTime` F1 time? – no, and nothing on the page says which F1 time it is

`currentTime` is the position in the VOYO video (live measured 2534.41 →
2541.47 s, 1 s per second; it follows pause/seek). Checked and **not usable as
a UTC clock**: the VOD HLS playlist has no `EXT-X-PROGRAM-DATE-TIME` /
`EXT-X-DATERANGE`; `mediaId`, `title`, `length`, `startAt` (a restore position)
and the GraphQL `videoUrlV2.info` field (empty, `infoCode 0`) carry no time;
Chrome has no `getStartDate()` for MSE streams; the picture cannot be read
(DRM – and reading it would be working around the protection). Therefore

    absoluteF1Time = anchorF1Time + (currentTime − anchorVideoTime) = currentTime + offset

and every offset comes from an **anchor**, i.e. one moment of the video whose
F1 time is known. Verified with OpenF1 (2026 Japanese GP race): OpenF1
`session_key` 11253 = the live-timing key; `laps.date_start` of #12 lap 6 =
05:22:02.092 = the live-timing message that completed lap 5 (same clock, ms);
`SESSION STARTED` 05:14:02.078 = lights out, **14 min after the scheduled
`date_start` 05:00** – the schedule is not the start.

**Delayed starts.** The F1 race timeline is anchored to the **actual start**
(the SessionStatus *Started* before the first completed lap – an aborted start
or a restart after a red flag is not mistaken for it), never to the scheduled
time. The sync tracks the start state: **PRE-START** → **DELAYED** (scheduled
time + `race_start_grace_seconds` passed without a start, or race control
announced a delay / suspended start procedure / "FORMATION LAP WILL START AT
14:10") → **STARTED** → **RUNNING**. While DELAYED the scheduled start is not
used for anything that anchors the race clock (VOYO Countdown in Auto, `L`).
Two delays are shown separately in the SYNC menu and on the phone:
**F1 START DELAY** = actual − scheduled start (the event was late) and
**STREAM DELAY** = how far the video is behind the F1 events (the broadcast).
Example: scheduled 15:00, lights out 15:04, the stream shows it at 15:08 →
F1 start delay +4:00, stream delay +4:00 (not 8 minutes). The server log has
`[SYNC]` lines for every step (stream start marked, VOYO position, actual /
scheduled start, detected race delay, calculated stream delay, confidence,
"scheduled start IGNORED as the race-time anchor").

### SYNC menu (button `SYNC` in the top bar, key `Y`, phone remote)

Shows the session (detected from the VOYO title via OpenF1), the video
position and four methods:

| Method | What you do | Result |
|---|---|---|
| **Mark Stream Start** (also on the phone remote: MARK STREAM START / RESET STREAM START) | position VOYO at the **absolute beginning of the stream (0:00)** and press MARK STREAM START | the **origin of this VOYO broadcast** – not the race start, lights out or the schedule. No F1 topic contains the broadcast origin, so its F1 time comes from an F1 reference of the same video (best: `L` at lights out; also countdown / `S` / exact time) and is **saved per session + video**: reopening that VOD restores it (HIGH when it came from lights out) even without other anchors. LIVE: estimated from the moment 0:00 airs (F1 receive clock − stream delay estimate, LOW) until lights out confirms it. A recording never uses today's clock. Shows where the scheduled / actual start are in the stream. RESET removes it for this session + video. |
| **VOYO Countdown** (recommended before a session) | pause VOYO on the countdown to the start (or press *Capture* when you read it) and enter `23:47`, `00:23:47` or `23m 47s`; *Counts to*: Auto / Scheduled / Announced new start / Actual start | F1 time = start − countdown at that video moment. **MEDIUM**, ±2 s (whole seconds, the countdown graphic may lag the world feed). Auto = the scheduled `date_start`, **except when the start was delayed** (scheduled time passed without a start, or race control announced a delay / new start time): then it is refused and you choose – a delayed start is never counted against the schedule. Also gives the "VOYO broadcast start". |
| **Manual Exact Time** | enter the time the video shows: **Slovenia** or **track** time, **24-hour** (`14:48:32`, `14.48.32`) or **12-hour** (`2:48:32` + AM/PM); a preview shows UTC / Slovenia / track before you apply | `anchorVideoTime = currentTime`, `anchorF1Time = your value` – **MANUAL** |
| **Automatic** | nothing | uses only a sync saved for *this video and this session*. Otherwise it explains why it cannot sync (above) and changes nothing – never silently. It suggests a learned lead time for the estimate. |
| **Session Start Estimate** | enter how long the video runs before the scheduled start (prefilled with a learned value, never assumed) | **LOW / ESTIMATED**, no exact time is shown anywhere |

**Lights out (`L` / BLUE / phone START / SYNC menu LIGHTS OUT)** is matched to the
actual start event in the F1 data – never the schedule: `SessionData.StatusSeries`
(*SessionStatus Started* with its own millisecond Utc – the only exact source in the F1
archive, which has no `SessionStatus` topic), the live `SessionStatus`, OpenF1
`SESSION STARTED`, and as a ±1 s fallback the moment `ExtrapolatedClock` starts running.
OpenF1 and the archive are merged (one missing source never hides the other), and the small
archive topics are read before the big download, so `L` works on a recording before SYNC.
Alone it is **HIGH**; with other anchors it counts twice in the median. The menu shows
`LIGHTS OUT ✓ · F1 EVENT · VOYO · STREAM DELAY`, or why it is not available (not received
yet, not in the historical data, VOD timing unavailable, unknown / wrong session, unreadable
messages); the log has `[SYNC] Looking for Lights Out event …` lines.

**One canonical LIGHTS OUT** (server/lights_out.py) feeds `L`, Event Sync, the start state and
the stream start. Every report of a start keeps its source: LIVE = *F1 TV timing* (authenticated
socket) → *F1 SignalR* (the public feed; when the F1 TV socket has no start once it is due, or the
socket is down, a second minimal anonymous connection – the same client the dashboard used before
F1 TV – is opened just for it) ; VOD = *F1 archive* (stored official timing) → *OpenF1*; the session
clock (±1 s) only when nothing exact exists. The race start is the last *Started* before the first
completed lap; reports of one start are combined (agreeing sources → VERY HIGH, a disagreement is
reported and the source hierarchy decides). The scheduled start is metadata only – with no verified
start it says *Unavailable: No verified actual race-start timestamp found*. A resolved start is saved
per session key (reopening a VOD reuses it; never another session's). When the server runs from one
session into the next, the previous session's starts / laps are dropped.

**EVENT SYNC** (SYNC menu → *Event Sync*; also the phone's EVENT SYNC button) opens a sub-menu
with the real, timestamped F1 events of the session: LIGHTS OUT / SESSION START, every LAP n
(the leader crossing the line), PIT EXIT OPEN / CLOSED, TRACK YELLOW, SAFETY CAR DEPLOYED /
ENDING, VSC, RED FLAG, SESSION SUSPENDED, RESTART, TRACK CLEAR, CHEQUERED FLAG, qualifying /
practice phase markers – only what the session's data actually contains (no 5 / 3 / 1 minute or
formation lap: the F1 data has no such event). Move VOYO to the moment (pause on it) and SET;
repeat with more events. Each point is an anchor of the sync above: median, outlier rejection
(a wrong point is marked ⚠ OUTLIER and kept, not used), confidence by precision (millisecond
events vs race-control messages with whole-second times, marked ±1 s), saved per session +
video. Remove single points (✕) or CLEAR EVENT SYNC POINTS (the stream start stays).
↑ / ↓ select, OK / Enter SET, BACK / Backspace back – the TV remote and the phone d-pad work in
the sub-menu. Race incidents are listed only once the synchronised video reaches them (no
spoilers). Nothing of it appears outside the SYNC menu.

Precise **event anchors** without typing (any time, also to verify):
`L` / BLUE = lights out / the session clock starts; `S` / RED = the selected
car (or the leader) crosses the line. The press is matched automatically to
that event's millisecond timestamp in OpenF1 (fallback: the F1 archive / live
feed), 0.2 s reaction time is subtracted (0 if the video is paused on the
event). A line crossing cannot know *which* lap by itself: without a
countdown / exact time / `L` before it, it stays LOW until you press `C` (the
dashboard's lap matches the TV). `K` pins the shown time (MANUAL), `X` clears.

**Confidence** – descriptive, no invented score; the menu always shows method,
anchor(s), offset (as "video 0:00 = … UTC · session start at video …"),
confidence, the estimated error when it can be computed, and the reason:

* **HIGH** – ≥ 2 independent anchors agree within their errors (e.g. countdown + `L`, or several `S`)
* **MEDIUM** – one good anchor that cannot be verified independently
* **LOW** – estimated from the session start, anchors that disagree, or an unconfirmed lap
* **MANUAL** – time entered, pinned or adjusted (`+`/`−`) by you
* **UNSYNCED** – nothing reliable: no timestamp; the clock shows N/A, the board is dimmed and a banner says why

With several anchors the most precise class decides (median, MAD, outliers
rejected), the others verify it. The sync is saved per video (`mediaId`) and
session in `data/sync_calibration.json` and restored when you reopen the
dashboard; another video, Grand Prix or session never reuses it.

### SYNC HEALTH, drift detection, several anchors

Besides *how* the time was obtained (confidence) the menu shows **SYNC HEALTH** –
how good the shown time actually is:

| Health | Meaning | Error shown |
|---|---|---|
| **HIGH** | error ≤ 0.5 s | `Error ±0.13 s` when ≥ 2 independent event anchors were **measured** against each other (never below 0.1 s, the resolution of a key press / feed timestamp); `Estimated error ±0.2–0.5 s` (±0.1–0.3 s if the video was paused on the event) for one event anchor |
| **MEDIUM** | 0.5 – 2 s | e.g. `Estimated error ±1–2 s` for a countdown alone |
| **LOW** | > 2 s or unknown | estimate from the session start, unconfirmed lap, anchors that disagree – no number is invented |
| **MANUAL** | typed / pinned / adjusted by you | not measurable, not shown |
| **UNSYNCED** | no time can be determined | – |

**Drift detection** – every new anchor (L, S, countdown, exact time) is first
compared with the current sync (`shift = how far the shown F1 time would move`):

* ≤ `anchors_agree_seconds` (0.5 s): consistent → used at once, it strengthens confidence/health;
* ≤ `drift_warning_seconds` (2 s): **minor deviation** → used, but the display moves
  gradually (no jump of seconds) and the menu says the anchors do not match perfectly;
* \> 2 s: **POSSIBLE SYNC DRIFT** – shows current / new offset and the difference,
  the new anchor is **not used** until you choose *Keep old* (`O`) or *Use new* (`N`);
  a banner and `SYNC DRIFT?` in the top bar make sure you see it. *Use new*
  moves the old anchors to the history (kept for diagnostics).
* Exception: a time you **type** (countdown, exact time) is an explicit correction – it
  replaces a differing sync at once (old anchors go to the history, the menu says by how much).

A typed time more than 3 h away from the session is refused with the reason (usually the
wrong zone Slovenia/track or 12/24-hour) – there is no data at such a time. If the sync points
outside the loaded session data, the banner says **NO SESSION DATA AT THIS TIME**.

**Several anchors** – anchors from the same action / source are not
independent: S pressed twice for the same crossing is one event, all countdown
readings share the countdown graphic's bias, all typed times share your
reading. The offset is the **median** of the independent event anchors (per
event the median of its presses); with ≥ 3 of them anything further than
`outlier_seconds` from the median is an **outlier** and ignored – a single wrong
press cannot move the result (no averaging). Countdown / typed times only
verify it within their stated range. *View anchors* lists every anchor with
its offset (`OK`, `OUTLIER`, `UNCONFIRMED`, and the history `OLD` /
`REJECTED`) and the calculated offset. *Re-sync* recomputes from the current
anchors and drops manual adjustments. The offset is shown as the video
position of the scheduled session start (e.g. `+1730.00 s` = 28:50 in the video).

### Qualifying, Sprint Qualifying and practice (session-aware)

The session type comes from the session metadata (Race / Sprint → race board;
Qualifying / Sprint Qualifying → Q1–Q3 / SQ1–SQ3; Practice 1–3 → FP1–FP3). The
session's structure is read from the official timing (`server/session_phases.py`),
never from a schedule: `ExtrapolatedClock` (the session clock of the TV graphic:
it is posted when it starts, e.g. 14:59 exactly one second after 15:00, and when
it reaches 00:00:00), `SessionData` (QualifyingPart + status history) and race
control. The phase lengths are the clock's own start values (Suzuka 2026: Q1 18:00,
Q2 15:00, **Q3 13:00**). These small topics are downloaded as soon as the session is
known (a few kB), the big data still only after SYNC.

**Leaderboard** – `leaderboard = state(session data, current F1 time)`:

* qualifying: ranked by the best **valid** lap of the *current* part only
  (`BestLapTimes[part]` plus every completed lap of that part), gap to P1, `NO TIME`
  for cars without a time, knocked-out cars below with `OUT Q1` / `OUT Q2`; when Q2
  starts the ranking starts again; practice: best valid lap of the session so far
* a lap time that race control deleted (`CAR 41 (LIN) TIME 1:31.537 DELETED …`) is
  never a best lap – the next valid lap of that phase is used (the feed itself does
  not correct `BestLapTimes`, seen in the real Suzuka 2026 data)
* columns: BEST · GAP · LAST · NOW (`L19·S2` = lap being driven and the sector being
  driven – never the last completed one); the title bar shows `QUALIFYING — Q2`,
  the phase clock (left / elapsed of the official length) and a strip with Q1 / Q2 /
  Q3 (START / END markers, red flags / chequered flag only once passed, small ticks =
  completed laps of the selected car)
* driver detail: NOW `LAP 19 · S2` + running lap time, BEST, LAST, S1–S3 with the
  sector being driven (running time), the last completed one (`LAST`) and the personal
  best of each sector; `--` whenever the timing data does not say it
* lap / sector progress (`server/laps.py`, derived topic `_Laps`) comes only from
  timing messages (line crossing: `NumberOfLaps`, `LastLapTime`, sector 3, `LapSeries`;
  sectors: `Sectors[k].Value`, mini-sector `Segments`; pit exit = out lap). It is part
  of the replay state (checkpoints), so seeking back / forward gives exactly the same
  board – nothing from the future stays.

**SYNC for qualifying / practice** (the countdown and the start estimate are only
offered for races):

| Method | What you do | Result |
|---|---|---|
| **Session Clock** | pause VOYO on the session clock, choose the phase (Q1/Q2/Q3, FP), *Time remaining* `07:32` or *Time elapsed* `05:28` | F1 time from the official timing clock of that phase – **MEDIUM**, stated `±0.5–1 s` (the TV clock shows whole seconds). A value the clock stood still at (15:00 before Q2 starts, a red flag) is refused. |
| **Phase Marker – SYNC HERE** | pause exactly when the clock turns to 0:00 (Q2 END / SESSION END) or starts (Q2 START), press SYNC HERE | event anchor with the millisecond timestamp of the timing clock – `±0.1–0.3 s` when paused; with a second independent anchor (clock, another marker, `S`) **HIGH**, e.g. *Method: Q2 END Marker + Q3 Time Remaining* |
| Manual Exact Time, Restore Saved Sync, `S` | as above | as above |

API: `POST /api/sync/clock {"clock": "Q2|remaining|07:32"}`,
`POST /api/sync/marker {"marker": "Q2_END"}`; `GET /api/sync` → `sessionKind`,
`sessionTimeline` (phases with duration, SYNC HERE markers, current phase clock).

### Replay clock, race control state and what a car is doing (qualifying)

* **The video is the master clock.** The session clock (race / Q1–Q3 / practice) is computed
  on the server for the shown F1 moment (`ExtrapolatedClock` at that moment); the TV only
  interpolates between two states with the replay rate, which is **0 while VOYO is paused**
  (`dashboard/components/f1time.js` `clockNow`). A seek rebuilds the complete state for the new
  moment. The clock never goes below 0:00; when the session has officially ended (`Finished`)
  it stays where it was then - also in a race, where the feed never stops the clock itself.
* **Session flow** (`server/race_control.py` + `normalizer.session_flow`): RUNNING →
  SUSPENDED (red flag) → RUNNING → FINISHED from the official session status, the track
  status and race control - each only from the moment it was issued (seeking back before a
  red flag shows the running session again). Without `TrackStatus` the flag / SC / VSC state
  is derived from the race control messages (marked "from race control"); with no data it is
  UNKNOWN, never an invented GREEN. `Race Control coverage: COMPLETE / PARTIAL / NONE` is shown
  in the SYNC menu details and in view 5.
* **FIA per driver**: the latest messages naming a car (only that car) are listed in its
  detail panel; lap deletions invalidate a lap only when the message names its time
  (`TIME 1:31.537 DELETED`) or its lap number (`LAP 11`); a "LAP DELETED - DOUBLE YELLOW"
  without either is shown in race control but attached to no lap.
* **Qualifying / practice lap state** (`server/lap_state.py`): `OUT` / `PREP` / `HOT` / `COOL`
  next to `L12·S2` on the board and in the driver detail (`LAP 12 · S2 HOT LAP`). No field in
  the feed says it, so it is classified per driver against the driver's OWN pace (personal best
  sectors / lap / finish-line speed; the session's best only while the driver has none): lap
  begun at the pit exit → OUT LAP; sectors within 3 % → HOT LAP; ≥ 10 % slower → PREP, or
  COOLDOWN after a hot lap; nothing completed yet → the finish-line speed with which the lap
  began, or the time already spent in the sector. Ambiguous → nothing shown (UNKNOWN), and
  nothing before at least three cars have set a representative lap.

### AUTO MEDIA SYNC – which session is this video? (recordings only)

Two separate questions, never mixed:

    VOYO video ─► AUTO / MANUAL SESSION DETECTION ─► load that OpenF1 / F1-archive session ─► SYNC ─► replay timeline
                  "which F1 session is this video?"                                         "which F1 instant is currentTime?"

Detection reads only what the page states – VOYO title, media title, `og:title`,
the URL path (never its query) and a publish date if the page has one – and
normalises Slovenian and English forms (`VN Azerbajdžana`, `Velika nagrada
Azerbajdžana`, `Azerbaijan Grand Prix`, `Azerbaijan GP`, `vn-azerbajdzana-dirka`;
`dirka`/`race`, `kvalifikacije`/`qualifying`, `1. prosti trening`/`trening 1`/`FP1`,
`sprint`, `sprint kvalifikacije`/`Sprint Qualifying`). VOYO's page suffix
*"Glej dirke online"* is boiler plate and is ignored. Then it picks the OpenF1
session – and **never guesses**:

* no Grand Prix / two Grands Prix / two session types in the title → **AUTO MEDIA SYNC FAILED**;
* a title with only the Grand Prix (`VN Azerbajdžana - Glej dirke online`) is the
  **Race** only by VOYO's naming convention *and* if the video is ≥ 2.25 h
  (`race_min_video_seconds`) – otherwise you choose;
* no year in the title and the session exists in several seasons → the latest is
  taken only if it was ≤ 21 days ago (`assume_recent_season_days`), shown as
  "season ASSUMED"; otherwise you choose. A year that does not match fails.

The SYNC menu shows **MEDIA** (e.g. *Azerbaijan Grand Prix — Race*, detected /
selected / failed, *OpenF1 session loaded*) and **SYNC** (*SYNC REQUIRED* until
you sync, then *SYNCED — HIGH* with method, offset, health). **SELECT SESSION**
(TV menu or phone remote) lists the season's Grands Prix and sessions from
OpenF1 (fallback: the F1 archive index). A manual choice is remembered for that
`mediaId`. Another video unloads the previous session at once; a saved **sync**
is only restored for the same `mediaId` **and** session – another recording of
the same Grand Prix is a new video.

### Watching a recording (VOD)

`launch.bat vod` (or `python main.py --vod` / `--vod 11253`): the server
detects the session from the VOYO title (`Velika nagrada Japonske – dirka`,
`VN Kitajske: sprint kvalifikacije`, `1. prosti trening`, …; it never guesses
a missing session type), loads that session from the public F1 live-timing
archive (tyres, pit, penalties, positions, telemetry, race control, weather)
and shows it **at the video's time** – seeking in VOYO seeks the dashboard,
pause freezes it. Choose a session manually with
`curl -X POST http://127.0.0.1:8080/api/sync/session -d '{"session_key":11253}'`
or `[vod] session_key`. OpenF1 and the archive are cached in `data/`; if they
are unreachable the state says so and no time is invented.

API: `GET /api/sync` (state: `synced, absoluteTime, sessionKey, sessionName,
offsetSeconds, errorSeconds, confidence, method, anchor, source, reason`),
`POST /api/sync/{capture|countdown|exact|auto|estimate|clear|clock|marker}`,
`POST /api/sync/session`.

### Drift, pause, seek, clock loss

* **Pause** → the target stops; nothing advances (session clock, map, timing).
* **Seek / DVR** → the target jumps with the video; the state is rebuilt from
  the buffer; the map is refilled around the new time.
* **Buffering** (`readyState < 3` or the position not moving while "playing")
  → treated like a pause.
* **Drift**: the offset is constant for one video; a new anchor applies at
  once, `R` (force resync) re-applies the anchors and drops manual trims.
* **Video change** (other `mediaId` / page) → the old sync is not used; the
  saved sync of that video is restored if it belongs to the same session.
* **VOYO clock lost** (window closed, no video) → recording: the shown time is
  held and the menu says so; live stream: the last delay is held (no jump).
* `SYNC +` / `SYNC −` (`+`/`−`, CH+/CH−, phone remote, menu) adjust ±0.25 s on
  top → confidence **MANUAL**; the next anchor resets the adjustment.

### How the clock gets from VOYO to the server

`tools/tv_launcher.py` starts the VOYO window (dedicated profile) with
`--remote-debugging-port=9223` and runs `tools/voyo_clock.py`, which evaluates
the small, read-only `tools/voyo_clock_probe.js` in the VOYO page 5× per second
(`currentTime, paused, playbackRate, readyState, seeking, ended, duration,
buffered, seekable` + the standard media events) and posts
`{playback_time, paused, playback_rate, timestamp_local, …}` to
`POST /api/sync/voyo` (once per second while paused). No browser extension, no
user script, no layout scraping. **No DRM/EME, licence, key, stream or network
access; nothing is recorded or copied; the player is not controlled.**

### On screen

* Top bar / race-info / focus bar: the **SYNC** button – `SYNC ±0.1s ●`
  (recording) or `SYNC 5.24s ●` (live: delay behind live); dot green HIGH,
  light green MEDIUM, blue MANUAL, hollow amber LOW (`ESTIMATED`), orange
  `NOT SYNCED`; `❚❚` paused, `…` buffering, `!` clock lost. Hidden on a plain
  live dashboard without video.
* LOW / UNSYNCED on a recording: a banner over the leaderboard, the session
  clock only as `≈1:23` (LOW) or N/A (UNSYNCED), never an exact-looking time.
* The menu's *Details* show mode, source, reference events, VOYO state,
  TV delay, receive latency, buffer, VOYO metadata (display only) and flags.

Recommended for a recording: before the start, pause on VOYO's countdown →
**VOYO Countdown** (MEDIUM) → at lights out press `L` (→ HIGH). Joining
later: **Manual Exact Time** or the countdown if it is still in the video,
then `S` at a couple of line crossings of the selected car.

## 9c. AUTO SYNC (VOYO stream instances, LIVE DATA DELAY)

AUTO SYNC is part of the sync above, not a second engine (`server/autosync.py`):

* **Stream instance.** Every VOYO stream / recording the server sees gets a `stream_instance_id`
  and the server wall-clock time it appeared (`first_seen`) - and, when the server saw it from its
  beginning, the time position 0:00 played (`stream start wall time` = wall clock − position).
  A new instance starts on another video (asset / media id), another recording on the same page
  (shorter length), or - LIVE - a page / player reload (the clock probe reports a new page-load id;
  or `loadstart` / `emptied` with the position falling back). Seek, pause and buffering never start
  one. A new instance inherits no offset and no stream-start mark; a reopened recording (same video
  + length = the same timeline) gets the sync saved for that video + F1 session back.
* **Why the stream start is not the offset.** It says when 0:00 played on this server - not which
  F1 moment the frame shows (the broadcaster's / CDN's latency; for a recording nothing at all).
  The picture cannot be read (DRM) and VOYO's playlists / metadata carry no UTC. So:
  stream start = base anchor; F1 events matched to VOYO positions (Event Sync, `L`, countdown,
  session clock, MARK STREAM START's origin) = calibration; offset = the median of the precise
  ones (outliers rejected) exactly as in §9b.
* **LIVE DATA DELAY** = server receive time − F1 message timestamp of every live feed message,
  median over 60 s (± spread; STABLE when ≥ 20 samples within ±0.35 s). Shown as the `DATA 4.8s`
  chip next to the SYNC chip and in the SYNC menu. The server clock must be NTP-synchronised.
* **RECOMMENDED SYNC** (LIVE): the stream latency learned on calibrated stretches of the same live
  stream, else the measured LIVE DATA DELAY (the dashboard can never be closer to real time than its
  data), else `broadcast_delay_seconds`. It seeds a new live instance's estimate (CALIBRATING); the
  configured value is never changed.
* **Signs.** SYNC chip = TOTAL DELAY = how far the dashboard (= the video) is behind real time.
  LIVE DATA DELAY = the F1 data's part of it. **VOYO SYNC = TOTAL DELAY − LIVE DATA DELAY**
  (+ = the video is behind the F1 data).
* **States.** SEARCHING (no stream / no session) → CALIBRATING (estimate only, or moving to a new
  offset) → LOCKED (calibrated, the applied offset = the computed one); UNSTABLE when observations
  disagree / a new one is waiting for keep old / use new; HOLD while VOYO is paused / buffering /
  seeking (never taken for drift). A LIVE stream's latency change without a seek (player catch-up)
  is reported but does not move the sync - it follows the video position.

## 9d. VOYO stream recordings (one package per stream instance)

Separate from the F1 timing recordings (`[live] record` → `data/recordings/*.jsonl.gz`, replayed with
`--replay`): every VOYO stream instance AUTO SYNC detects (§9c) gets a **recording package** with
what is needed to use that stream again later without the VOYO window - its identity, the playback
timeline, the sync anchors and observations, LIVE DATA DELAY - and, opt-in, a window capture.

**Written by the server.** The PC that shows VOYO only sends what it already sent for the sync:

```
PC (VOYO window, --remote-debugging-port on 127.0.0.1)
  tools/voyo_clock.py  --Runtime.evaluate(voyo_clock_probe.js, read-only)-->  <video> state
  POST /api/sync/voyo  (5/s while playing, X-Remote-Token)                 -->  server
        { playback_time, paused, playback_rate, ready_state, seeking, ended, duration,
          seekable_start/end, buffered_end, events[loadstart/emptied/seeking/seeked/waiting/
          playing/pause/ratechange/ended/...], meta{length,startAt,drmProtected},
          page{title, og_title, media_title, media_id, url_path, published, options_fp, load_id},
          timestamp_local }
server: parse_voyo_sample -> SyncManager.update -> StreamTracker (new instance?) -> VoyoStreamRecorder
  <-- reply { ok, recording: { instance, capture, segment_seconds, fps, crf } }
```

Stream-instance detection runs on the server (`server/autosync.py`); the server clock is the time
base (receive time of each sample). A new package starts only when the tracker reports a new
instance (another video / media id / shorter recording / live reload) - never on position updates,
pause, buffering, seek or a lost clock (that is written as a `gap`). The previous package is
closed first; the new one inherits no offset and no MARK STREAM START (the sync of a reopened
recording comes back through the per-video+session store, and a "resumed" instance appends to its
old package).

### Layout under `[voyo.recording] path`

```
<path>/
  index.json                      { schema, recordings: { <id>: summary } }      - the fast listing
  <stream_instance_id>/
    manifest.json                 summary - see below (status recording / closed / interrupted)
    meta.json                     identity in full: tracker instance, VOYO ids (asset, media_id, title,
                                  options_fp, load_id, url_path, page fields), detection reason / time / mode
    timeline.jsonl                {"t": server epoch s, "pb": s, "state": playing|paused|buffering|seeking|
                                   ended, "rate", "events": [...], "dur", "edge"}  - one line per state change,
                                  per event and every timeline_interval_seconds while playing;
                                  {"t", "type": "gap", "seconds"} when the clock was lost
    anchors.json                  { stream_start_wall_time (automatic base anchor), stream_start_marks
                                    (MARK STREAM START / RESET events), stream_start_mark (active one),
                                    anchors (applied), pending, history, mapping, session }
    sync_observations.jsonl       {"type": "anchor", status applied|pending|rejected|used_new|removed, ...anchor}
                                  {"type": "pair", "t", "pb", "f1_ms", "offset", "confidence", "quality"}
                                  {"type": "live_data_delay", "t", "seconds", "spread", "state", ...}  (LIVE)
                                  {"type": "autosync", "state", "reason", "confidence"}
                                  {"type": "stream_start" | "stream_start_reset", ...}
                                  {"type": "note", "text"}   (clock lost, session changed, write error ...)
    capture/                      opt-in window capture: run<time>_seg_NNNNN.mp4 + capture.jsonl
                                  {name, bytes, pc_start_epoch, pc_end_epoch, pc_clock_minus_server_s}
```

`manifest.json`: `stream_instance_id, status, detected_at, stream_start_wall_time (+ _epoch, _how),
detection_reason, live, asset, media_id, title, mode {selected, effective, source}, session
{session_key, meeting, session_name, kind, year}, session_history, duration, position {first, min,
max}, counts {timeline, observations, anchors, pairs, gaps}, sync {confidence, offset, method,
state}, live_data_delay, stream_start_marks, capture {segments, bytes, deleted_at}, closed_at,
close_reason, resumes`. Times are UTC ISO (`...Z`) of the server clock; `offset` = F1 epoch s −
VOYO position s (F1 time of a frame = position + offset).

The stream instance is the identity; the F1 session is a secondary association (learned when the
session is identified, changes are kept in `session_history`). Packages of different GPs /
sessions / recordings never share files.

### Load API (served by the server, reads the configured path)

| | |
|---|---|
| `GET /api/voyo/recordings` | `{status: {path, ok, error, free_bytes, capture, current}, recordings: [summary...]}` newest first - title, time, session, duration, sync quality, LIVE DATA DELAY, capture size |
| `GET /api/voyo/recordings/<id>` | manifest + meta + anchors + capture list + `calibration` (AUTO SYNC re-run from the saved anchors); `?full=1` adds timeline + observations |
| `GET /api/voyo/recordings/<id>/files/<name>` | a file (`timeline.jsonl`, ..., `capture/<segment>`) - remote token required when set |
| `PUT /api/voyo/recordings/<id>/capture/<name>` | the PC's capture uploads (token, only with `record_video_capture`) |

In Python: `server.voyo_recording.load_package(path)` (all files as data, tolerant of a line cut by a
crash), `recalibrate(package)` (median offset of the applied anchors, spread, LOCKED / UNSTABLE) and
`live_delay_history(package)`. With `offset` and `timeline.jsonl` the existing timeline / sync code
can be driven offline: the F1 time at server time *t* is `pb(t) + offset`.

### Window capture (opt-in, default off)

`record_video_capture = true` on the server → its clock replies say `capture: true` → the launcher
(`tools/voyo_capture.py`) records the VOYO window with ffmpeg on the PC (Windows `gdigrab` by window
title, Linux/X11 `x11grab` by window id), in `capture_segment_seconds` fragmented-MP4 segments
spooled in `data/voyo_capture_spool/<id>/`, and uploads each finished segment to the server.
A screen recording of what the window shows - it never reads VOYO's stream, buffers or DRM; if the
browser / OS blanks protected video, the capture is black (not worked around). `launch.bat` / 
`tv_launcher.py --no-capture` disables it on a PC. Video files are deleted after
`keep_<practice1|practice2|practice3|sprint_qualifying|sprint|qualifying|race|other>_days`
(0 = keep); metadata stays.

### The server's own VOYO player

`tools/voyo_server_player.py` (`server/voyo-player.sh`, service `f1-voyo-player`) lets the server
open VOYO itself in Google Chrome on a virtual screen around each F1 session and record it, with no
PC on. Its samples carry `"channel": "server_player"` (and a `session_hint` from the schedule),
accepted from the server itself only. They get their own stream tracker and packages (manifest
`channel: "server_player"`) and never touch the dashboard's sync of the VOYO window you watch. A
POST with `"close": true` closes its package at the end of the session window. Setup:
`server/README.md` "Recording VOYO on the server".

### Errors

At start the path is resolved, created (if `create_path_if_missing`), write-tested and logged
(`VOYO stream recordings: /mnt/usb/voyo (free 812.4 GB)`). Not writable / missing mount / inside
the F1 recordings folder → `VOYO stream recording DISABLED: <why>` and nothing is written anywhere
else. A write error later (disk full, USB unplugged) stops the writer with an error in the log and
in `/api/voyo/recordings`; the path is re-checked every 60 s and the open package continues (a
`note` marks the gap). `min_free_bytes` stops writing before a disk is full.

## 9e. Team radio (`[team_radio]`)

A **TEAM RADIO** panel next to RACE CONTROL (FULL_DASHBOARD: beside it under the board; RACE_VIEW: below
it, right of the map). It lists the radio clips F1 published for the session shown, plays them, filters
by team / driver / text, and can show optional AI transcripts.

### Research: where team radio comes from (October 2026)

| Source | What it is | Used here | Limits |
|---|---|---|---|
| Live-timing topic `TeamRadio` (SignalR) | `{"Captures": [{"Utc", "RacingNumber", "Path": "TeamRadio/<file>.mp3"}]}`; the MP3 is at `https://livetiming.formula1.com/static/<SessionInfo.Path><Path>` | **LIVE**: already subscribed on the existing connection (`CORE_TOPICS`); whatever arrives is shown | F1 publishes a **selection** of clips (the broadcast ones), each **after** it was spoken. Whether F1 sends this topic to an anonymous or an **F1 TV Access** connection is **not verified**: the help page lists live team radio as a **Pro/Premium** feature, and the project's own live recording (`data/recordings/sample-2026-japan-race.json.gz`, 47 071 messages, auth mode unknown) contains **no** TeamRadio message. Having `F1TV_TOKEN` does not mean this feed is delivered. |
| Archive `TeamRadio.jsonStream` (`livetiming.formula1.com/static/<path>`) | the same captures, timestamped, after the session; FastF1 lists it as `team_radio` (mirror `livetiming-mirror.fastf1.dev`) | **REPLAY / VOD**: loaded with the other archive topics and fed **in step with the video** - a clip appears only when the video reaches the moment it was published | public, no login; not every session has it |
| OpenF1 `GET /v1/team_radio?session_key=…` | `{date, driver_number, recording_url, …}` - `recording_url` points to the same F1 archive MP3s | **VOD fallback only**, when the F1 archive has no TeamRadio stream (`openf1_fallback`); labelled `ARCHIVE · OPENF1` | OpenF1 says coverage is a limited selection and has decreased a lot during 2026: never treated as complete or guaranteed. Only `recording_url`s on the archive host under this session's path are accepted. |
| F1 TV app (Pro) | in-player radio | **not used** | no public API; the project does not bypass subscriptions or DRM |

Nothing is made up: no clip, URL, driver, time or transcript is generated. A clip whose capture has no
valid file is not shown as playable; an invalid path / driver / time is dropped.

### What the panel shows

* every clip newest first: team colour, time (local hh:mm:ss), driver TLA + number, team, a play status
  (▶ ready, … loading, ❚❚ playing, ✕ failed, — no recording) and the kind:
  * `LIVE FEED` - received on the live connection (a recording, delayed by F1 - not a live audio stream);
  * `ARCHIVE` - the session archive (replay / VOD); `ARCHIVE · OPENF1` - OpenF1's list;
  * the panel badge says which: `LIVE FEED`, `ARCHIVE · WITH THE VIDEO`, `REPLAY · ARCHIVE`, `TEST MODE`.
* no clips: an honest reason (live: "No team radio clip received on this connection yet … with F1 TV
  Access live team radio may not be delivered at all"; VOD: "No team radio published up to this point of
  the session (or none in the archive for it)"; TEST MODE: not simulated).
* new clips while you scroll down do not move the list; a `N NEW ↑` pill jumps to the top.
* filters (team, driver, search in driver / team / transcript) are kept per browser (`localStorage`).

### Playback

One `<audio>` element: click a row (or ▶) to play, again to pause, seek bar + time. Only one clip at a
time. The browser never gets an F1 URL: it plays `/api/radio/audio/<id>`, and the server fetches the MP3
**only** for a clip of the session shown now, **only** from `archive_base`, never following redirects,
size-limited (`max_clip_mb`), checked to be an MP3, with Range support for seeking. Played clips stay in a
small memory cache (`audio_cache_mb`); nothing is written to disk. A missing / failed recording shows the
reason (`recording not available (F1 archive answered 404)`, `F1 archive did not answer in time`, …) and
is not retried for 5 minutes. A radio failure never affects the timing loop or the rest of the dashboard.

The audio endpoint follows the dashboard's rules: behind `protect_dashboard`, and through the public
gateway (Tailscale Funnel) only for an approved `/tv` page or the trusted phone.

### Transcripts (optional, AI)

F1 publishes no transcripts. `tools/transcribe_radio.py` (needs `pip install faster-whisper`; only this
tool) transcribes the clips on a PC with a GPU and sends the text to the server, which shows it under the
clip with an **AI** tag and the model's confidence - machine transcription, may be wrong, never official.

    python tools/transcribe_radio.py --server http://<server>:8080 --token <[remote] token> --loop 20

RTX 3070 Ti (8 GB): `--model large-v3 --device cuda --compute-type float16` fits and handles radio noise
best; `distil-large-v3` / `medium.en` are faster. The tool authenticates with the `[remote]` token
(`X-Remote-Token`); a server without a token accepts it only from its own machine. Transcripts are kept
in `data/radio_transcripts.json`. The panel works the same without them.

### Configuration

```toml
[team_radio]
enabled = true              # false: panel hidden, endpoints off
openf1_fallback = true
audio_cache_mb = 12
max_clip_mb = 8
fetch_timeout_s = 10
transcripts = true          # accept transcripts from tools/transcribe_radio.py
archive_base = "https://livetiming.formula1.com/static/"
```

Environment overrides as for every section, e.g. `F1DASH_TEAM_RADIO_ENABLED=false`.

### Not verified / not available

* Live team radio with F1 TV **Access** or anonymously: not verified (no live session could be tested
  from the development environment, and the bundled live recording has none). If F1 does not send the
  topic, the panel says so; it does not pretend.
* OpenF1 coverage for 2026 sessions: not verified (OpenF1 was not reachable from the development
  environment); the code treats any OpenF1 answer, including an empty one, as optional.
* No live audio stream of team radio exists in any public source; "live" here means clips published
  during the session.

## 10. Troubleshooting

| Symptom | Check |
|---|---|
| "LIVE DATA DISCONNECTED · RECONNECTING…" | `curl -I https://livetiming.formula1.com/signalrcore/negotiate` from the server; firewall/DNS; the log line after "F1 feed disconnected" gives the reason. Try `transport = "legacy"`. |
| Timing works but map has no cars / telemetry N/A | Expected when F1 withholds Position.z from your connection and the archive stream is not readable (§7a). Run `tools/probe_feed.py` during the session. With token: log says "token EXPIRED" or `negotiate rejected (HTTP 401)` → copy a fresh `login-session` cookie. |
| "TRACK MAP UNAVAILABLE" | Server cannot reach `api.multiviewer.app`: run `tools/fetch_tracks.py` from a machine that can and copy `data/tracks/`, or wait – the outline is learned from position data after one lap. |
| Nothing between sessions | Normal: the feed is idle; the map shows NO LIVE SESSION and the next session. |
| Dashboard not in sync with the VOYO picture | Open the SYNC menu (`Y`): it shows the session, the video position, method, anchors and the reason. No video position → see "VOYO clock" lines in the launcher console. Session "not identified" → choose it (`POST /api/sync/session`). Without the VOYO window: `--delay 30` / `SYNC +/−`. |
| Sync panel says `VOYO CLOCK LOST` | The launcher is not reading the player: VOYO window closed, no `<video>` on the page yet, or the window was not started by the launcher (`--attach` needs a VOYO window started with `--remote-debugging-port=9223`). |
| Recording on VOYO, time set, board stays empty; no `VOD` / `SYNC CHECK` lines in the launcher console | The server runs in **LIVE** mode (banner `RECORDING IN LIVE MODE`): live timing only keeps the last minutes. `launch.bat` without an argument starts in **AUTO** (LIVE only from 90 min before a session until 60 min after it, otherwise VOD) and prints `Mode: AUTO - detected …`. Click **VOD** in the MODE selector (top right, key **E**) - the server switches at once, no restart. |
| Board empty before SYNC (VOD) | Intended: without a video time no session data is shown, and with `[vod] preload_data = false` (default) it is not even downloaded – only the session details (start, time zone, OpenF1 lap times for L/S). Banner `SYNC REQUIRED`; the download starts the moment a time is set. |
| Time entered, but the board stays empty (VOD) | Look at the **DATA** line in the SYNC menu and the banner: `DOWNLOADING SESSION DATA` (a race is 200+ MB – the sync you entered is kept and the board fills when the download finishes), `SESSION DATA NOT LOADED` (reason shown, retried every 60 s) or `NO SESSION DATA AT THIS TIME` (the time is outside the recorded session). The launcher console mirrors the important server lines (`server … AUTO MEDIA SYNC / VOD / SYNC CHECK`) and every change of the VOYO player (`VOYO clock: playing at …, video length …`). |
| Dashboard jerky on a weak TV browser | `[dashboard] map_fps = 20`, `animations = "reduced"`. |
| Remote keys do nothing | Server log shows every remote event (`Remote key ...`). `Unmapped remote key` → add it to `[remote.keymap]`. |
| Server log | Connections, reconnects, session changes, parser errors (rate-limited), WebSocket clients and remote events are logged; telemetry packets are not. `log_level = "DEBUG"` for more. |
| Anything else | `http://server:8080/api/health` and `http://server:8080/api/state` show the normalized state. |

## 11. Security

* The browser can only send `{"type":"key"}` / `{"type":"command"}` messages
  (max 1 KB, rate-limited); keys must match `[A-Z0-9_]` and a configured
  mapping, commands must be in the whitelist, arguments are validated.
  No shell commands exist anywhere in the server.
* Remote HTTP API can be protected with a token. Intended for your LAN only –
  do not expose port 8080 to the internet.
* The F1 TV token (optional) stays on the server; it is never sent to the browser.
* VOYO clock: `POST /api/sync/voyo` is accepted only from the same computer
  (`[sync] allow_remote_clock = false`), size- and rate-limited, every field is
  type/range checked. The VOYO window's DevTools port (`127.0.0.1:9223`) is only
  reachable from this computer but gives local programs control of the
  dedicated VOYO browser profile – use `tv_launcher.py --no-clock` if that is
  not acceptable on your PC (sync then falls back to a fixed delay).

## 12. Credits / licences

* Feed protocol knowledge: FastF1, undercut-f1, f1-dash and other open-source projects.
* Circuit geometry: MultiViewer circuit API (downloaded at runtime, not bundled).
* Test circuits: [bacinger/f1-circuits](https://github.com/bacinger/f1-circuits) (MIT, `data/test_tracks/LICENSE-f1-circuits.md`).
* Sample recording: [matteocelani/f1-telemetry](https://github.com/matteocelani/f1-telemetry) (MIT, `data/recordings/LICENSE-f1-telemetry-samples.txt`).
* F1, FORMULA 1 and related marks are trademarks of Formula One Licensing B.V.
