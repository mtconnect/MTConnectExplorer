@echo off

:: Change to the directory where this script lives
cd /d "%~dp0"

call dev.bat no_pause || (
    echo Error: Could not update venv
    pause
    exit /b %ERRORLEVEL%
)

echo Building application...
venv\Scripts\pyinstaller.exe installer.spec --clean || (
    echo Error: Could not update venv requirements
    pause
    exit /b %ERRORLEVEL%
)
echo Built application

IF EXIST "build/" (
    echo Removing build/...
    RD /S /Q "build" || (
        echo Error: Could not remove "build/"
        pause
        exit /b %ERRORLEVEL%
    )
    echo Removed build/
)

echo Build completed successfully
:: Pause if arg not specified
IF "%~1"=="" (
    pause
)
