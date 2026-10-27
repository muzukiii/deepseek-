@echo off
setlocal enabledelayedexpansion
title AstraBot 控制台
cd /d "%~dp0"
set "CTL=%~dp0tools\launcher\astrabot_ctl.ps1"
set "PS=powershell -NoProfile -ExecutionPolicy Bypass -File"
set "CLI="

if /i "%~1"=="all" set "CLI=1" & goto :do_all
if /i "%~1"=="panel" set "CLI=1" & goto :do_panel
if /i "%~1"=="open" set "CLI=1" & goto :do_open
if /i "%~1"=="status" set "CLI=1" & goto :do_status
if /i "%~1"=="stop" set "CLI=1" & goto :do_stop
if /i "%~1"=="stopall" set "CLI=1" & goto :do_stopall

:menu
cls
call :banner
echo   1  启动全部   桥接检查 + 启动 Bot + 启动面板并打开
echo   2  只启动面板 不启动 Bot
echo   3  打开面板
echo   4  查看状态
echo   5  停止 Bot 与面板   桥接保持在线
echo   6  停止全部   含 SnowLuma 桥接
echo   0  退出
echo.
set "SEL="
set /p "SEL=请选择 [0-6]: "
if not defined SEL goto :menu
if "%SEL%"=="1" goto :do_all
if "%SEL%"=="2" goto :do_panel
if "%SEL%"=="3" goto :do_open
if "%SEL%"=="4" goto :do_status
if "%SEL%"=="5" goto :do_stop
if "%SEL%"=="6" goto :do_stopall
if "%SEL%"=="0" exit /b 0
echo.
echo 无效选择：%SEL%
ping -n 2 127.0.0.1 >nul
goto :menu

:banner
echo ============================================
echo   AstraBot 控制台
echo ============================================
echo.
exit /b 0

:do_all
call :banner
%PS% "%CTL%" -Action start-all
goto :done

:do_panel
call :banner
%PS% "%CTL%" -Action start-panel
%PS% "%CTL%" -Action open-panel
goto :done

:do_open
call :banner
%PS% "%CTL%" -Action open-panel
goto :done

:do_status
call :banner
%PS% "%CTL%" -Action status
goto :done

:do_stop
call :banner
%PS% "%CTL%" -Action stop
goto :done

:do_stopall
call :banner
echo 这会同时停掉 SnowLuma 桥接，Bot 将离线。
set "CONFIRM="
set /p "CONFIRM=确认停止全部？(y/N): "
if /i not "%CONFIRM%"=="y" goto :menu
%PS% "%CTL%" -Action stop-all
goto :done

:done
if defined CLI exit /b 0
echo.
pause
goto :menu
