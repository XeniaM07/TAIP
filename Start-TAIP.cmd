@echo off
setlocal
title TAIP
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0deployment\windows\start.ps1" %*
set "taip_exit=%errorlevel%"
if not "%taip_exit%"=="0" pause
exit /b %taip_exit%
