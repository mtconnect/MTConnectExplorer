@echo off

:: Change to the directory where this script lives
cd /d "%~dp0"

call dev.bat no_pause || (
    echo Error: Could not update venv
    pause
    exit /b %ERRORLEVEL%
)

:: Build the client wheel and install it over the editable install so the
:: frozen binary uses (and proves) the real wheel
echo Building client wheel...
IF EXIST "lib\dist" RD /S /Q "lib\dist"
venv\Scripts\python.exe -m build --wheel lib || (
    echo Error: Could not build the client wheel
    pause
    exit /b %ERRORLEVEL%
)
for %%f in (lib\dist\*.whl) do (
    venv\Scripts\pip.exe install --force-reinstall "%%f" || (
        echo Error: Could not install the client wheel
        pause
        exit /b 1
    )
)
echo Built client wheel

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
