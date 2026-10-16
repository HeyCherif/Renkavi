@echo off
cd /d "%~dp0"
if exist "Renkavi.exe" (
  start "" "Renkavi.exe" %*
  exit /b
)
if exist ".venv\Scripts\python.exe" (
  ".venv\Scripts\python.exe" -X utf8 run.py %*
) else (
  python -X utf8 run.py %*
)
if errorlevel 1 pause
