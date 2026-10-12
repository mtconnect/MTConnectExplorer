@echo off

:: Change to the directory where this script lives
cd /d "%~dp0"

:: Set up the venv and agent.exe (no_pause: dev.bat won't pause on success)
call dev.bat no_pause || (
    echo Error: Could not set up the environment
    pause
    exit /b %ERRORLEVEL%
)

:: Launch the app straight from the venv - no dist/ build required. Any extra
:: args are forwarded to the app, e.g. run.bat --port 9000
echo Starting MTConnectExplorer...
venv\Scripts\python.exe -m src %*

:: Pause if run without args (e.g. double-clicked) so output stays visible.
IF "%~1"=="" (
    pause
)
