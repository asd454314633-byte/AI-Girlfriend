# Switch-Girlfriend-LLM.ps1
# AI_GIRLFRIEND_DUAL_LLM_MEMORY_V1 - minimal production model switcher for 8090.
#
# Usage:  .\Switch-Girlfriend-LLM.ps1 -Target <llm provider id>
#
# Contract:
#   1. Verify target GGUF exists and SHA256 matches models.json.
#   2. Find the current 8090 llama-server process via its listening socket.
#      ONLY stop it when its CommandLine matches THIS project (llama-server.exe
#      + --port 8090 + model path under C:/AI-Girlfriend).
#      Never kill by process name.
#   3. Start the target model with the same b11065 build and the frozen params.
#      The GGUF's embedded chat template is used (no --chat-template override).
#      No mmproj / no vision / no MTP / no second parallel LLM instance.
#   4. Poll /health; verify /v1/models reports the target path; then update
#      active_model.json atomically.
#   5. If the new model fails to become healthy: stop it and automatically
#      restart the previous model (rollback).
#   6. Never touches any other service (8765 / 7860 / 8010 / 8193 / 9333 ...).
#
# ASCII-only on purpose: PS 5.1 misreads non-ASCII literals in BOM-less files.

param(
    [Parameter(Mandatory = $true)]
    [string]$Target
)

$ErrorActionPreference = 'Stop'

# Shared provider-manifest reader (scripts/Runtime-Common.ps1): the target is
# validated against the SAME LLM manifest the Python (providers/registry.py) and
# browser layers read, so registering a new model needs no edit to this file.
# The models.json registry check below still guards that a declared target has a
# real GGUF entry.
. (Join-Path (Split-Path -Parent (Split-Path -Parent $PSScriptRoot)) 'scripts\Runtime-Common.ps1')
if ($Target -notin (Get-ProviderIds 'LLM' -EnabledOnly)) { throw "UNKNOWN_LLM_TARGET target=$Target; not declared/enabled in the LLM manifest" }

# WSL interop can prepend a PowerShell 7 module directory to PSModulePath.
# Import the Windows PowerShell module explicitly so Get-FileHash is available.
Import-Module (Join-Path $PSHOME 'Modules\Microsoft.PowerShell.Utility\Microsoft.PowerShell.Utility.psd1') -Force -ErrorAction Stop

$RegistryPath = ([IO.Path]::GetFullPath((Join-Path $PSScriptRoot '../../runtime/llm/models.json')))
$ActivePath   = ([IO.Path]::GetFullPath((Join-Path $PSScriptRoot '../../runtime/llm/active_model.json')))
$LabelPath    = ([IO.Path]::GetFullPath((Join-Path $PSScriptRoot '../../runtime/llm/active_model_label.txt')))
$LabelPathWsl = Join-Path $PSScriptRoot 'active_model_label_wsl.txt'
$LogDir       = ([IO.Path]::GetFullPath((Join-Path $PSScriptRoot '../../runtime/llm/logs')))
$ProjectRoot  = ([IO.Path]::GetFullPath((Join-Path $PSScriptRoot '../../')))
# $LlamaExe (a hardcoded llama-server.exe path) used to sit here and was never read:
# 8090 is launched by the scheduled task running start_llama_task.bat, which takes the
# exe path from runtime/llm/models.json -> llama_server. Dead literal removed.
# 0.0.0.0 is REQUIRED (not 127.0.0.1): the s2s pipeline runs inside WSL
# (mirrored networking). A 127.0.0.1-only bind is unreachable from WSL
# (tested 2026-09-21: connect timeout). This matches the pre-existing
# production command line exactly.
$Host_        = '0.0.0.0'
$Port         = 8090
$TaskName     = 'GirlfriendLLM8090_Manual'
$HealthTimeoutS = 180
# The client-side probe address, deliberately SEPARATE from the server bind above.
# PS 5.1 Invoke-RestMethod sends 0.0.0.0 request URIs through the system web
# proxy (HTTP_PROXY is set on this machine), which answers 502 - so a health
# check against $Host_ can never succeed. 127.0.0.1/localhost are on the
# .NET proxy bypass list. Verified 2026-09-22: 127.0.0.1 -> status=ok,
# 0.0.0.0 -> (502). This changes only the probe URI; the bind stays 0.0.0.0.
$ProbeHost    = '127.0.0.1'

