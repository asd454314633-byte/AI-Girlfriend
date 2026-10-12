param([switch]$StopService)
$ErrorActionPreference='Stop'
$root=Split-Path -Parent $PSScriptRoot
$cfg=Get-Content (Join-Path $root 'runtime\live\config.json') -Raw -Encoding UTF8 | ConvertFrom-Json
$port=[int]$cfg.api_port
$body=Join-Path $root 'runtime\live\disable-request.json'
[System.IO.File]::WriteAllText($body,'{"enabled":false}')
& curl.exe --max-time 5 --silent --show-error --fail --noproxy '*' -H 'Content-Type: application/json' --data-binary "@$body" "http://127.0.0.1:$port/control"
if($LASTEXITCODE -ne 0){throw 'LIVE_OFF_FAILED'}
if($StopService){
    # Give the frontend time to cancel its own live response before shutting down.
    Start-Sleep -Seconds 2
    $conn=Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue | Select-Object -First 1
    if($conn){
        $p=Get-CimInstance Win32_Process -Filter "ProcessId=$($conn.OwningProcess)"
        if($p.CommandLine -notlike '*runtime.live.service:app*'){throw 'LIVE_PORT_FOREIGN_OWNER'}
        Stop-Process -Id $p.ProcessId
    }
}
