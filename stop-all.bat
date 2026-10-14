@echo off
title Stop AstraBot
setlocal enabledelayedexpansion

echo ============================================
echo   Stopping AstraBot / SnowLuma
echo ============================================
echo.

REM --- AstraBot: ??????????????? python ???? ---
set "BOTPID="
for /f "tokens=5" %%a in ('netstat -ano ^| findstr ":8080" ^| findstr "LISTENING"') do set "BOTPID=%%a"

if defined BOTPID (
    echo [1/2] Stopping AstraBot ^(PID !BOTPID!^)...
    taskkill /PID !BOTPID! /T /F >nul 2>&1
    if errorlevel 1 (
        echo       [FAIL] try running as Administrator
    ) else (
        echo       [OK] AstraBot stopped
    )
) else (
    echo [1/2] AstraBot is not running
)

REM --- ????? bot.py ?????????????????---
for /f "skip=1 tokens=2 delims=," %%p in ('wmic process where "name='python.exe'" get processid^,commandline /format:csv 2^>nul ^| findstr "bot.py"') do (
    taskkill /PID %%p /T /F >nul 2>&1
)

REM --- SnowLuma ---
set "SLPID="
for /f "tokens=5" %%a in ('netstat -ano ^| findstr ":5099" ^| findstr "LISTENING"') do set "SLPID=%%a"

if defined SLPID (
    echo [2/2] Stopping SnowLuma ^(PID !SLPID!^)...
    taskkill /PID !SLPID! /T /F >nul 2>&1
    if errorlevel 1 (
        echo       [FAIL] try running as Administrator
    ) else (
        echo       [OK] SnowLuma stopped
    )
) else (
    echo [2/2] SnowLuma is not running
)

echo.
echo ============================================
echo   Done. QQ stays logged in.
echo ============================================
ping -n 3 127.0.0.1 >nul