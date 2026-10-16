param([switch]$NoBrowser)
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'Runtime-Common.ps1')
# The digital-human launch body lives in its own library so the cold start and the
# switch run the same one. Dot-sourced (same process, same scope): $script: vars,
# $env: restore and $ErrorActionPreference all stay exactly as they were.
. (Join-Path $PSScriptRoot 'Avatar-Launch-Common.ps1')
$log = Join-Path $script:LogRoot 'startup.log'
$lock = New-Object System.Threading.Mutex($false,'Global\AIGirlfriendNativeStartup')
$locked=$false
$stage='init'
$taskName='GirlfriendLLM8090_Manual'
$startedLlmByThisRun=$false

function Invoke-AvatarSupervisor {
    # One call to the ONE lifecycle entry for :8010, as a CHILD PROCESS on purpose:
    # this file keeps $taskName for the 8090 LLM task (line below), and dot-sourcing
    # Avatar-Supervisor.ps1 would run it in this same script scope -- PowerShell
    # variable names are case-insensitive, so its $script:TaskName and this $taskName
    # are the same variable and an avatar action could be aimed at the LLM's task.
    # A separate process also means the global supervisor mutex serialises us against
    # the UI and the task wrapper instead of three callers racing in one session.
    param([string]$SupervisorAction, [string]$ForId = '')
    $supervisor = Join-Path $script:NativeRoot 'Avatar-Supervisor.ps1'
    if (-not (Test-Path -LiteralPath $supervisor)) { throw "AVATAR_SUPERVISOR_MISSING path=$supervisor" }
    # PS 5.1 drops an EMPTY string from a native command's argument list: passing
    # '-AvatarId ''' used to arrive as '-AvatarId -Json', the child then failed
    # parameter binding with exit=1 and no JSON — on every id-less call, which is
    # exactly the cold start's Ensure. An absent id is therefore omitted, not empty.
    $childArgs = @('-NoLogo', '-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass',
                  '-File', $supervisor, '-Action', $SupervisorAction)
    if ($ForId) { $childArgs += @('-AvatarId', $ForId) }
    $childArgs += '-Json'
    $call = @(& powershell.exe @childArgs 2>&1)
    $exit = $LASTEXITCODE
    # Write-RuntimeLog inside the supervisor prints to stdout too, so the JSON answer is
    # the last line that actually starts with a brace.
    $answer = @($call | Where-Object { $_ -is [string] -and $_.Trim().StartsWith('{') }) | Select-Object -Last 1
    if (-not $answer) {
        $tail = (@($call | ForEach-Object { [string]$_ }) -join ' | ')
        throw "AVATAR_$($SupervisorAction.ToUpper())_NO_RESULT exit=$exit output=$tail"
    }
    $result = $answer | ConvertFrom-Json
    $st = $result.status
    Write-RuntimeLog 'startup.log' ('AVATAR_{0} exit={1} verdict={2} state={3} id={4} launcher_pid={5} listener_pid={6} owner_pid={7}' -f `
        $SupervisorAction.ToUpper(), $exit, [string]$result.verdict, [string]$st.state, [string]$st.avatar_id, `
        [string]$st.launcher_pid, [string]$st.listener_pid, [string]$st.owner_pid)
    if ($exit -ne 0) { throw "AVATAR_$($SupervisorAction.ToUpper())_FAILED exit=$exit verdict=$($result.verdict) error=$($result.error)$($st.last_error)" }
    return $result
}

function Start-AvatarNative {
    # Delegated, not copied -- and now one level deeper than before: the supervisor's
    # Ensure action is the whole request ("make :8010 run the person the pointer names,
    # idempotently"). It replaces the old bare Start-GirlfriendAvatarNative call, which
    # could not see that a healthy service was already up: the tracked pid in
    # service_pids.json is the VENV LAUNCHER, so once the launcher exited the guard at
    # the call site below used to launch a second instance onto a busy port. Ensure
    # answers ALREADY_RUNNING and kills nothing.
    $result = Invoke-AvatarSupervisor 'Ensure'
    if ([string]$result.status.state -ne 'RUNNING') { throw "AVATAR_NOT_RUNNING state=$($result.status.state) error=$($result.status.last_error)" }
    return $result
}

function Start-UiNative {
    & (Join-Path $PSScriptRoot 'Start-UI7860.ps1')
    if(-not $?){throw 'UI_MANAGED_START_FAILED'}
}

