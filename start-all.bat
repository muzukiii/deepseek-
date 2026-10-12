@echo off
chcp 65001 >nul
title AstraBot Launcher
setlocal

set "BOT_DIR=%~dp0"
set "SL_DIR=C:\Users\li204\Documents\git\tools\SnowLuma"

echo ============================================
echo   AstraBot one-click launcher
echo ============================================
echo.

echo [1/3] Checking SnowLuma (WebUI 5099 / OneBot 3001)...
netstat -ano | findstr ":5099" | findstr "LISTENING" >nul
if errorlevel 1 (
    if exist "%SL_DIR%\node.exe" (
        echo       Not running. Starting SnowLuma...
        start "SnowLuma" /D "%SL_DIR%" "%SL_DIR%\node.exe" "%SL_DIR%\index.mjs"
        echo       Waiting for SnowLuma to initialize...
        ping -n 13 127.0.0.1 >nul
    ) else (
        echo       [ERROR] SnowLuma not found at %SL_DIR%
        pause
        exit /b 1
    )
) else (
    echo       [OK] SnowLuma already running
)

echo.
echo [2/3] Waiting for OneBot WebSocket on 3001...
set /a tries=0
:waitloop
netstat -ano | findstr ":3001" | findstr "LISTENING" >nul
if not errorlevel 1 goto portready
set /a tries+=1
if %tries% GEQ 25 goto porttimeout
ping -n 3 127.0.0.1 >nul
goto waitloop

:portready
echo       [OK] OneBot 3001 is ready
goto launch

:porttimeout
echo       [WARN] Timed out waiting for 3001
echo       Make sure QQ is logged in and SnowLuma injection succeeded
echo       AstraBot will keep retrying anyway

:launch
echo.
echo [3/3] Starting AstraBot on port 8080...
echo.
echo ============================================
echo   Ready. Press Ctrl+C to stop AstraBot.
echo   SnowLuma runs in its own window.
echo ============================================
echo.

cd /d "%BOT_DIR%"
".venv-win\Scripts\python.exe" bot.py

echo.
echo --------------------------------------------
echo AstraBot exited with code %errorlevel%
pause
