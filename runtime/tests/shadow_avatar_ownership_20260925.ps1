# Shadow test: avatar process ownership repair (AI_GIRLFRIEND_AVATAR_PROCESS_OWNERSHIP_FINAL_REPAIR_01)
# Pure-function level + live read-only assertions. Kills nothing, touches no state file.
# Run: powershell.exe -NoProfile -NonInteractive -ExecutionPolicy Bypass -File runtime\tests\shadow_avatar_ownership_20260925.ps1
# Keep this file pure ASCII (PS 5.1 decodes BOM-less UTF-8 as codepage 936).

$ErrorActionPreference = 'Stop'
$ProjectRoot = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
. (Join-Path $ProjectRoot 'scripts\Runtime-Common.ps1')
. (Join-Path $ProjectRoot 'scripts\Avatar-Launch-Common.ps1')
. (Join-Path $ProjectRoot 'runtime\windows\Avatar-Supervisor.ps1')

$script:PASS = 0
$script:FAIL = 0
function Assert-Shadow([string]$Name, [bool]$Cond) {
    if ($Cond) { $script:PASS++; Write-Output ("PASS " + $Name) }
    else { $script:FAIL++; Write-Output ("FAIL " + $Name) }
}

# ---- fixtures -------------------------------------------------------------
$now = Get-Date
$nowDmtf = [System.Management.ManagementDateTimeConverter]::ToDmtfDateTime($now)
$oldDmtf = [System.Management.ManagementDateTimeConverter]::ToDmtfDateTime($now.AddMinutes(-7))
$nowIso = $now.ToString('o')

function New-FakeProc([int]$Id, [string]$DmtfBirth) {
    return [pscustomobject]@{ ProcessId = $Id; CreationDate = $DmtfBirth; CommandLine = 'x'; Name = 'python.exe'; ExecutablePath = 'x' }
}
function New-FakeFile {
    return [pscustomobject]@{
        launcher_pid = 100; listener_pid = 200
        launcher_start_time = $nowIso; listener_start_time = $nowIso
        owner_pid = 300; owner_start_time = $nowIso
    }
}

# ---- T-A: recorded-generation matching (pid + birth) ----------------------
$file = New-FakeFile
Assert-Shadow 'T-A1 listener pid+birth match -> recorded generation' (Test-AvatarRecordedGenerationProcess (New-FakeProc 200 $nowDmtf) $file)
Assert-Shadow 'T-A2 listener pid match, birth differs 7min -> PID reuse, NOT recorded' (-not (Test-AvatarRecordedGenerationProcess (New-FakeProc 200 $oldDmtf) $file))
Assert-Shadow 'T-A3 launcher pid+birth match -> recorded generation' (Test-AvatarRecordedGenerationProcess (New-FakeProc 100 $nowDmtf) $file)
$legacyFile = [pscustomobject]@{ launcher_pid = 100; listener_pid = 200; owner_pid = 300 }
Assert-Shadow 'T-A4 legacy file, bare pid match -> conservative TRUE (cannot disown)' (Test-AvatarRecordedGenerationProcess (New-FakeProc 200 $oldDmtf) $legacyFile)
Assert-Shadow 'T-A5 pid absent from file -> FALSE' (-not (Test-AvatarRecordedGenerationProcess (New-FakeProc 999 $nowDmtf) $file))

# ---- T-B: owner birth matching -------------------------------------------
Assert-Shadow 'T-B1 owner birth matches -> owner adopted' (Test-AvatarOwnerBirthMatches (New-FakeProc 300 $nowDmtf) $nowIso)
Assert-Shadow 'T-B2 owner birth differs -> PID reuse refused' (-not (Test-AvatarOwnerBirthMatches (New-FakeProc 300 $oldDmtf) $nowIso))
Assert-Shadow 'T-B3 no expected birth on file (legacy) -> adopt' (Test-AvatarOwnerBirthMatches (New-FakeProc 300 $oldDmtf) '')

# ---- T-C: live read-only assertions on the RUNNING generation -------------
$F = Get-AvatarFacts
$status = Get-AvatarStatus $F
Assert-Shadow 'T-C1 live status derived RUNNING' ([string]$status.state -eq 'RUNNING')
Assert-Shadow 'T-C2 owner_pid resolved' ([int]$status.owner_pid -gt 0)
Assert-Shadow 'T-C3 listener_pid resolved' ([int]$status.listener_pid -gt 0)
Assert-Shadow 'T-C4 pid_agreement MATCH' ([string]$status.pid_agreement -eq 'MATCH')

$orphans = Get-AvatarOrphanWorkers $F
$orphanPids = @($orphans | ForEach-Object pid)
Assert-Shadow 'T-C5 live reaper does NOT name the recorded launcher' (-not ($orphanPids -contains [int]$status.launcher_pid))
Assert-Shadow 'T-C6 live reaper does NOT name the recorded listener' (-not ($orphanPids -contains [int]$status.listener_pid))

# T-C7: birth fields present in the live state file (written by the repaired HoldOnce)
$liveFile = Read-AvatarStateFile
$hasBirths = $false
if ($liveFile -and $liveFile.PSObject.Properties['owner_start_time'] -and $liveFile.PSObject.Properties['listener_start_time']) {
    $hasBirths = ([string]$liveFile.owner_start_time -ne '' -and [string]$liveFile.listener_start_time -ne '')
}
Assert-Shadow 'T-C7 live state file carries owner/listener births' $hasBirths

Write-Output ('SHADOW_OWNERSHIP_RESULT pass=' + $script:PASS + ' fail=' + $script:FAIL)
if ($script:FAIL -gt 0) { exit 1 }
exit 0
