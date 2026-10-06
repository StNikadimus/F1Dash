# pc variant/ — Windows deployment

This folder holds the Windows launchers. All code is shared with the Linux server in `..\main\`.
The Python environment (`.venv`) and the data (VOYO login profile, F1 TV sign-in,
sync state and recordings) stay in the **repository root**, so updating from the old
layout loses nothing.

| File | What |
|---|---|
| `launch.bat` | everything on this PC: server, dashboard and VOYO window full screen, the TV agent and the VOYO clock bridge |
| `start-windows.bat` | only the dashboard server (opens the browser) |
| `..\launch.bat` | shim in the repository root that calls `pc variant\launch.bat` (old shortcuts keep working) |

## Requirements

- Windows 10/11 with Python 3.11+ (`py -3 --version`). `launch.bat` creates `.venv` and
  installs `main\requirements.txt` the first time.
- Microsoft Edge or Chrome (for the dashboard and VOYO windows).

## Use

```bat
launch.bat                 AUTO: LIVE while an F1 session is on, otherwise VOD
launch.bat live | vod | test | replay
launch.bat capture1|capture2|capture3|capture0   AirParrot capture mode (default: capture3 = no GPU,
                                                 from [voyo] capture_compat = "no-gpu")
launch.bat server http://192.168.1.10:8080       use the Linux server's backend (..\server)
```

With `server <url>`, this PC does not start its own backend. It shows only the dashboard
and VOYO, and runs the TV agent and the VOYO clock bridge (which reads the video position
read-only and posts it to the server for AUTO SYNC). Set the same remote token on both sides:
`F1DASH_REMOTE_TOKEN` (an environment variable) here, and `server/.env` on the server.

Closing the window (or the dashboard / VOYO window) closes everything and restores the taskbar.
Logs: `data\server.log`.

## VOYO stream recordings and the opt-in window capture

The server writes the VOYO stream recordings, not this PC. This PC only sends the VOYO clock
samples it already sends for the sync (`POST /api/sync/voyo`).

- **Everything on one PC:** the "server" is this PC. Choose the disk with `[voyo.recording] path`
  in `main\config\config.toml`, e.g. `path = "D:/F1Recordings/voyo_streams"`, or with the
  `F1DASH_VOYO_RECORDING_PATH` environment variable.
- **With the Linux server:** the recordings go to the server's configured path.

The window capture is **off** unless the server's `[voyo.recording] record_video_capture = true`.
When it is on, the launcher records the VOYO window with ffmpeg (`gdigrab`):
- AirParrot `capture3` stays the default.
- The capture is a screen recording only. The result is black if the browser blanks protected video.
- Segments are buffered in `data\voyo_capture_spool\` and uploaded to the server, then deleted
  here. Failed uploads are retried, also after the next start.

Setup and options:
- Install ffmpeg (e.g. `winget install ffmpeg`). If it is not on PATH, set
  `[voyo.recording] ffmpeg = "C:/ffmpeg/bin/ffmpeg.exe"`.
- Audio is optional: `capture_audio_device = "Stereo Mix (Realtek(R) Audio)"`. `ffmpeg -list_devices true -f dshow -i dummy`
  lists the device names.
- Keep the VOYO window on screen (not minimized) while recording.
- `python main\tools\tv_launcher.py --no-capture ...` never captures on this PC.
