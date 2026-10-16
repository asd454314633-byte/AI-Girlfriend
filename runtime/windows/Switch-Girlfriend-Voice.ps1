param(
    [Parameter(Mandatory=$true)]
    [string]$Target
)

$ErrorActionPreference = 'Stop'
. (Join-Path (Split-Path -Parent (Split-Path -Parent $PSScriptRoot)) 'scripts\Runtime-Common.ps1')
try { & (Join-Path $script:AppRoot 'scripts\Write-Lifecycle-Context.ps1') }
catch { Write-RuntimeLog 'tts-switch.log' ('LIFECYCLE_CONTEXT_UNAVAILABLE '+$_.Exception.Message) }
# Validation is resolved against the TTS manifest at run time (no hardcoded set),
# so a newly registered provider switches without editing this file.
$targetSpec = Get-ProviderSpec 'TTS' $Target
if (-not $targetSpec) { throw "UNKNOWN_TTS_TARGET target=$Target; not declared in the TTS manifest" }
$Target = [string]$targetSpec.id
$selFlag = Get-ProviderSelectionFlag 'TTS'
$backendFile = Join-Path $script:NativeRoot 'active_tts_backend.txt'
$switchFile = Join-Path $script:NativeRoot 'tts_switch_state.json'
$mutex = New-Object System.Threading.Mutex($false,'Global\AIGirlfriendNativeTTSLifecycle')
$locked = $false
# labels / handlers tables deleted: read display_name_zh / handler.class from the
# resolved provider spec instead (see Get-ProviderLabel / Get-ProviderHandlerClass).
# Provider asset facts are no longer listed here. The worker script, model
# directory and any other launch placeholder a row declares are resolved from that
# row (Get-ProviderLaunchContext) and handed to the expander -- and since 2026-09-24
# that includes the externally installed native worker too: fish's install root, exe,
# DLL directory, model, tokenizer, voice directory and sample voice all live in
# tts.json -> providers[id=fish].launch.placeholders, and Start-Fish takes them from
# the $Ctx it is already handed. Before that they were six literals in this
# system-layer file -- i.e. the one thing that still forced an edit here when a voice
# was reinstalled elsewhere (see runtime/audit/system_v2/LIFECYCLE_MANIFEST_SWEEP.md).
# $cudaBin used to be repeated here with the same value the shared lifecycle layer
# already publishes; one owner is enough (Runtime-Common.ps1 is dot-sourced above).
$cudaBin = $script:CudaBin
if (-not $cudaBin) { throw 'CUDA_BIN_UNDECLARED scripts/Runtime-Common.ps1 did not publish $CudaBin' }
# The whisper model path is no longer written here. It is resolved from the STT manifest row's
# launch.placeholders.STT_MODEL_DIR, so pointing the pipeline at another recognizer-weight
# directory is a manifest edit rather than an edit of this lifecycle script.
$refText = '这是一段用于验证语音克隆的合成参考文本。'

function Save-SwitchState([bool]$Busy,[string]$Phase,[string]$Message,[string]$Result='',[string]$Detail='',[string]$Previous='',[string]$Destination='') {
    # TSSCP01 (TASK_ID = AI_GIRLFRIEND_TTS_SWITCH_CONTROL_PLANE_FINAL_REPAIR_01):
    # 本脚本不再是 tts_switch_state.json 的写者。该文件现在是**事务日志**，唯一写者是
    # :7860 的 BFF（voice_models_admin.py:_journal_write）；"是否正在切换"只能由它得出，
    # busy 由 phase 派生、不再是独立事实。此前的双写者设计有真实事故路径：mutex 冲突
    # 走到下面 catch 分支时，第二次调用会把一次**正在运行**的事务覆盖成
    # busy=false/SWITCH_FAILED —— 两个写者互相破坏。
    # 生命周期逻辑（全局互斥锁 / Stop-S2S / worker stop-start / Start-S2S / Wait-S2S /
    # rollback / SWITCH_OK-SWITCH_FAILED-ROLLBACK_OK stdout 契约 / active_tts_backend
    # 成功提交）全部冻结不变：本函数只保留同样的形参并把每条状态行写进运行日志，
    # 供取证对照；stdout/exit code 契约照旧。
    Write-RuntimeLog 'tts-switch.log' ('SWITCH_STATE(readonly) busy={0} phase={1} result={2} previous={3} target={4} detail={5}' -f $Busy,$Phase,$Result,$Previous,$Destination,$Detail)
}

