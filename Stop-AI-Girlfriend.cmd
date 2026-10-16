@echo off
pwsh.exe -NoLogo -NoProfile -File "%~dp0scripts\Stop-AI-Girlfriend.ps1"
if errorlevel 1 pause
