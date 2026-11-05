@echo off
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo First run: py -3.11 tools\setup.py
  pause
  exit /b 1
)
".venv\Scripts\python.exe" tools\start.py
pause
