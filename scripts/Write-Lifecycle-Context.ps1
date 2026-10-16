# Read-only diagnostics executed by the project's existing lifecycle executor.
$root=Split-Path -Parent $PSScriptRoot
$me=[Security.Principal.WindowsIdentity]::GetCurrent()
$admin=([Security.Principal.WindowsPrincipal]$me).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
$rows=@()
foreach($listener in @(Get-NetTCPConnection -State Listen | Where-Object LocalPort -in 7860,8765,8090,8010)){
    $p=Get-CimInstance Win32_Process -Filter "ProcessId=$($listener.OwningProcess)"
    $command=[string]$p.CommandLine
    $commandHash=[Convert]::ToHexString([Security.Cryptography.SHA256]::HashData([Text.Encoding]::UTF8.GetBytes($command)))
    $command=$command -replace '(?i)(--(?:responses_api_api_key|api_key)\s+)\S+','$1[REDACTED]'
    $rows += [pscustomobject]@{port=$listener.LocalPort;pid=$p.ProcessId;parent_pid=$p.ParentProcessId;created=$p.CreationDate;start_utc=(Get-Process -Id $p.ProcessId).StartTime.ToUniversalTime().ToString('o');path=$p.ExecutablePath;command_line=$command;command_sha256=$commandHash}
}
$value=[pscustomobject]@{at=(Get-Date).ToString('o');executor_pid=$PID;executor_user=$me.Name;administrator=$admin;processes=$rows}
$path=Join-Path $root 'runtime\windows\lifecycle_context_latest.json'
[IO.File]::WriteAllText($path,($value|ConvertTo-Json -Depth 6),[Text.UTF8Encoding]::new($false))