if (-not (Test-Path $LogDir)) { New-Item -ItemType Directory -Path $LogDir | Out-Null }

# models.json carries Chinese display labels. Get-Content without -Encoding UTF8
# decodes a BOM-less UTF-8 file as ANSI on PS 5.1, which makes ConvertFrom-Json
# throw. These two JSON files are only ever loaded through Read-JsonFile.
function Read-JsonFile([string]$path) {
    return (Get-Content -LiteralPath $path -Raw -Encoding UTF8 | ConvertFrom-Json)
}

function Write-Step([string]$msg) {
    $line = "[{0}] {1}" -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $msg
    Write-Host $line
    Add-Content -Path (Join-Path $LogDir 'switch.log') -Value $line
}

function Get-ActiveModel {
    if (Test-Path $ActivePath) {
        try { return ((Read-JsonFile $ActivePath).active_model) } catch { return $null }
    }
    return $null
}

function Set-ActiveModel([string]$key) {
    $obj = [ordered]@{
        active_model = $key
        updated_at   = (Get-Date -Format 'yyyy-MM-ddTHH:mm:sszzz')
        updated_by   = 'Switch-Girlfriend-LLM.ps1'
    }
    # Write-AtomicFile (scripts/Runtime-Common.ps1, dot-sourced above) keeps two
    # properties this file depends on: BOM-less UTF-8 -- `Set-Content -Encoding UTF8`
    # on PS 5.1 prepends a BOM, json.loads('utf-8') then rejects the file and callers
    # fall back to "no selection" -- and a per-write unique temp name, because one
    # lingering handle on a fixed `active_model.json.tmp` wedges every later switch
    # while the UI still reports the model as active.
    Write-AtomicFile $ActivePath ($obj | ConvertTo-Json)
    # The memory bridge tags every turn from a one-word label file, and it reads
    # the WSL-side copy at runtime (girlfriend_memory_bridge.py:_model_label).
    # Without these the whole history keeps naming the model that used to be up.
    # A failed UNC write must not undo a switch that already passed /health.
    foreach ($p in @($LabelPath, $LabelPathWsl)) {
        try {
            Set-Content -Path $p -Value $key -Encoding ASCII -Force
        } catch {
            Write-Step "LABEL_SYNC_FAILED path=$p err=$($_.Exception.Message)"
        }
    }
}

function Get-8090-Process {
    # Returns $null or a Win32_Process whose CommandLine proves it is THIS project's 8090 llama-server.
    $conn = Get-NetTCPConnection -LocalPort $Port -LocalAddress $Host_ -State Listen -ErrorAction SilentlyContinue |
            Select-Object -First 1
    if (-not $conn) {
        $conn = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue |
                Select-Object -First 1
    }
    if (-not $conn) { return $null }
    $proc = Get-CimInstance Win32_Process -Filter "ProcessId=$($conn.OwningProcess)" -ErrorAction SilentlyContinue
    if (-not $proc) { return $null }
    $cmd = [string]$proc.CommandLine
    if ($proc.Name -eq 'llama-server.exe' -and $cmd -match '--port\s+8090' -and $cmd -like "*$ProjectRoot*") {
        return $proc
    }
    # Port 8090 held by something that is NOT this project's llama-server: refuse to touch it.
    Write-Step "PORT_8090_FOREIGN name=$($proc.Name) pid=$($proc.ProcessId) - refusing to stop"
    throw "Port 8090 is held by a non-project process; aborting (no kill performed)."
}

