$ErrorActionPreference='Stop'
& (Join-Path $PSScriptRoot 'Manage-Project-Service.ps1') -Service live -Action stop
if(-not $?){throw 'LIVE_MANAGED_STOP_FAILED'}