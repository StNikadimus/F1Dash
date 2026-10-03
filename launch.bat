@echo off
REM F1 TV: dashboard server + dashboard + VOYO window + TV agent.
REM   launch.bat          AUTO: LIVE while an F1 session is on, otherwise VOD - switch any time with the
REM                       MODE selector on the dashboard (AUTO / LIVE / VOD, key E), no restart
REM   launch.bat test     simulator
REM   launch.bat replay   replay
REM   launch.bat vod      force VOYO recording (VOD): data of the session shown in VOYO
REM   launch.bat live     force live timing
REM Closing this window (or the dashboard / VOYO window) closes everything and restores the taskbar.
title F1 TV
cd /d "%~dp0"
if not exist .venv\Scripts\python.exe (
  echo Creating Python environment...
  py -3 -m venv .venv 2>nul || python -m venv .venv
)
call .venv\Scripts\activate.bat
python -m pip install -q -r requirements.txt
set MODE=
if /i "%~1"=="test" set MODE=--mode test
if /i "%~1"=="replay" set MODE=--mode replay
if /i "%~1"=="live" set MODE=--mode live
if /i "%~1"=="vod" set MODE=--mode vod
python tools\tv_launcher.py --start-server %MODE%
REM safety net: taskbar back even if the launcher crashed
python tools\tv_launcher.py --restore-taskbar >nul 2>&1
exit
