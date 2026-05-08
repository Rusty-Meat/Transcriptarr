@echo off
REM Diagnose why Transcriptarr is using CPU instead of GPU.
REM Uses the same Python-finding logic as run.bat.

setlocal ENABLEEXTENSIONS

set "VENV_DIR=C:\Second Brain\Second Brain"
set "PY="

if exist "%VENV_DIR%\.venv\Scripts\python.exe"   set "PY=%VENV_DIR%\.venv\Scripts\python.exe"
if not defined PY if exist "%VENV_DIR%\venv\Scripts\python.exe"    set "PY=%VENV_DIR%\venv\Scripts\python.exe"
if not defined PY if exist "%VENV_DIR%\env\Scripts\python.exe"     set "PY=%VENV_DIR%\env\Scripts\python.exe"
if not defined PY if exist "%VENV_DIR%\Scripts\python.exe"         set "PY=%VENV_DIR%\Scripts\python.exe"
if not defined PY if exist "%VENV_DIR%\python.exe"                 set "PY=%VENV_DIR%\python.exe"
if not defined PY (
    where python >nul 2>nul && set "PY=python"
)

if not defined PY (
    echo Could not find Python. Edit VENV_DIR in run.bat / diagnose.bat.
    pause
    exit /b 1
)

echo === Python being used ===
echo %PY%
echo.

echo === nvidia-smi (driver + GPU sanity check) ===
where nvidia-smi >nul 2>nul
if %errorlevel%==0 (
    nvidia-smi
) else (
    echo nvidia-smi not on PATH. Either no NVIDIA driver installed, or PATH is missing it.
)
echo.

echo === Torch / CUDA report ===
"%PY%" -c "import sys, torch; print('python      :', sys.executable); print('torch       :', torch.__version__); print('torch.cuda  :', torch.version.cuda); print('cuda avail  :', torch.cuda.is_available()); print('device count:', torch.cuda.device_count()); [print('device', i, ':', torch.cuda.get_device_name(i)) for i in range(torch.cuda.device_count())]"

echo.
echo === If 'torch' shows '+cpu' or 'torch.cuda' is None, you have the CPU wheel. ===
echo === Reinstall with:                                                          ===
echo pip uninstall -y torch torchaudio
echo pip install torch==2.5.1 torchaudio==2.5.1 --index-url https://download.pytorch.org/whl/cu121
echo.
pause
endlocal
