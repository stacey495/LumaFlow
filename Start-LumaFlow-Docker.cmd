@echo off
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0Run.ps1" -Action docker-demo -Mode docker
if errorlevel 1 pause
