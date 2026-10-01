@echo off
chcp 65001 >nul
title XK Assistant
cd /d "%~dp0"

echo ============================================================
echo   Nan Yuan Course Assistant
echo ============================================================
echo.

REM Use project venv first, then system python
set "PY=%~dp0.venv\Scripts\python.exe"
if not exist "%PY%" set "PY=python"

"%PY%" --version >nul 2>&1
if errorlevel 1 (
    echo [ERROR] Python not found. Install Python 3.10+ and add to PATH.
    echo         https://www.python.org/downloads/
    pause
    exit /b 1
)

REM Install dependencies on first run
"%PY%" -c "import fastapi, uvicorn, requests" >nul 2>&1
if errorlevel 1 (
    echo [INFO] Installing dependencies (about 1 min)...
    "%PY%" -m pip install -r requirements.txt
    if errorlevel 1 (
        echo [ERROR] Dependency install failed. Check network and retry.
        pause
        exit /b 1
    )
)

echo [START] Service starting, browser will open automatically...
echo.

REM Open browser after 2s delay
start "" cmd /c "timeout /t 2 >nul & start http://127.0.0.1:8720"

REM --real: force real JWC
"%PY%" serve.py --real --port 8720

pause
