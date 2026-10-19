@echo off
title Disk Map
cd /d "%~dp0"
echo.
echo  Disk Map is starting.
echo  Your browser will open. Keep this window open while you use it.
echo  Close this window to quit.
echo.
where py >nul 2>&1
if %errorlevel%==0 (
  py -3 -u "%~dp0diskspace.py"
) else (
  python -u "%~dp0diskspace.py"
)
if errorlevel 1 (
  echo.
  echo  Disk Map stopped because of an error.
  pause
)
