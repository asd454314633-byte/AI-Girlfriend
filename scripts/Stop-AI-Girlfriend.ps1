$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'Runtime-Common.ps1')
$log='stop.log'
Write-Host '正在停止本项目拥有的 Windows 原生服务…'
$pids=Read-ServicePids
try { Stop-NativeProcess 'ui' $script:UiPython 'uvicorn server:app --host 127.0.0.1 --port 7860' } catch { Write-RuntimeLog $log "UI_STOP_WARNING $($_.Exception.Message)" }
# :8010 goes through the supervisor's Stop action, as a CHILD PROCESS (the same reason
# Start-AI-Girlfriend gives: this file uses $taskName for the 8090 task and PowerShell
# variable names are case-insensitive, so dot-sourcing the supervisor into this scope
# could aim an avatar action at the LLM's task). What that buys over the bare
# Stop-NativeProcess call that used to sit here: it also stops the task wrapper that
# holds the service, it proves ownership per process instead of by image name, it
# reaps the re-exec'd interpreter that outlives its launcher (the old call no-ops once
# service_pids.json['avatar'] is stale), it MEASURES that the port really went away, and
# it records STOPPED + exit_reason in runtime\windows\avatar_runtime_state.json -- so a
# later Start cannot mistake a deliberate stop for a crash.
$avatarSupervisor = Join-Path $script:NativeRoot 'Avatar-Supervisor.ps1'
if (-not (Test-Path -LiteralPath $avatarSupervisor)) {
    Write-RuntimeLog $log "AVATAR_STOP_SKIPPED reason=supervisor_missing path=$avatarSupervisor"
} else {
    $stopOut = @(powershell.exe -NoLogo -NoProfile -NonInteractive -ExecutionPolicy Bypass -File $avatarSupervisor -Action Stop -Json 2>&1)
    $stopAnswer = @($stopOut | Where-Object { $_ -is [string] -and $_.Trim().StartsWith('{') }) | Select-Object -Last 1
    if ($LASTEXITCODE -ne 0) {
        Write-RuntimeLog $log "AVATAR_STOP_WARNING exit=$LASTEXITCODE answer=$stopAnswer"
    } else {
        Write-RuntimeLog $log "AVATAR_STOP_OK answer=$stopAnswer"
    }
}
try { Stop-S2S } catch { Write-RuntimeLog $log "S2S_STOP_WARNING $($_.Exception.Message)" }
# Provider-agnostic teardown: iterate every enabled external-worker TTS provider
# declared in the manifest and stop it. No .ps1 names a provider or its exe path.
foreach($spec in (Get-ExternalProviderWorkers 'TTS')) {
    $wkPid = [string]$spec.worker.pid_key
    try { Stop-ProviderWorker $spec } catch { Write-RuntimeLog $log "WORKER_STOP_WARNING provider=$wkPid $($_.Exception.Message)" }
}
$clearLlmOwner=$false
if($pids.llmStartedByStart -and $pids.llmOwnership -and $pids.llmOwnership.owned) {
    $owner=$pids.llmOwnership
    $ownedPid=[int]$owner.pid
    $proc=Get-CimInstance Win32_Process -Filter "ProcessId=$ownedPid" -ErrorAction SilentlyContinue
    if($proc) {
        if($proc.Name -ne 'llama-server.exe' -or $proc.ExecutablePath -ine [string]$owner.executablePath -or
           $proc.CommandLine -cne [string]$owner.commandLine -or (Get-ProcessStartStamp $ownedPid) -ne [string]$owner.startTimeUtc) {
            Write-RuntimeLog $log "LLM_STOP_SKIPPED_OWNERSHIP_MISMATCH pid=$ownedPid; leaving process untouched"
        } else {
            $taskName=[string]$owner.scheduledTask
            $task=if($taskName){Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue}else{$null}
            if($task -and $task.State -eq 'Running') { Stop-ScheduledTask -TaskName $taskName }
            Stop-Process -Id $ownedPid -Force -ErrorAction SilentlyContinue
            try { Wait-Process -Id $ownedPid -Timeout 15 -ErrorAction Stop } catch {}
            if(Get-Process -Id $ownedPid -ErrorAction SilentlyContinue) { Write-RuntimeLog $log "LLM_STOP_FAILED pid=$ownedPid" }
            else { Write-RuntimeLog $log "LLM_STOPPED_OWNED_PID pid=$ownedPid startTimeUtc=$($owner.startTimeUtc)" }
        }
    } else { Write-RuntimeLog $log "LLM_OWNER_ALREADY_EXITED pid=$ownedPid"; $clearLlmOwner=$true }
}
if($ownedPid -gt 0 -and -not (Get-Process -Id $ownedPid -ErrorAction SilentlyContinue)) { $clearLlmOwner=$true }
$pids=Read-ServicePids
if($clearLlmOwner) { Set-ObjectValue $pids 'llmStartedByStart' $false; Set-ObjectValue $pids 'llmOwnership' $null }
Save-ServicePids $pids
Write-RuntimeLog $log 'STOP_NATIVE_COMPLETE; active TTS choice preserved; WSL fallback was not touched'
Write-Host '已停止本项目受控的 Windows 服务；WSL 回滚基线未操作。'
