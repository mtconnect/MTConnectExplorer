@echo off

:: Change to the directory where this script lives
cd /d "%~dp0"

:: Delete venv if it exists
IF EXIST "venv/" (
    echo Removing venv/...
    RD /S /Q "venv/" || (
        echo Error: Could not remove python virtual environment "venv/"
        pause
        exit /b %ERRORLEVEL%
    )
    echo Removed venv/
)

IF EXIST "src/bin/" (
    echo Removing src/bin/...
    RD /S /Q "src/bin/" || (
        echo Error: Could not remove "src/bin/"
        pause
        exit /b %ERRORLEVEL%
    )
    echo Removed src/bin/
)

IF EXIST "build/" (
    echo Removing build/...
    RD /S /Q "build/" || (
        echo Error: Could not remove "build/"
        pause
        exit /b %ERRORLEVEL%
    )
    echo Removed build/
)

IF EXIST "dist/" (
    echo Removing dist/...
    RD /S /Q "dist/" || (
        echo Error: Could not remove "dist/"
        pause
        exit /b %ERRORLEVEL%
    )
    echo Removed dist/
)

echo Clean completed successfully
:: Pause if arg not specified
IF "%~1"=="" (
    pause
)
