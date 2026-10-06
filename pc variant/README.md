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