try {
    $locked=$lock.WaitOne(0)
    if(-not $locked) { Write-Host '启动已在进行中。'; exit 0 }
    Write-RuntimeLog 'startup.log' 'START_NATIVE_BEGIN'

    $stage='8090'
    Write-Host '[1/5] 正在检查聊天模型 8090…'
    if(-not (Test-Http 'http://127.0.0.1:8090/v1/models')) {
        $task=Get-ScheduledTask -TaskName $taskName -ErrorAction Stop
        if($task.State -ne 'Running') {
            Start-ScheduledTask -TaskName $taskName
            $startedLlmByThisRun=$true
        }
        $deadline=(Get-Date).AddSeconds(300)
        while((Get-Date) -lt $deadline -and -not (Test-Http 'http://127.0.0.1:8090/v1/models')) { Start-Sleep -Seconds 3 }
        if(-not (Test-Http 'http://127.0.0.1:8090/v1/models')) { throw 'LLM_8090_HEALTH_TIMEOUT' }
        $listener=Get-NetTCPConnection -LocalPort 8090 -State Listen -ErrorAction SilentlyContinue | Select-Object -First 1
        if(-not $listener) { throw 'LLM_HEALTHY_BUT_NO_8090_LISTENER; ownership was not recorded' }
        $llmProc=Get-CimInstance Win32_Process -Filter "ProcessId=$($listener.OwningProcess)" -ErrorAction SilentlyContinue
        if(-not $llmProc -or $llmProc.Name -ne 'llama-server.exe' -or $llmProc.CommandLine -notmatch '--port\s+8090' -or $llmProc.CommandLine -notlike "*$($script:AppRoot)*") {
            throw 'LLM_STARTED_BUT_OWNERSHIP_UNVERIFIED; refusing to claim Stop ownership'
        }
        if($startedLlmByThisRun) {
            $startedUtc=Get-ProcessStartStamp ([int]$llmProc.ProcessId)
            $pids=Read-ServicePids
            Set-ObjectValue $pids 'llmStartedByStart' $true
            Set-ObjectValue $pids 'llmOwnership' ([pscustomobject]@{
                owned=$true; pid=[int]$llmProc.ProcessId; startTimeUtc=$startedUtc
                executablePath=[string]$llmProc.ExecutablePath; commandLine=[string]$llmProc.CommandLine
                scheduledTask=$taskName; createdBy='Start-AI-Girlfriend.ps1'
            })
            Save-ServicePids $pids
            Write-RuntimeLog 'startup.log' "LLM_OWNERSHIP_CAPTURED pid=$($llmProc.ProcessId) startTimeUtc=$startedUtc"
        } else { Write-RuntimeLog 'startup.log' "LLM_NOT_OWNED_BY_THIS_START pid=$($llmProc.ProcessId)" }
    }
    Write-Host '8090 聊天模型已就绪。'

    $stage='8765'
    Write-Host '[2/5] 正在启动语音核心并加载当前选定的 TTS…'
    $backendFile=Join-Path $script:NativeRoot 'active_tts_backend.txt'
    if(-not (Test-Path -LiteralPath $backendFile)) { $backend=Get-ProviderDefault 'TTS' }
    else {
        $backend=(Read-JsonText $backendFile).Trim()
        if(-not (Get-ProviderSpec 'TTS' $backend)) { throw "ACTIVE_TTS_BACKEND_INVALID path=$backendFile value=$backend; no fallback was selected" }
    }
    $switcher=Join-Path $script:NativeRoot 'Switch-Girlfriend-Voice.ps1'
    $pwsh=Join-Path $env:LOCALAPPDATA 'Microsoft\WindowsApps\pwsh.exe'
    if(-not (Test-Path -LiteralPath $pwsh)) { throw "POWERSHELL_7_NOT_FOUND path=$pwsh" }
    & $pwsh -NoLogo -NoProfile -NonInteractive -ExecutionPolicy Bypass -File $switcher -Target $backend
    if($LASTEXITCODE -ne 0) { throw "TTS_START_FAILED backend=$backend" }

    $stage='8010'
    Write-Host '[3/5] 正在启动数字人 8010…'
    # Ask the supervisor to Ensure :8010. No liveness guard here any more, on purpose:
    # the old one tested service_pids.json['avatar'], which names the VENV LAUNCHER and
    # goes stale as soon as the launcher re-execs into the interpreter that really owns
    # the port -- that read used to green-light a second instance onto a busy port.
    # Ensure is idempotent (ALREADY_RUNNING, nothing killed) and it is the same call the
    # UI and the task wrapper make, so the three cannot disagree about who owns 8010.
    $null = Start-AvatarNative
    Write-Host '8010 数字人已就绪。'

    $stage='7860'
    Write-Host '[4/5] 正在启动前端 7860…'
    $pids=Read-ServicePids
    if([int]$pids.ui -le 0 -or -not (Get-Process -Id ([int]$pids.ui) -ErrorAction SilentlyContinue)) { Start-UiNative }
    Write-Host '[5/5] 正在打开界面…'
    if(-not $NoBrowser) { Start-Process 'http://localhost:7860' }
    Write-Host 'AI 女友已就绪。'
    Write-RuntimeLog 'startup.log' "START_NATIVE_OK backend=$backend"
} catch {
    $reason=$_.Exception.Message
    Write-Host "FAIL_STAGE=$stage" -ForegroundColor Red
    Write-Host "LOG_PATH=$log" -ForegroundColor Yellow
    Write-Host "ROOT_CAUSE=$reason" -ForegroundColor Red
    Write-RuntimeLog 'startup.log' "START_NATIVE_FAILED stage=$stage reason=$reason"
    exit 1
} finally {
    if($locked) { $lock.ReleaseMutex() }
    $lock.Dispose()
}
