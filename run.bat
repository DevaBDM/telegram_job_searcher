@echo off
REM Double-click (or schedule) to keep the job saver running.
REM Any arguments are forwarded to main.py, e.g.  run.bat --once
cd /d "%~dp0"
if exist ".venv\Scripts\python.exe" (
    ".venv\Scripts\python.exe" main.py %*
) else (
    python main.py %*
)
echo.
echo Job saver stopped.
pause
