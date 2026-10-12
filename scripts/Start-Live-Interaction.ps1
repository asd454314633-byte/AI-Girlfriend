param([switch]$Enable)
$ErrorActionPreference='Stop'
$root=Split-Path -Parent $PSScriptRoot
$python=Join-Path $root 'runtime\core\venv\Scripts\python.exe'
$cfg=Get-Content (Join-Path $root 'runtime\live\config.json') -Raw -Encoding UTF8 | ConvertFrom-Json
$port=[int]$cfg.api_port
$listener=Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue
if($listener){
    $p=Get-CimInstance Win32_Process -Filter "ProcessId=$($listener[0].OwningProcess)"
    if($p.CommandLine -notlike '*runtime.live.service:app*'){throw 'LIVE_PORT_FOREIGN_OWNER'}
}else{
    $out=Join-Path $root 'logs\live_service.stdout.log'
    $err=Join-Path $root 'logs\live_service.stderr.log'
    Start-Process -FilePath $python -ArgumentList @('-u','-m','uvicorn','runtime.live.service:app','--host','127.0.0.1','--port',"$port") -WorkingDirectory $root -WindowStyle Hidden -RedirectStandardOutput $out -RedirectStandardError $err | Out-Null
    $deadline=(Get-Date).AddSeconds(20)
    do {
        & curl.exe --max-time 5 --silent --fail --noproxy '*' "http://127.0.0.1:$port/health" 2>$null | Out-Null
        if($LASTEXITCODE -eq 0){break}
        Start-Sleep -Milliseconds 500
    }while((Get-Date) -lt $deadline)
    if($LASTEXITCODE -ne 0){throw "LIVE_START_FAILED see $err"}
}
if($Enable){
    $body=Join-Path $root 'runtime\live\enable-request.json'
    [System.IO.File]::WriteAllText($body,'{"enabled":true}')
    & curl.exe --max-time 5 --silent --show-error --fail --noproxy '*' -H 'Content-Type: application/json' --data-binary "@$body" "http://127.0.0.1:$port/control"
    if($LASTEXITCODE -ne 0){throw 'LIVE_ON_FAILED'}
}
Write-Host "Live Interaction: http://127.0.0.1:$port/status; connect the existing AI Girlfriend UI on 7860."
