@echo off
pwsh.exe -NoLogo -NoProfile -File "%~dp0scripts\Start-AI-Girlfriend.ps1"
if errorlevel 1 pause