function Stop-Project-8090 {
    $proc = Get-8090-Process
    if ($null -eq $proc) { Write-Step "NO_8090_PROCESS (nothing to stop)"; return }
    Write-Step "STOPPING pid=$($proc.ProcessId) model=$([regex]::Match($proc.CommandLine, '-m\s+\"([^\"]+)\"').Groups[1].Value)"
    Stop-Process -Id $proc.ProcessId -Force -Confirm:$false
    # Do not start the next model until both the old process and socket are gone.
    for ($i = 0; $i -lt 30; $i++) {
        Start-Sleep -Milliseconds 500
        $alive = Get-CimInstance Win32_Process -Filter "ProcessId=$($proc.ProcessId)" -ErrorAction SilentlyContinue
        $listener = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
        if (-not $alive -and -not $listener) {
            Write-Step "OLD_PROCESS_EXIT_PASS pid=$($proc.ProcessId)"
            Write-Step "PORT_RELEASE_PASS port=$Port"
            Write-Step "STOPPED_8090"
            return
        }
    }
    throw "Old PID $($proc.ProcessId) or port $Port did not release within 15 seconds"
}

function Assert-HostTask {
    $task = Get-ScheduledTask -TaskName $TaskName -ErrorAction Stop
    $action = @($task.Actions)[0]
    $expected = ([IO.Path]::GetFullPath((Join-Path $PSScriptRoot '../../runtime/llm/start_llama_task.bat')))
    if ($task.Triggers -or $action.Execute -ne 'cmd.exe' -or $action.Arguments -notlike "*$expected*") {
        throw "Task $TaskName no longer matches the verified manual production entry"
    }
}

function Wait-HostTaskReady {
    for ($i = 0; $i -lt 40; $i++) {
        if ((Get-ScheduledTask -TaskName $TaskName -ErrorAction Stop).State -ne 'Running') { return }
        Start-Sleep -Milliseconds 500
    }
    throw "Task $TaskName did not release within 20 seconds"
}

function Start-Model([hashtable]$m) {
    # start_llama_task.bat reads active_model.json. Stage the desired key before
    # manual task start, then verify /props before declaring a successful switch.
    Wait-HostTaskReady
    Set-ActiveModel $m.key
    Write-Step "TASK_START key=$($m.key) task=$TaskName"
    Start-ScheduledTask -TaskName $TaskName -ErrorAction Stop
    Write-Step "TASK_START_SUBMITTED key=$($m.key)"
}

function Stop-TaskModel([string]$modelPath) {
    $task = Get-ScheduledTask -TaskName $TaskName -ErrorAction Stop
    if ($task.State -eq 'Running') { Stop-ScheduledTask -TaskName $TaskName -ErrorAction Stop }
    $matches = @(Get-CimInstance Win32_Process -Filter "Name='llama-server.exe'" -ErrorAction SilentlyContinue |
        Where-Object { $_.CommandLine -like "*$ProjectRoot*" -and $_.CommandLine -like "*$modelPath*" -and $_.CommandLine -match '--port\s+8090' })
    foreach ($proc in $matches) { Stop-Process -Id $proc.ProcessId -Force -Confirm:$false }
    Wait-HostTaskReady
}

