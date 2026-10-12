@echo off
chcp 65001 >nul
title AstraBot
cd /d "%~dp0"

echo ============================================
echo   AstraBot starting...
echo ============================================
echo.

if not exist ".venv-win\Scripts\python.exe" (
    echo [ERROR] venv not found: .venv-win
    pause
    exit /b 1
)

if not exist ".env" (
    echo [ERROR] .env not found
    pause
    exit /b 1
)

echo [1/2] Checking SnowLuma on port 3001...
netstat -ano | findstr ":3001" | findstr "LISTENING" >nul
if errorlevel 1 (
    echo       [WARN] port 3001 not listening - SnowLuma may be down
    echo       AstraBot will keep retrying and connect automatically
) else (
    echo       [OK] SnowLuma is running
)

echo.
echo [2/2] Starting AstraBot on port 8080...
echo       Press Ctrl+C to stop
echo.
echo --------------------------------------------

".venv-win\Scripts\python.exe" bot.py

echo.
echo --------------------------------------------
echo AstraBot exited with code %errorlevel%
pause
