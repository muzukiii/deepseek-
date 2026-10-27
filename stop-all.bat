@echo off
setlocal enabledelayedexpansion
title Í£Ö¹ AstraBot
cd /d "%~dp0"
set "CTL=%~dp0tools\launcher\astrabot_ctl.ps1"
set "PS=powershell -NoProfile -ExecutionPolicy Bypass -File"

echo ============================================
echo   Í£Ö¹ AstraBot / SnowLuma
echo ============================================
echo.

%PS% "%CTL%" -Action stop-all

echo.
echo ============================================
echo   Íê³É¡£QQ ÈÔ±£³ÖµÇÂ¼¡£
echo ============================================
ping -n 3 127.0.0.1 >nul
