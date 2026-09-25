@echo off
setlocal
REM ============================================================
REM  Double-click this file, then choose your recipe file.
REM  (You can also drag a recipe file onto this icon.)
REM  A report of what is inside it opens in Notepad.
REM  Copy that text and send it back for diagnosis.
REM ============================================================

cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
  echo  ERROR: the Python environment is missing.
  pause
  exit /b 1
)

set "RECIPEFILE=%~1"

if not defined RECIPEFILE (
  echo.
  echo  A "choose a file" window is opening...
  echo  Pick your firmware recipe file ^(.xlsx or .csv^).
  echo.
  powershell -NoProfile -STA -Command ^
    "Add-Type -AssemblyName System.Windows.Forms;" ^
    "$d = New-Object System.Windows.Forms.OpenFileDialog;" ^
    "$d.Title = 'Choose your firmware recipe file';" ^
    "$d.Filter = 'Recipe files (*.xlsx;*.xlsm;*.csv)|*.xlsx;*.xlsm;*.csv|All files (*.*)|*.*';" ^
    "if ($d.ShowDialog() -eq 'OK') { [System.IO.File]::WriteAllText('recipe_path.tmp', $d.FileName, [System.Text.Encoding]::ASCII) }"

  if not exist "recipe_path.tmp" (
    echo  No file was chosen. Nothing to do.
    pause
    exit /b 0
  )
  set /p RECIPEFILE=<recipe_path.tmp
  del "recipe_path.tmp"
)

if not exist "%RECIPEFILE%" (
  echo.
  echo  Could not find the file: %RECIPEFILE%
  pause
  exit /b 1
)

".venv\Scripts\python.exe" "tools\inspect_recipe.py" "%RECIPEFILE%" "RECIPE-REPORT.txt"

if exist "RECIPE-REPORT.txt" (
  echo.
  echo  Report written. Opening it in Notepad...
  start "" notepad "RECIPE-REPORT.txt"
) else (
  echo.
  echo  Something went wrong - no report was produced.
  pause
)
