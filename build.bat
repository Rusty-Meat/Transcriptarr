@echo off
REM ---------------------------------------------------------------------------
REM Build Transcriptarr.exe and update.exe via PyInstaller.
REM
REM Build order matters:
REM   1. update.exe   (built from Update.spec)
REM   2. Transcriptarr.exe (built from Transcriptarr.spec, which bundles
REM      dist\update.exe as a data file so the wizard can extract it
REM      into the user's install folder during setup).
REM
REM Output:
REM   dist\update.exe              (~30 MB, the updater/repair tool)
REM   dist\Transcriptarr.exe       (~60 MB - includes update.exe inside)
REM ---------------------------------------------------------------------------

setlocal ENABLEEXTENSIONS

set "VENV=C:\Second Brain\Second Brain\.venv"
set "PY=%VENV%\Scripts\python.exe"

if not exist "%PY%" (
    echo Could not find venv Python at:
    echo   %PY%
    echo Edit build.bat and update the VENV line.
    pause
    exit /b 1
)

REM Make sure pyinstaller is available
"%PY%" -m PyInstaller --version >nul 2>nul
if errorlevel 1 (
    echo Installing PyInstaller...
    "%PY%" -m pip install pyinstaller
)

if not exist transcriptarr.ico (
    echo.
    echo ERROR: transcriptarr.ico is missing.
    echo Run make_icon.py first to generate it.
    pause
    exit /b 1
)

echo === Cleaning previous builds ===
if exist build rmdir /s /q build
if exist dist rmdir /s /q dist

echo.
echo === Building update.exe (step 1 of 2) ===
"%PY%" -m PyInstaller Update.spec --clean --noconfirm
if errorlevel 1 (
    echo.
    echo update.exe build failed. Check output above.
    pause
    exit /b 1
)

if not exist dist\update.exe (
    echo.
    echo Build reported success but dist\update.exe is missing. Aborting.
    pause
    exit /b 1
)

echo.
echo === Building Transcriptarr.exe (step 2 of 2) ===
"%PY%" -m PyInstaller Transcriptarr.spec --clean --noconfirm
if errorlevel 1 (
    echo.
    echo Transcriptarr.exe build failed. Check output above.
    pause
    exit /b 1
)

echo.
echo === Done ===
echo Outputs:
echo   %~dp0dist\update.exe
echo   %~dp0dist\Transcriptarr.exe
echo.
dir /b dist\*.exe
echo.
pause
endlocal
