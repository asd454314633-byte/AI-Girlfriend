$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'Runtime-Common.ps1')
$pids=Read-ServicePids
$backend='NONE'
$backendFile=Join-Path $script:NativeRoot 'active_tts_backend.txt'
if(Test-Path -LiteralPath $backendFile){ $backend=(Read-JsonText $backendFile).Trim() }
if(-not $backend){ $backend='NONE' }
$s2sPid=[int]$pids.s2s
$pool=$null
if($s2sPid -gt 0){ try { $pool=Get-HttpJson 'http://127.0.0.1:8765/v1/pool' 3 } catch { $pool=$null } }
$s2sHandler='FAIL'
if($s2sPid -gt 0 -and $pool){
    $proc=Get-CimInstance Win32_Process -Filter "ProcessId=$s2sPid" -ErrorAction SilentlyContinue
    $flag=Get-ProviderSelectionFlag 'TTS'
    $pattern = ('{0}(?:\s+|=){1}(?:\s|$)' -f [regex]::Escape($flag), [regex]::Escape($backend))
    if($pool.size -eq 1 -and $proc -and $proc.CommandLine -match $pattern) { $s2sHandler='PASS' }
}
$ui='FAIL'; if([int]$pids.ui -gt 0 -and (Test-Http 'http://127.0.0.1:7860/api/config')) {$ui='PASS'}
# ---- :8010: read through the ONE lifecycle entry, never off service_pids.json -------
# The retired judgement on this spot was "tracked avatar pid > 0 AND a TCP connect
# succeeds". That pid is the VENV LAUNCHER, and the launcher re-execs into the
# interpreter that really owns the socket, so the pid goes stale while the service is
# alive -- a stale ledger read as "dead" is the bug the supervisor exists to remove.
# The verdict now comes from `runtime\windows\Avatar-Supervisor.ps1 -Action Status -Json`
# (the single status read that the UI, the task wrapper and the switch script all use),
# cross-checked against that supervisor's own ledger
# runtime\windows\avatar_runtime_state.json. -Action Status is a pure read: it starts,
# stops and kills nothing. Every other port in this file keeps its own untouched check.
$repoRoot=Split-Path -Parent $PSScriptRoot
$supervisor=Join-Path $repoRoot 'runtime\windows\Avatar-Supervisor.ps1'
$avatarLedger=Join-Path $repoRoot 'runtime\windows\avatar_runtime_state.json'
$avatar='FAIL'; $avatarNote=''; $aState='NO_RECEIPT'; $aId=''
$ownerPid=0; $launcherPid=0; $listenerPid=0; $supExit='n/a'; $pidSource='none'
$lState='NO_LEDGER'; $lOwner=0; $lLauncher=0; $lListener=0; $lUpdated=''; $lReason=''
if(Test-Path -LiteralPath $avatarLedger){
    try {
        $led=Read-JsonFile $avatarLedger
        $lState=[string]$led.state; $lOwner=[int]$led.owner_pid; $lLauncher=[int]$led.launcher_pid
        $lListener=[int]$led.listener_pid; $lUpdated=[string]$led.updated_at; $lReason=[string]$led.exit_reason
    } catch { $lState="LEDGER_UNPARSEABLE $($_.Exception.Message)" }
} else { $avatarNote="$avatarNote ledger_missing=$avatarLedger" }
if(-not (Test-Path -LiteralPath $supervisor)){
    # Named the same way the switch shim names it, so a grep finds both.
    $avatar="FAIL"; $avatarNote="$avatarNote AVATAR_SUPERVISOR_MISSING path=$supervisor"
} else {
    # Argument vector built as an ARRAY: PS 5.1 drops an empty string token from a
    # native command, so -AvatarId is omitted when there is no id rather than passed ''.
    $childArgs=@('-NoLogo','-NoProfile','-NonInteractive','-ExecutionPolicy','Bypass',
                 '-File',$supervisor,'-Action','Status','-Json')
    $prev=$ErrorActionPreference; $ErrorActionPreference='Continue'
    $call=@(& powershell.exe @childArgs 2>&1)
    $supExit=$LASTEXITCODE; $ErrorActionPreference=$prev
    # The supervisor logs to the host too, so the receipt is the LAST line starting with
    # a brace.
    $answer=@($call | Where-Object { $_ -is [string] -and $_.Trim().StartsWith('{') }) | Select-Object -Last 1
    if(-not $answer){ $avatarNote="$avatarNote AVATAR_STATUS_NO_RESULT exit=$supExit" }
    else {
        try {
            $st=$answer | ConvertFrom-Json
            if($st.PSObject.Properties['status'] -and $st.status){ $st=$st.status }
            $aState=[string]$st.state; $aId=[string]$st.avatar_id; $pidSource='supervisor_status'
            $ownerPid=[int]$st.owner_pid; $launcherPid=[int]$st.launcher_pid; $listenerPid=[int]$st.listener_pid
            if($aState -eq 'RUNNING'){ $avatar='PASS' }
            else { $avatarNote="$avatarNote exit_reason=$([string]$st.exit_reason) last_error=$([string]$st.last_error)" }
        } catch { $avatarNote="$avatarNote AVATAR_STATUS_UNPARSEABLE $($_.Exception.Message)" }
    }
}
$llm='FAIL'; if(Test-Http 'http://127.0.0.1:8090/v1/models') {$llm='PASS'}
# One readiness row per provider, each probed from its OWN manifest health plan
# (http/tcp/in_process_cli_probe). Nothing here lists qwen/higgs/fish by name.
$rows=New-Object System.Collections.Generic.List[object]
$activeCount=0
foreach($id in (Get-ProviderIds 'TTS' -EnabledOnly)){
    $spec=Get-ProviderSpec 'TTS' $id
    $verdict=Test-ProviderReady $spec $s2sPid $pool
    if($verdict -eq 'RUNNING'){ $activeCount++ }
    $rows.Add([pscustomobject]@{ Id=$id; Verdict=$verdict })
}
$cap=Get-ProviderMaxResident 'TTS'
Write-Host "8090 LLM       $llm"
Write-Host "8765 VOICE     $s2sHandler"
Write-Host "7860 UI        $ui"
Write-Host "8010 Avatar    $avatar"
# The three PIDs the supervisor keeps distinct, spelled out, and a zero is said as zero:
# owner_pid = the powershell holding/waiting on the child (the task wrapper),
# launcher_pid = the venv python the launch returned, listener_pid = who really owns
# the socket.
Write-Host ('8010 PIDS       owner_pid={0} launcher_pid={1} listener_pid={2} (source={3}, supervisor_exit={4})' -f `
    $ownerPid, $launcherPid, $listenerPid, $pidSource, $supExit)
Write-Host ('8010 LEDGER      {0} owner_pid={1} launcher_pid={2} listener_pid={3} updated_at={4} exit_reason={5}' -f `
    $lState, $lOwner, $lLauncher, $lListener, $lUpdated, $lReason)
Write-Host ('8010 DETAIL      state={0} id={1}{2}' -f $aState, $aId, $avatarNote)
Write-Host "ACTIVE TTS     $backend"
foreach($r in $rows){ Write-Host ('{0} WORKER  {1}' -f ([string]$r.Id).ToUpperInvariant(), $r.Verdict) }
Write-Host "ACTIVE_MAIN_TTS_COUNT = $activeCount"
if($cap -gt 0 -and $activeCount -gt $cap){
    Write-Host "ACTIVE_MAIN_TTS_INVARIANT = FAIL (max_resident_provider_instances=$cap)"
} else {
    Write-Host "ACTIVE_MAIN_TTS_INVARIANT = OK (max_resident_provider_instances=$cap)"
}
Write-Host 'WSL DEPENDENCY = NO (Windows Native launch/runtime path)'
Write-Host "TTS PROCESS    $($pids.s2s)"
try { & nvidia-smi.exe --query-gpu=memory.used,memory.total --format=csv,noheader } catch { Write-Host 'VRAM           unavailable' }