function Test-Healthy([string]$expectPath, [int]$timeoutS = $HealthTimeoutS) {
    $deadline = (Get-Date).AddSeconds($timeoutS)
    $lastErr = 'none'
    while ((Get-Date) -lt $deadline) {
        try {
            $h = Invoke-RestMethod -Uri "http://${ProbeHost}:$Port/health" -TimeoutSec 3
            if ($h.status -eq 'ok') {
                $props = Invoke-RestMethod -Uri "http://${ProbeHost}:$Port/props" -TimeoutSec 3
                $loaded = [string]$props.model_path
                if ([string]::Equals($loaded, $expectPath, [System.StringComparison]::OrdinalIgnoreCase)) {
                    Write-Step "HEALTH_PROPS_PASS model_path=$loaded"
                    return $true
                }
                Write-Step "HEALTH_OK_BUT_MODEL_MISMATCH model_path=$loaded expected=$expectPath"
                return $false
            }
        } catch { $lastErr = $_.Exception.Message }
        Start-Sleep -Seconds 2
    }
    # Without this line a timeout looks identical to a proxy/bind failure from
    # the log alone, which cost a full 180 s cycle to diagnose once already.
    Write-Step "HEALTH_PROBE_NEVER_OK uri=http://${ProbeHost}:$Port/health last_err=$lastErr"
    return $false
}

# ---- main ----
$registry = Read-JsonFile $RegistryPath
$props = $registry.models.PSObject.Properties | Where-Object { $_.Name -eq $Target }
if (-not $props) { throw "Target '$Target' not in registry" }
$m = @{ key = $Target; path = $props.Value.path; sha256 = $props.Value.sha256; role = $props.Value.role }

if (-not (Test-Path -LiteralPath $m.path)) { throw "TARGET GGUF MISSING: $($m.path)" }

Write-Step "==== SWITCH to $Target (role=$($m.role)) ===="
Write-Step "VERIFY_SHA256 $($m.path) (large file, may take ~1 min)"
$actual = (Get-FileHash -LiteralPath $m.path -Algorithm SHA256).Hash.ToLower()
if ($actual -ne $m.sha256) { throw "SHA256 MISMATCH expected=$($m.sha256) actual=$actual" }
Write-Step "SHA256_OK"

$prevKey = Get-ActiveModel
Write-Step "PREVIOUS_ACTIVE=$prevKey"
Assert-HostTask

if ($Target -eq $prevKey -and (Test-Healthy $m.path 5)) {
    Write-Step "ALREADY_ACTIVE $Target - no switch performed"
    exit 0
}

# The target must not be started while the previous model still holds 8090:
# the new llama-server cannot bind, so Test-Healthy would describe the OLD
# process (that is what the 20:58 log entry recorded as a false SWITCH_OK, and
# the 21:01 one as a 180s HEALTH_FAIL). Stopping first also keeps
# ACTIVE_LLM_COUNT at 1 -- both models never occupy the GPU at the same time.
Stop-Project-8090

$startError = $null
try { Start-Model $m } catch { $startError = $_.Exception.Message; Write-Step "TASK_START_FAILED $startError" }
if (-not $startError -and (Test-Healthy $m.path)) {
    Write-Step "SWITCH_OK active_model=$Target"
    exit 0
}

# ---- failure path: rollback ----
Write-Step "HEALTH_FAIL for $Target - rolling back"
try { Stop-TaskModel $m.path } catch { Write-Step "TASK_STOP_FAILED $($_.Exception.Message)" }
Start-Sleep -Seconds 2

if ($prevKey -and $registry.models.PSObject.Properties[$prevKey] -and $prevKey -ne $Target) {
    $prev = $registry.models.$prevKey
    if (Test-Path -LiteralPath $prev.path) {
        $pm = @{ key = $prevKey; path = $prev.path; sha256 = $prev.sha256; role = $prev.role }
        $rollbackStartError = $null
        try { Start-Model $pm } catch { $rollbackStartError = $_.Exception.Message; Write-Step "ROLLBACK_TASK_START_FAILED $rollbackStartError" }
        if (-not $rollbackStartError -and (Test-Healthy $pm.path)) {
            Write-Step "ROLLBACK_OK active_model=$prevKey"
        } else {
            Write-Step "ROLLBACK_FAIL prev=$prevKey - MANUAL INTERVENTION REQUIRED"
        }
    }
}
Write-Step "SWITCH_FAILED target=$Target"
exit 1
