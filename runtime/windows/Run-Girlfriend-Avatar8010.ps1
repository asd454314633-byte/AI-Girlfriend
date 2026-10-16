# Run-Girlfriend-Avatar8010.ps1 -- the :8010 task wrapper. Intentionally almost empty:
# the ONLY lifecycle logic lives in runtime\windows\Avatar-Supervisor.ps1. This file is
# what the scheduled task's Action runs, so it exists to be a long-lived PARENT: it
# asks the supervisor for the Hold action, and Hold starts the service idempotently,
# records this pid as owner_pid, and blocks until the child is really gone.
#
# Nothing else belongs here. No Start-Process, no port probe, no path to a python, no
# retry loop -- a second copy of any of those is how the task action and the UI started
# disagreeing about who owns 8010. Must also work when
# runtime\windows\avatar_runtime_state.json does not exist yet (first run): the
# supervisor treats a missing/foreign state file as UNOWNED and adopts or starts.
#
# Keep this file pure ASCII. PowerShell 5.1 decodes a BOM-less UTF-8 script as the
# system ANSI codepage (936 here), which silently mangles non-ASCII literals.
[CmdletBinding()]
param(
    [switch]$ValidateShellOnly,
    [string]$AvatarId = '',
    [int]$ReadyTimeoutSec = 180
)

$ErrorActionPreference = 'Stop'

# $PSScriptRoot inside a DOT-SOURCED file is that file's own directory, so resolving
# the supervisor from here (not from the sourced file) is what keeps this working when
# Task Scheduler launches us with a different working directory.
$supervisor = Join-Path $PSScriptRoot 'Avatar-Supervisor.ps1'
if (-not (Test-Path -LiteralPath $supervisor)) { throw "AVATAR_SUPERVISOR_MISSING $supervisor" }
. $supervisor

# InvocationName is '.' while dot-sourcing, which is how the supervisor knows to define
# its functions and skip its own dispatcher.
if (-not (Get-Command -CommandType Function -Name 'Invoke-AvatarHold' -ErrorAction SilentlyContinue)) {
    throw 'AVATAR_SUPERVISOR_NOT_SOURCED Invoke-AvatarHold missing'
}

# SHELL_ONLY_SEAM_BEGIN
if ($ValidateShellOnly) {
    $expectedWorkingDirectory = ([IO.Path]::GetFullPath((Join-Path $PSScriptRoot '../../')))
    $actualWorkingDirectory = (Get-Location).ProviderPath
    if (-not $actualWorkingDirectory -or -not ([IO.Path]::GetFullPath($actualWorkingDirectory).TrimEnd('\') -ieq [IO.Path]::GetFullPath($expectedWorkingDirectory).TrimEnd('\'))) {
        [Console]::Error.WriteLine('VALIDATION_ERROR = WORKING_DIRECTORY_MISMATCH')
        [Console]::Error.WriteLine('AVATAR_HOLD_INVOKED = NO')
        [Console]::Error.WriteLine('PORT_8010_STARTED = NO')
        [Console]::Error.WriteLine('GPU_WORKFLOW_STARTED = NO')
        exit 2
    }
    $facts = Get-AvatarFacts
    $target = Resolve-AvatarTarget $AvatarId $facts
    $launchPlan = Get-AvatarLaunchPlan $facts ([string]$target.id)
    foreach ($requiredPath in @($supervisor, $facts.project_root, $facts.app_dir, $facts.launcher_exe, $facts.runtime_exe)) {
        if (-not $requiredPath -or -not (Test-Path -LiteralPath $requiredPath)) { throw "SHELL_ONLY_REQUIRED_PATH_MISSING $requiredPath" }
    }
    if ([int]$facts.port -ne 8010) { throw "SHELL_ONLY_PORT_MISMATCH $($facts.port)" }
    if (-not $launchPlan.args -or @($launchPlan.args).Count -eq 0) { throw 'SHELL_ONLY_LAUNCH_ARGS_EMPTY' }
    $processPath = (Get-Process -Id $PID).Path
    Write-Output 'VALIDATION_MODE = SHELL_ONLY'
    Write-Output "ProcessPath = $processPath"
    Write-Output "PSHOME = $PSHOME"
    Write-Output "PSVersion = $($PSVersionTable.PSVersion)"
    Write-Output "PSEdition = $($PSVersionTable.PSEdition)"
    Write-Output "Architecture = $([Runtime.InteropServices.RuntimeInformation]::ProcessArchitecture)"
    Write-Output "WorkingDirectory = $actualWorkingDirectory"
    Write-Output 'PRODUCTION_ENTRY_REACHED = YES'
    Write-Output 'AVATAR_HOLD_INVOKED = NO'
    Write-Output 'PORT_8010_STARTED = NO'
    Write-Output 'HEAVY_AI_WORKLOAD_STARTED = NO'
    Write-Output 'GPU_WORKFLOW_STARTED = NO'
    Write-Output 'PRODUCTION_STATE_CHANGED = NO'
    exit 0
}
# SHELL_ONLY_SEAM_END

$result = $null
try {
    $facts = Get-AvatarFacts
    $result = Invoke-AvatarHold $facts $AvatarId $ReadyTimeoutSec
} catch {
    try { Write-RuntimeLog 'avatar-owner.log' ('AVATAR_OWNER_FATAL pid={0} error={1}' -f $PID, $_.Exception.Message) } catch {}
    throw
}

# A clean Stop leaves exit_code empty (the child was killed, it did not return); the
# task history should read 0 for that, not blank.
$code = [int]$result.exit_code
if ($null -eq $result.exit_code) { $code = 0 }
if ($result.verdict -eq 'FAILED' -and $code -eq 0) { $code = 1 }
exit $code
