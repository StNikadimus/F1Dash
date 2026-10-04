@echo off
REM F1 TV: dashboard server + dashboard + VOYO window + TV agent.
REM   launch.bat          AUTO: LIVE while an F1 session is on, otherwise VOD - switch any time with the
REM                       MODE selector on the dashboard (AUTO / LIVE / VOD, key E), no restart
REM   launch.bat test     simulator
REM   launch.bat replay   replay
REM   launch.bat vod      force VOYO recording (VOD): data of the session shown in VOYO
REM   launch.bat live     force live timing
REM   launch.bat capture1 VOYO video black on the TV when mirroring (AirParrot)? capture1 = no GPU
REM                       video overlays, capture2 = + no hardware video decode, capture3 = no GPU
REM                       (combine: launch.bat live capture1), capture0 = browser default.
REM                       Default: [voyo] capture_compat = "no-gpu" (same as capture3, needed for AirParrot)
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
set CAPTURE=
for %%A in (%*) do (
  if /i "%%~A"=="test" set MODE=--mode test
  if /i "%%~A"=="replay" set MODE=--mode replay
  if /i "%%~A"=="live" set MODE=--mode live
  if /i "%%~A"=="vod" set MODE=--mode vod
  if /i "%%~A"=="capture0" set CAPTURE=--capture off
  if /i "%%~A"=="capture1" set CAPTURE=--capture no-overlays
  if /i "%%~A"=="capture2" set CAPTURE=--capture no-hw-decode
  if /i "%%~A"=="capture3" set CAPTURE=--capture no-gpu
)
python tools\tv_launcher.py --start-server %MODE% %CAPTURE%
REM safety net: taskbar back even if the launcher crashed
python tools\tv_launcher.py --restore-taskbar >nul 2>&1
exit