function Get-ActiveBackend {
    # Missing state file falls back to the manifest default_selection, never a
    # hardcoded provider; the stored value is validated against the manifest
    # (id / cli_flag_value / alias), never against a hardcoded handler table.
    if (-not (Test-Path -LiteralPath $backendFile)) { return Get-ProviderDefault 'TTS' }
    $value = (Read-JsonText $backendFile).Trim()
    if (Get-ProviderSpec 'TTS' $value) { return $value }
    return $null
}

function Get-NativeStatus {
    try { return Get-HttpJson 'http://127.0.0.1:8765/v1/pool' 3 } catch { return $null }
}

function Wait-PortClosed([int]$Port,[int]$TimeoutSec=30) {
    $deadline=(Get-Date).AddSeconds($TimeoutSec)
    while ((Get-Date) -lt $deadline) {
        if (-not (Test-Tcp '127.0.0.1' $Port 300)) { return }
        Start-Sleep -Milliseconds 500
    }
    throw "PORT_STILL_OPEN port=$Port"
}

function Start-Fish($Spec,$Ctx) {
    # Class-A launch body for one external worker. It is reached only through
    # Start-ProviderWorker, which resolves the name from that row's worker.pid_key,
    # so the pid/state key, the log stem and the listen port below are read from the
    # manifest instead of being repeated here. So is every asset path: $Ctx is that
    # row's resolved launch context, and a placeholder that did not resolve to a path
    # that exists is an error here rather than an empty argument further down.
    $pidKey=[string](Get-ProviderProp (Get-ProviderProp $Spec 'worker') 'pid_key')
    $port=Get-ProviderHealthPort $Spec
    if (-not $Ctx) { throw "WORKER_LAUNCH_CONTEXT_EMPTY pid_key=$pidKey; that row declares no placeholder resolving to an existing path" }
    foreach($key in @('FISH_ROOT','FISH_EXE','FISH_EXE_DIR','FISH_MODEL','FISH_TOKENIZER','FISH_VOICES','FISH_VOICE_SAMPLE')) {
        if (-not [string]$Ctx[$key]) { throw "WORKER_ASSET_UNDECLARED pid_key=$pidKey placeholder=$key; launch.placeholders must name a path that exists" }
    }
    $fishRoot=[string]$Ctx['FISH_ROOT']
    $fishExe=[string]$Ctx['FISH_EXE']
    $fishBuild=[string]$Ctx['FISH_EXE_DIR']
    $fishModel=[string]$Ctx['FISH_MODEL']
    $fishTokenizer=[string]$Ctx['FISH_TOKENIZER']
    $fishVoices=[string]$Ctx['FISH_VOICES']
    foreach($path in @($fishExe,$fishModel,$fishTokenizer,[string]$Ctx['FISH_VOICE_SAMPLE'])) {
        if (-not (Test-Path -LiteralPath $path)) { throw "FISH_ASSET_MISSING path=$path" }
    }
    Assert-PortFree $port
    $oldPath=$env:PATH
    try {
        $env:PATH="$cudaBin;$fishBuild\bin;$fishBuild;$fishRoot;$oldPath"
        $args=@('--model',$fishModel,'--tokenizer',$fishTokenizer,'--threads','14','--cuda','0',
            '--gpu-layers','32','--voice-dir',$fishVoices,'--server','-H','127.0.0.1','-P',[string]$port)
        $pidValue=Start-NativeProcess $pidKey $fishExe $args $fishRoot ($pidKey+'-worker')
    } finally { $env:PATH=$oldPath }
    Wait-Tcp (Get-ProviderHealthHost $Spec) $port 180 $pidValue
    return $pidValue
}

