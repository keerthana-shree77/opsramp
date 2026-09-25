@echo off
REM ============================================================
REM  Double-click this file to edit the settings.
REM  Notepad will open. Replace the SHOUTY placeholder text,
REM  then press Ctrl+S to save and close Notepad.
REM ============================================================

cd /d "%~dp0"

if not exist ".env" (
  echo.
  echo  The settings file is missing. Creating it from the template...
  copy ".env.example" ".env" >nul
)

start "" notepad ".env"
