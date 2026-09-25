@echo off
REM ============================================================
REM  Double-click this file to start the Firmware Compliance
REM  Portal. Then open http://127.0.0.1:5000 in your browser.
REM  To stop it, close this black window.
REM ============================================================

cd /d "%~dp0"

if not exist ".env" (
  echo.
  echo  ERROR: The settings file ".env" is missing.
  echo  Copy ".env.example" to ".env" and fill it in first.
  echo.
  pause
  exit /b 1
)

if not exist ".venv\Scripts\python.exe" (
  echo.
  echo  ERROR: The Python environment is missing.
  echo  Run this once to create it:
  echo      python -m venv .venv
  echo      .venv\Scripts\python.exe -m pip install -r requirements.txt
  echo.
  pause
  exit /b 1
)

echo.
echo  Starting the Firmware Compliance Portal...
echo  Open this address in your browser:  http://127.0.0.1:5000
echo  Leave this window open. Close it to stop the portal.
echo.

".venv\Scripts\python.exe" app.py

echo.
echo  The portal has stopped.
pause