function Wait-Higgs($Spec,[int]$PidValue,[int]$TimeoutSec=900) {
    # Readiness probe for an external HTTP worker that checks one fact the declared
    # health plan does not: the engine must be idle, not merely loaded. Everything
    # else it touches - credential file, base url, endpoint paths, port, pid key - is
    # read from the same row, so a new row needs no edit here.
    $pidKey=[string](Get-ProviderProp (Get-ProviderProp $Spec 'worker') 'pid_key')
    $health=Get-ProviderProp $Spec 'health'
    $base=[string](Get-ProviderProp $health 'base_url')
    $endpoints=@((Get-ProviderProp $health 'endpoints'))
    $keyPath=Join-Path $script:AppRoot (([string](Get-ProviderProp $health 'credentials_file')).Replace('/','\'))
    $apiKey=(Read-JsonText $keyPath).Trim()
    $deadline=(Get-Date).AddSeconds($TimeoutSec)
    while((Get-Date) -lt $deadline) {
        if(-not (Get-Process -Id $PidValue -ErrorAction SilentlyContinue)) {
            throw ('{0}_PROCESS_EXITED pid={1}' -f $pidKey.ToUpperInvariant(),$PidValue)
        }
        try {
            $health=Get-HttpJson ($base+[string]$endpoints[0].path) 5
            $status=Invoke-RestMethod -Uri ($base+[string]$endpoints[1].path) -Headers @{Authorization="Bearer $apiKey"} -TimeoutSec 5 -Proxy $null
            if($health.ok -and $status.engineLoaded -and $status.modelLoaded -and -not $status.generating) { return }
        } catch {}
        Start-Sleep -Seconds 3
    }
    throw ('{0}_START_TIMEOUT port={1} timeout_s={2}' -f $pidKey.ToUpperInvariant(),(Get-ProviderHealthPort $Spec),$TimeoutSec)
}

function Start-Higgs($Spec,$Ctx) {
    # Class-A launch body, dispatched by that row's worker.pid_key. The interpreter,
    # the worker script, the working directory, the log stem and the listen port all
    # come from the row; only the "-u" flag and the --host/--port spelling are still
    # assembled here because no manifest field carries them yet.
    $pidKey=[string](Get-ProviderProp (Get-ProviderProp $Spec 'worker') 'pid_key')
    $port=Get-ProviderHealthPort $Spec
    $hostName=Get-ProviderHealthHost $Spec
    Assert-PortFree $port
    $plan=Expand-ProviderLaunch $Spec $Ctx
    if (@($plan.missing_placeholders).Count) {
        throw "PROVIDER_WORKER_PLACEHOLDER_MISSING provider=$pidKey missing=$(($plan.missing_placeholders -join ','))"
    }
    $workerPath=@($plan.worker_args)[0]
    $keyPath=Join-Path $script:AppRoot (([string](Get-ProviderProp (Get-ProviderProp $Spec 'health') 'credentials_file')).Replace('/','\'))
    foreach($path in @($script:S2SPython,$workerPath,$keyPath)) {
        if(-not (Test-Path -LiteralPath $path)) { throw "HIGGS_ASSET_MISSING path=$path" }
    }
    $args=@('-u') + @($plan.worker_args) + @('--host',$hostName,'--port',[string]$port)
    $old=$env:PYTHONUNBUFFERED
    $env:PYTHONUNBUFFERED='1'
    try { $pidValue=Start-NativeProcess $pidKey $script:S2SPython $args (Split-Path -Parent $workerPath) ($pidKey+'-'+$port) }
    finally { [Environment]::SetEnvironmentVariable('PYTHONUNBUFFERED',$old,'Process') }
    return $pidValue
}

function Get-ActiveLlmModelName {
    # The served model name is derived from the LLM selection state + the model
    # registry label, never hardcoded, so a model swap needs no edit here.
    $activeFile = Join-Path $script:RuntimeRoot 'llm\active_model.json'
    $modelsFile = Join-Path $script:RuntimeRoot 'llm\models.json'
    if (-not (Test-Path -LiteralPath $activeFile) -or -not (Test-Path -LiteralPath $modelsFile)) { return $null }
    $activeKey = [string](Get-ProviderProp (Read-JsonFile $activeFile) 'active_model')
    if (-not $activeKey) { return $null }
    $models = Read-JsonFile $modelsFile
    $modelTable = Get-ProviderProp $models 'models'
    if ($modelTable -and $modelTable.PSObject.Properties[$activeKey]) {
        $entry = $modelTable.$activeKey
        $label = Get-ProviderProp $entry 'label'
        if ($label) { return [string]$label }
        return $activeKey
    }
    return $activeKey
}

function Start-S2S([string]$Backend) {
    Assert-PortFree 8765
    $ttsSpec = Get-ProviderSpec 'TTS' $Backend
    if (-not $ttsSpec) { throw "S2S_TTS_PROVIDER_UNDECLARED backend=$Backend" }
    # The --tts fragment (flag + provider-specific args) is expanded from the
    # manifest launch.cli_args, never assembled field-by-field in this file, and the
    # asset paths it asks for are resolved from that same row. Only the frozen
    # reference transcript has no machine-readable manifest value yet.
    $ctx = Get-ProviderLaunchContext $ttsSpec
    $ctx['QWEN3_REF_TEXT'] = $refText
    $ttsMissing = New-Object System.Collections.Generic.List[string]
    $ttsCli = Get-ProviderProp (Get-ProviderProp $ttsSpec 'launch') 'cli_args'
    $ttsArgs = if ($ttsCli) { Expand-ProviderLaunchItems @($ttsCli) $ctx $ttsMissing } else { @() }
    if (@($ttsMissing).Count) {
        $uniq = @($ttsMissing | Sort-Object -Unique)
        throw "S2S_TTS_PLACEHOLDER_UNRESOLVED provider=$Backend missing=$($uniq -join ',')"
    }
    # STT likewise comes from its own manifest row. The whisper model directory is now a
    # machine-readable launch placeholder ({STT_MODEL_DIR}) instead of a literal in this file,
    # and "first enabled row" is no longer an acceptable way to choose a recognizer: with more
    # than one STT registered, row order silently deciding the pipeline is the coupling this
    # whole layer exists to remove.
    $sttId = Get-ProviderDefault 'STT'
    if (-not $sttId) { throw 'S2S_STT_DEFAULT_UNDECLARED stt.json has no default_selection; refusing to pick by row order' }
    $sttSpec = Get-ProviderSpec 'STT' $sttId
    if (-not $sttSpec) { throw "S2S_STT_PROVIDER_UNDECLARED stt=$sttId" }
    $sttCtx = Get-ProviderLaunchContext $sttSpec
    $sttMissing = New-Object System.Collections.Generic.List[string]
    $sttCli = Get-ProviderProp (Get-ProviderProp $sttSpec 'launch') 'cli_args'
    $sttArgs = if ($sttCli) { Expand-ProviderLaunchItems @($sttCli) $sttCtx $sttMissing } else { @() }
    if (@($sttMissing).Count) {
        $uniq = @($sttMissing | Sort-Object -Unique)
        throw "S2S_STT_PLACEHOLDER_UNRESOLVED provider=$sttId missing=$($uniq -join ',')"
    }
    $llmMf = Get-ProviderManifest 'LLM'
    $transport = Get-ProviderProp $llmMf 'transport'
    $llmBackend = [string](Get-ProviderProp $transport 's2s_llm_backend_flag')
    $llmBaseUrl = [string](Get-ProviderProp $transport 'base_url')
    $modelName = Get-ActiveLlmModelName
    if (-not $modelName) { throw 'S2S_LLM_MODEL_NAME_UNDETERMINED active_model.json/models.json unreadable' }
    # The S2S executable is always required; the provider's own assets come from the resolved
    # launch.requires of the rows actually selected. An entry that still contains
    # {PLACEHOLDER} is a worker-side asset validated by that provider's start hook, so it is
    # intentionally skipped here.
    $assets = @($script:S2SExe)
    foreach ($pair in @(@{spec=$ttsSpec; ctx=$ctx }, @{spec=$sttSpec; ctx=$sttCtx })) {
        foreach ($req in @((Expand-ProviderLaunch $pair.spec $pair.ctx).requires)) {
            if ($req -match '\{') { continue }
            if ([System.IO.Path]::IsPathRooted($req)) { $assets += $req }
            else { $assets += (Join-Path $script:AppRoot $req) }
        }
    }
    foreach($path in $assets) {
        if (-not (Test-Path -LiteralPath $path)) { throw "S2S_ASSET_MISSING path=$path" }
    }
    $args=@('--mode','realtime') + $sttArgs + @(
        '--language','zh',
        '--llm_backend',$llmBackend,'--model_name',$modelName,
        '--responses_api_base_url',$llmBaseUrl,
        '--responses_api_api_key','sk-noauth','--responses_api_stream',
        '--chat_size','24','--compact_history','--num_pipelines','1') + $ttsArgs + @(
        '--thresh','0.6','--min_speech_ms','500','--min_silence_ms','1200','--speech_pad_ms','300',
        '--ws_port','8765')
    $old=@{}
    foreach($name in @('NLTK_DATA','TORCH_HOME','HF_HUB_OFFLINE','TRANSFORMERS_OFFLINE','HF_HOME','PYTHONUNBUFFERED','PATH')) {
        $old[$name]=[Environment]::GetEnvironmentVariable($name,'Process')
    }
    try {
        $env:NLTK_DATA=Join-Path $script:RuntimeRoot 'core\nltk_data'
        $env:TORCH_HOME=Join-Path $script:RuntimeRoot 'core\torch_home'
        $env:HF_HUB_OFFLINE='1'; $env:TRANSFORMERS_OFFLINE='1'; $env:PYTHONUNBUFFERED='1'
        $env:HF_HOME=Join-Path $script:RuntimeRoot 'core\hf_home'
        $env:PATH="$cudaBin;$($old['PATH'])"
        $pidValue=Start-NativeProcess 's2s' $script:S2SExe $args $script:AppRoot 's2s'
    } finally {
        foreach($name in $old.Keys) { [Environment]::SetEnvironmentVariable($name,$old[$name],'Process') }
    }
    $null=Wait-S2S $Backend 900 $pidValue
    return $pidValue
}

try {
    $locked=$mutex.WaitOne(0)
    if (-not $locked) { throw 'SWITCH_ALREADY_RUNNING' }

    $pids=Read-ServicePids
    $previous=Get-ActiveBackend
    $status=Get-NativeStatus
    $s2sPid=[int]$pids.s2s
    $s2sProc=if($s2sPid -gt 0){Get-CimInstance Win32_Process -Filter "ProcessId=$s2sPid" -ErrorAction SilentlyContinue}else{$null}
    $nativeS2S=$s2sProc -and ($s2sProc.ExecutablePath -ieq $script:S2SExe)
    # Reverse-infer the running backend from the S2S command line. The selection
    # flag and the accepted tokens are read from the manifest (never a hardcoded
    # provider list), and a captured id/flag/alias is normalized back to its id.
    $inferTokens = @(Get-ProviderFlags 'TTS' | Where-Object { $_ } |
        Sort-Object { $_.Length } -Descending | ForEach-Object { [regex]::Escape($_) }) -join '|'
    $inferRegex = "{0}(?:\s+|=)({1})(?:\s|$)" -f [regex]::Escape($selFlag), $inferTokens
    $runtimeBackend=$null
    if($nativeS2S -and $s2sProc.CommandLine -match $inferRegex) {
        $captured = Get-ProviderSpec 'TTS' $Matches[1]
        if ($captured) { $runtimeBackend = [string]$captured.id }
    }
    if(-not $previous -and (Test-Path -LiteralPath $backendFile)) {
        if($nativeS2S -and $runtimeBackend) { $previous=$runtimeBackend }
        elseif(-not (Test-Tcp '127.0.0.1' 8765 250)) { $previous=$null }
        else { throw "ACTIVE_TTS_BACKEND_INVALID path=$backendFile; current 8765 owner/backend cannot be proven; refusing to guess" }
    }
    $prevSpec = Get-ProviderSpec 'TTS' $previous
    if ($prevSpec) { $previous = [string]$prevSpec.id }
    $prevLabel = Get-ProviderLabel $prevSpec
    $targetLabel = Get-ProviderLabel $targetSpec
    if($previous -and $runtimeBackend -and $runtimeBackend -ne $previous) {
        throw "ACTIVE_TTS_STATE_RUNTIME_MISMATCH file=$previous process=$runtimeBackend; refusing an unsafe switch"
    }
    if($nativeS2S -and -not $status) { throw 'TTS_STATUS_UNAVAILABLE; refusing to interrupt an unobservable active response' }
    if ($status -and [int]$status.in_use -gt 0) { throw 'BUSY' }

    $cmdMatches = $s2sProc -and $s2sProc.CommandLine -match ("{0}(?:\s+|=){1}(?:\s|$)" -f [regex]::Escape($selFlag), [regex]::Escape($Target))

    if ($Target -eq $previous -and $nativeS2S -and $cmdMatches -and $status -and $status.size -eq 1) {
        if(-not (Test-Path -LiteralPath $backendFile) -or (Get-ActiveBackend) -ne $Target) {
            Write-AtomicFile $backendFile $Target -Encoding Ascii
        }
        Save-SwitchState $false '' '' 'SWITCH_OK' 'already active' $previous $Target
        & (Join-Path $script:AppRoot 'scripts\Schedule-UI-Migration.ps1')
        Write-Output "SWITCH_OK target=$Target"
        exit 0
    }

    Save-SwitchState $true 'unloading' "正在关闭 $prevLabel…" '' '' $previous $Target
    if ($nativeS2S) {
        Write-RuntimeLog 'tts-switch.log' "GPU_TTS_UNLOAD_BEGIN backend=$previous pid=$s2sPid"
        Stop-S2S
        Wait-PortClosed 8765
        Write-RuntimeLog 'tts-switch.log' "OLD_TTS_PROCESS_EXITED backend=$previous pid=$s2sPid; CUDA allocations are released with process exit"
    } elseif (Test-Tcp '127.0.0.1' 8765 250) {
        throw 'LEGACY_8765_ACTIVE: leaving WSL fallback untouched'
    }

    # Unload the previous provider's external worker through the generic dispatcher:
    # it calls Stop-<Pascal(worker.pid_key)> when that provider ships one and, for
    # every other external worker, tears the process tree down from the declared
    # worker/health fields. No provider id is named here.
    foreach ($wSpec in (Get-ExternalProviderWorkers 'TTS')) {
        if (([string]$wSpec.id) -eq $previous) { Stop-ProviderWorker $wSpec }
    }
    Write-RuntimeLog 'tts-switch.log' "OLD_BACKEND_WORKERS_EXITED backend=$previous; proceeding with target=$Target"

    try {
        Save-SwitchState $true 'loading' "正在加载 $targetLabel…" '' '' $previous $Target
        foreach ($wStart in (Get-ExternalProviderWorkers 'TTS')) {
            if (([string]$wStart.id) -eq $Target) { Start-ProviderWorker $wStart (Get-ProviderLaunchContext $wStart) }
        }
        $null=Start-S2S $Target
        # This is the one write that makes the switch real for everyone else:
        # `active_tts_backend.txt` is the single live authority for the active TTS
        # (registry.SELECTION_SOURCES['TTS']).  If it ever stops accepting updates the
        # process is on the new voice while every reader still reports the old one, so
        # it goes through the unique-temp-name writer rather than a fixed `*.tmp`.
        Write-AtomicFile $backendFile $Target -Encoding Ascii
        Save-SwitchState $false '' '' 'SWITCH_OK' '' $previous $Target
        Write-Output "SWITCH_OK target=$Target"
        exit 0
    } catch {
        $reason=$_.Exception.Message
        try {
            if ([int](Read-ServicePids).s2s -gt 0) { Stop-S2S }
            foreach ($wStop in (Get-ExternalProviderWorkers 'TTS')) { Stop-ProviderWorker $wStop }
            if ($previous) {
                foreach ($wBack in (Get-ExternalProviderWorkers 'TTS')) {
                    if (([string]$wBack.id) -eq $previous) { Start-ProviderWorker $wBack (Get-ProviderLaunchContext $wBack) }
                }
                $null=Start-S2S $previous

                Save-SwitchState $false '' '' 'ROLLBACK_OK' $reason $previous $Target
                Write-Output "ROLLBACK_OK previous=$previous target=$Target reason=$reason"
                exit 0
            }
            throw "NO_PREVIOUS_BACKEND_TO_ROLL_BACK reason=$reason"
        } catch {
            $rollback=$_.Exception.Message
            Save-SwitchState $false '' '' 'SWITCH_FAILED' "$reason; rollback=$rollback" $previous $Target
            throw "SWITCH_FAILED target=$Target reason=$reason rollback=$rollback"
        }
    }
} catch {
    $reason=$_.Exception.Message
    Save-SwitchState $false '' '' 'SWITCH_FAILED' $reason (Get-ActiveBackend) $Target
    Write-Error "SWITCH_FAILED target=$Target reason=$reason"
    exit 1
} finally {
    if ($locked) { $mutex.ReleaseMutex() }
    $mutex.Dispose()
}
