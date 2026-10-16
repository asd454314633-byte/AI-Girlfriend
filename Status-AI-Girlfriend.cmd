@echo off
pwsh.exe -NoLogo -NoProfile -File "%~dp0scripts\Status-AI-Girlfriend.ps1"
if errorlevel 1 pause
