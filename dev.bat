@echo off
setlocal enabledelayedexpansion

:: Change to the directory where this script lives
cd /d "%~dp0"

:: Download and extract agent.exe
IF NOT EXIST "src\bin\agent.exe" (
    echo Downloading agent.exe...
    mkdir build 2>nul
    for /f "delims=" %%u in ('powershell -NoProfile -Command "(Invoke-RestMethod https://api.github.com/repos/mtconnect/cppagent/releases/latest).assets | Where-Object { $_.name -like '*win64.zip' } | Select-Object -ExpandProperty browser_download_url"') do set AGENT_URL=%%u
    curl -L -o build\agent.zip "!AGENT_URL!" || (
        echo Error: Could not download agent.exe
        RD /S /Q "build"
        pause
        exit /b 1
    )
    tar -xf build\agent.zip -C build
    mkdir src\bin 2>nul
    for /d %%d in (build\agent-*-win64) do copy "%%d\bin\agent.exe" src\bin\agent.exe
    RD /S /Q "build"
    echo Downloaded agent.exe
)

:: Create virtual environment and install requirements
IF NOT EXIST "venv" (
    echo Creating venv...
    python -m venv venv || (
        RD /S /Q "venv" || (
            echo Error: Could not remove venv
            pause
            exit /b %ERRORLEVEL%
        )
        echo Error: Could not create venv
        pause
        exit /b %ERRORLEVEL%
    )
    echo Created venv
)

echo Updating venv...
.\venv\Scripts\pip.exe install -Ur requirements-src.txt || (
    echo Error: Could not update venv requirements
    pause
    exit /b %ERRORLEVEL%
)
.\venv\Scripts\pip.exe freeze > requirements.txt
echo Updated venv

echo Dev completed successfully
:: Pause if arg not specified
IF "%~1"=="" (
    pause
)
