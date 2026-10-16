# Run deliberately on a NEW deployment only; no trigger, no highest privilege.
$ErrorActionPreference='Stop'
$root=Split-Path -Parent $PSScriptRoot
if(-not(Get-Command pwsh.exe -ErrorAction SilentlyContinue)){throw 'PowerShell 7 required'}
$entries=@{
 GirlfriendLLM8090_Manual=@('cmd.exe',('/k ""'+(Join-Path $root 'runtime\llm\start_llama_task.bat')+'""'))
 GirlfriendAvatar8010_Manual=@('pwsh.exe',('-NoExit -NoProfile -File "'+(Join-Path $root 'runtime\windows\Run-Girlfriend-Avatar8010.ps1')+'"'))
}
foreach($name in $entries.Keys){
 if(Get-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue){throw "TASK_ALREADY_EXISTS_NO_OVERWRITE $name; inspect owner and action manually"}
}
$user=[Security.Principal.WindowsIdentity]::GetCurrent().Name
foreach($name in $entries.Keys){
 $e=$entries[$name]
 $action=New-ScheduledTaskAction -Execute $e[0] -Argument $e[1] -WorkingDirectory $root
 $principal=New-ScheduledTaskPrincipal -UserId $user -LogonType Interactive -RunLevel Limited
 $settings=New-ScheduledTaskSettingsSet -ExecutionTimeLimit ([TimeSpan]::Zero) -MultipleInstances IgnoreNew -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
 Register-ScheduledTask -TaskName $name -Action $action -Principal $principal -Settings $settings | Out-Null
}
Write-Host 'Registered manual, interactive-only project tasks; no services started.'
