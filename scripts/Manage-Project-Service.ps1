param([ValidateSet('ui','live')][string]$Service='ui',
      [ValidateSet('start','status','stop','restart')][string]$Action='status')
$ErrorActionPreference='Stop'
$root=Split-Path -Parent $PSScriptRoot
$python=Join-Path $root 'runtime\core\venv\Scripts\python.exe'
$dir=Join-Path $root 'runtime\ui7860_manager'
$runner=Join-Path $dir 'manager.py'
$port=if($Service -eq 'ui'){7860}else{8781}
$mode=if($Service -eq 'ui'){'production'}else{'live'}
$pointer=Join-Path $root "runtime\windows\${Service}_manager_current.json"
function Invoke-Manager([string[]]$ManagerArgs){
    $lines=@(& $python -u $runner @ManagerArgs)
    $code=$LASTEXITCODE
    $last=$lines | Select-Object -Last 1
    $answer=$last | ConvertFrom-Json
    if($code -ne 0 -or -not $answer.ok){throw "MANAGED_${Service}_${Action}_HOLD: $last"}
    return $answer
}
function Current-Record {
    if(-not (Test-Path -LiteralPath $pointer)){throw "UNMANAGED_SERVICE_${port}_NO_ADOPTION"}
    $p=Get-Content -LiteralPath $pointer -Raw -Encoding UTF8 | ConvertFrom-Json
    return [string]$p.record
}
function Save-UiCompatibility($Answer){
    if($Service -ne 'ui'){return}
    $path=Join-Path $root 'runtime\windows\service_pids.json'
    $state=Get-Content -LiteralPath $path -Raw -Encoding UTF8 | ConvertFrom-Json
    $state.ui=[int]$Answer.identity.pid
    $proc=Get-CimInstance Win32_Process -Filter "ProcessId=$($state.ui)"
    $owner=[pscustomobject]@{pid=$state.ui;startTimeUtc=(Get-Process -Id $state.ui).StartTime.ToUniversalTime().ToString('o');executablePath=$proc.ExecutablePath;commandLine=$proc.CommandLine;createdBy='UI7860_MANAGER_V1';role='listener';listenerAuthority='runtime/windows/ui_manager_current.json';run_id=$Answer.identity.run_id}
    $state.owners.ui=$owner
    $temp=$path+'.'+[guid]::NewGuid().ToString('N')+'.tmp'
    [IO.File]::WriteAllText($temp,($state|ConvertTo-Json -Depth 7),[Text.UTF8Encoding]::new($false))
    Move-Item -LiteralPath $temp -Destination $path -Force
}
if($Action -eq 'restart'){
    & $PSCommandPath -Service $Service -Action stop
    if(-not $?){throw 'RESTART_STOP_FAILED'}
    & $PSCommandPath -Service $Service -Action start
    exit
}
if($Action -eq 'start'){
    $listening=Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue
    if($listening){
        $record=Current-Record
        $answer=Invoke-Manager -ManagerArgs @('status','--record',$record)
        Save-UiCompatibility $answer
        $answer | ConvertTo-Json -Depth 12
        exit
    }
    $state=Join-Path $dir ('state\'+$Service+'_'+[guid]::NewGuid().ToString('N'))
    $argv=@('start','--mode',$mode,'--port',[string]$port,'--state',$state)
    if($Service -eq 'ui'){$argv+='--maintenance-confirmed'}
    $answer=Invoke-Manager $argv
    $temp=$pointer+'.'+[guid]::NewGuid().ToString('N')+'.tmp'
    [IO.File]::WriteAllText($temp,(@{record=$answer.record;service=$Service;run_id=$answer.identity.run_id}|ConvertTo-Json),[Text.UTF8Encoding]::new($false))
    Move-Item -LiteralPath $temp -Destination $pointer -Force
    Save-UiCompatibility $answer
    $answer | ConvertTo-Json -Depth 12
    exit
}
$record=Current-Record
if($Service -eq 'live' -and $Action -eq 'stop'){
    # OFF first stops collector admissions. Existing in-flight reply still blocks
    # the authenticated shutdown until it finishes/cancels via the frontend.
    $handler=[Net.Http.HttpClientHandler]::new();$handler.UseProxy=$false
    $client=[Net.Http.HttpClient]::new($handler)
    try {
        $content=[Net.Http.StringContent]::new('{"enabled":false}',[Text.Encoding]::UTF8,'application/json')
        $response=$client.PostAsync('http://127.0.0.1:8781/control',$content).GetAwaiter().GetResult()
        $response.EnsureSuccessStatusCode() | Out-Null
    }finally{$client.Dispose();$handler.Dispose()}
}
$answer=Invoke-Manager -ManagerArgs @($Action,'--record',$record,'--timeout','15')
$answer | ConvertTo-Json -Depth 12
