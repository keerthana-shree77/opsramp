@echo off
setlocal
REM ============================================================
REM  Double-click this file. It asks OpsRamp for a few assets
REM  and writes down exactly what came back.
REM  The report opens in Notepad - send that back for diagnosis.
REM  No passwords or keys are written into the report.
REM ============================================================

cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
  echo  ERROR: the Python environment is missing.
  pause
  exit /b 1
)
if not exist ".env" (
  echo  ERROR: the settings file ".env" is missing.
  pause
  exit /b 1
)

echo.
echo  Asking OpsRamp for a few assets. This takes up to a minute...
echo.

".venv\Scripts\python.exe" "tools\diagnose_opsramp.py" %1 %2

if exist "OPSRAMP-DIAGNOSTIC.txt" (
  echo.
  echo  Report written. Opening it in Notepad...
  start "" notepad "OPSRAMP-DIAGNOSTIC.txt"
) else (
  echo.
  echo  No report was produced - see the messages above.
  pause
)
