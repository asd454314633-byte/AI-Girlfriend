# Switch-Girlfriend-Avatar.ps1 -- the ONLY process-side entry point for changing
# which digital human is on screen.
#
# Python (runtime/ui/app/avatar_admin.py) schedules and reports; this script owns
# every process side effect. It never touches 8090 / 8765 / 3030 / 7860 and never
# starts a second instance of the digital-human service.
#
# Modes:
#   Switch  - stop the current service, start the target, verify, commit pointer.
#   Sync    - point the pointer at whatever the service is actually running now
#             (the explicit "同步一下" button; no restart, no process change).
#   Restart - start whatever the pointer already names (the manual "重新启动"
#             button after a failed rollback left the service down).
#
# PROCESS SIDE EFFECTS ARE DELEGATED, not implemented here. Since the lifecycle
# work landed, this file dot-sources runtime\windows\Avatar-Supervisor.ps1 and calls
# its primitives: every launch goes through Invoke-AvatarLaunch, every teardown
# through Stop-AvatarTree, every read through Get-AvatarStatus. It keeps what is
# specific to switching -- the precheck/verify/rollback sequencing, the pointer
# commit below, and the exit contract -- and nothing else. Start-Process and
# Stop-NativeProcess are no longer called from here.
#
# Exit contract with the Python scheduler -- these strings are the whole API:
#   AVATAR_SWITCH_OK
#   AVATAR_SWITCH_ABORT                      (precheck refused; nothing changed)
#   AVATAR_SWITCH_FAILED ROLLBACK_OK         (old person is back and verified)
#   AVATAR_SWITCH_FAILED ROLLBACK_FAILED     (service left down; UI offers restart)
#
# There is deliberately no retry loop, no watchdog and no scheduled task here. A
# switch runs once, and when it is over the service is either the new person, the
# old person, or down -- never left half-started and never silently re-raised.

param(
    [string]$Target,
    [ValidateSet('Switch', 'Sync', 'Restart')][string]$Mode = 'Switch'
)

$ErrorActionPreference = 'Stop'
# This file lives in runtime\windows\; the lifecycle library it shares the launch
# body with lives in scripts\. Two levels up is the project root.
$script:ProjectRoot = (Split-Path -Parent (Split-Path -Parent $PSScriptRoot))
. (Join-Path $script:ProjectRoot 'scripts\Runtime-Common.ps1')
. (Join-Path $script:ProjectRoot 'scripts\Avatar-Launch-Common.ps1')
# The lifecycle primitives. Dot-sourcing sets $MyInvocation.InvocationName to '.',
# which is how the supervisor knows to define its functions and skip its dispatcher;
# its $script: assignments land in THIS scope, so the ownership identity rules and the
# state file path are literally the same objects the task wrapper uses.
$avatarSupervisor = Join-Path $PSScriptRoot 'Avatar-Supervisor.ps1'
if (-not (Test-Path -LiteralPath $avatarSupervisor)) { throw "AVATAR_SUPERVISOR_MISSING $avatarSupervisor" }
. $avatarSupervisor
if (-not (Get-Command -CommandType Function -Name 'Get-AvatarStatus' -ErrorAction SilentlyContinue)) {
    throw 'AVATAR_SUPERVISOR_NOT_SOURCED Get-AvatarStatus missing'
}
$script:Facts = Get-AvatarFacts

$switchLog = 'avatar-switch.log'
$pointerPath = Get-AvatarPointerPath
# The switch waits on the supervisor's own ready poll instead of a Wait-Tcp of its own,
# so the boot budget lives in one place (the supervisor's default is the same number).
$script:SwitchReadyTimeoutSec = 180
# 'Auto' = hand the start to the durable owner when GirlfriendAvatar8010_Manual is
# installed (see Invoke-AvatarLaunch), fall back to launching from this process when it
# is not. Deliberately not 'Direct': a switch that left :8010 with no long-lived parent
# would undo the ownership work this change is about.
$script:SwitchOwnerMode = 'Auto'


function Write-AvatarPointer {
    # The single writer of the pointer file. One line, ASCII, no BOM -- byte for
    # byte the same shape as active_tts_backend.txt. Unique temp name + os-style
    # replace, exactly like Runtime-Common's Write-AtomicFile, then read back to
    # prove what landed is what we meant to land.
    param([string]$AvatarId)
    if ([string]::IsNullOrWhiteSpace($AvatarId)) { throw 'POINTER_WRITE_EMPTY_ID' }
    $tmp = Join-Path $script:NativeRoot (
        'active_avatar.txt.{0}.{1}.tmp' -f $PID, [guid]::NewGuid().ToString('N').Substring(0, 12))
    Set-Content -LiteralPath $tmp -Value "$AvatarId" -Encoding Ascii -NoNewline:$false
    Move-Item -LiteralPath $tmp -Destination $pointerPath -Force
    $landed = (Read-JsonText $pointerPath).Trim()
    if ($landed -ne $AvatarId) { throw "POINTER_WRITE_VERIFY_FAILED wrote=$AvatarId read=$landed" }
    Write-RuntimeLog $switchLog "AVATAR_POINTER_COMMITTED id=$AvatarId"
}


function Get-RunningAvatarId {
    # What the service is ACTUALLY running right now. Delegated: the supervisor reads
    # the id off the live process command line and falls back to /api/admin/runtime,
    # and live process truth always beats the pointer file. Empty string when nothing
    # healthy is listening.
    $status = Get-AvatarStatus $script:Facts
    return [string]$status.avatar_id
}


# Test-AvatarServiceReady and Get-AvatarOrphans used to live here. Both are gone on
# purpose: readiness is Get-AvatarStatus.health_ok (the supervisor probes
# /api/admin/runtime, the same "the service answers its own API" test one level
# deeper), and the orphan rule is the supervisor's Get-AvatarOrphanWorkers. A second
# copy of either would be a second definition of "is 8010 up", which is the whole bug
# this change removes.


function Stop-AvatarService {
    # The teardown is Stop-AvatarTree, the same one `Avatar-Supervisor -Action Stop`
    # runs: owner wrapper first, then the tracked launcher tree through the production
    # helper, then a sweep for proven orphans, then a MEASUREMENT of the port (no
    # LISTEN row + connect refused + bindable) before it returns.
    #
    # Deliberately NOT Stop-ProviderWorker: that helper resolves the expected
    # executable from worker.executable_suffix, and for this provider the suffix
    # is a bare "python.exe", which sends it down the owners path -- a missing
    # owners entry then aborts the whole switch on a PID_OWNERSHIP_MISMATCH.
    $before = Get-AvatarStatus $script:Facts
    $oldPid = [int]$before.launcher_pid
    if (-not $oldPid) { $oldPid = [int]$before.tracked_pid }
    $stopped = Stop-AvatarTree $script:Facts 'SWITCH_AWAY'
    foreach ($orphan in @($stopped.killed)) {
        Write-RuntimeLog $switchLog "AVATAR_TREE_KILLED pid=$orphan"
    }
    Write-RuntimeLog $switchLog ('AVATAR_SWITCH_PORT_RELEASED old_pid={0} waited_s={1} listeners={2} bindable={3}' -f `
        $oldPid, $stopped.port.waited_s, $stopped.port.listeners, $stopped.port.bindable)
    return $oldPid
}


function Start-AvatarService {
    # DELEGATED to Invoke-AvatarStart, i.e. the same idempotent path every other caller
    # uses: same launch body (Start-GirlfriendAvatarNative is called exactly once, in
    # the supervisor), same CUDA PATH, same launch.env injection, same working
    # directory, same service_pids bookkeeping -- plus the state file write and the
    # ready poll. Returns $true only when the service answers its own API as the id we
    # asked for, which is what the rollback logic below depends on.
    param([string]$AvatarId)
    $app = Join-Path $script:RuntimeRoot 'avatar\app'
    foreach ($stem in @('stdout', 'stderr')) {
        $current = Join-Path $script:LogRoot "avatar-8010.$stem.log"
        if (Test-Path -LiteralPath $current) {
            Move-Item -LiteralPath $current -Destination "$current.prev" -Force
        }
    }
    $started = Invoke-AvatarStart $script:Facts $AvatarId $script:SwitchReadyTimeoutSec $script:SwitchOwnerMode 'SWITCH'
    if ([string]$started.verdict -ne 'STARTED') {
        Write-RuntimeLog $switchLog ('AVATAR_SWITCH_START_REFUSED verdict={0} wanted={1} running={2} error={3}' -f `
            $started.verdict, $AvatarId, [string]$started.running_id, [string]$started.status.last_error)
        return 0
    }
    Write-RuntimeLog $switchLog ('AVATAR_SWITCH_START_OK id={0} launcher_pid={1} listener_pid={2} owner_pid={3}' -f `
        $AvatarId, $started.status.launcher_pid, $started.status.listener_pid, $started.status.owner_pid)
    return 1
}


function Test-SwitchVerify {
    # V1: the live process really carries the target id -- read the same way every other
    #     caller reads it (Get-AvatarStatus: command line first, /api/admin/runtime
    #     second), not off the pointer file.
    # V2: the service is HEALTHY, i.e. something owns the socket, it answers, and it
    #     answers as RUNNING. Stop-AvatarTree zeroes service_pids.json['avatar'] on
    #     purpose, so "the tracked pid moved" is no longer a meaningful check; the port
    #     being released before the start is what replaces it.
    param([string]$TargetId, [int]$OldPid)
    $status = Get-AvatarStatus $script:Facts    if ([string]$status.state -ne 'RUNNING') { return "VERIFY_NOT_RUNNING state=$($status.state) launcher=$($status.launcher_pid) listener=$($status.listener_pid)" }    if ([string]$status.avatar_id -ne $TargetId) { return "VERIFY_ID_MISMATCH want=$TargetId running=$($status.avatar_id)" }
    if (-not $status.health_ok) { return 'VERIFY_SERVICE_NOT_READY' }
    if ($OldPid -gt 0 -and [int]$status.launcher_pid -eq $OldPid) { return "VERIFY_PID_UNCHANGED old=$OldPid" }
    Write-RuntimeLog $switchLog ('AVATAR_SWITCH_VERIFY_OK id={0} launcher_pid={1} listener_pid={2} owner_pid={3} old_pid={4}' -f `
        $TargetId, $status.launcher_pid, $status.listener_pid, $status.owner_pid, $OldPid)
    return ''
}


function Restore-PreviousAvatar {
    # The rollback limb shared by every failure path of a switch: put the old person
    # back through the SAME start primitive, verify it the same way, and only then move
    # the pointer back. Returns '' on success, otherwise the reason it could not be
    # done (the caller prints that as ROLLBACK_FAILED's reason).
    param([string]$OldId, [int]$OldPid)
    if (-not $OldId) { return 'no_previous_id' }
    $entry = Get-AvatarEntryById $OldId
    if (-not $entry) { return "previous_not_registered id=$OldId" }
    if (-not (Start-AvatarService $OldId)) { return "rollback_start_failed old=$OldId" }
    $back = Test-SwitchVerify $OldId $OldPid
    if ($back) { return "rollback_verify=$back" }
    Write-AvatarPointer $OldId
    return ''
}


function Invoke-Switch {
    param([string]$TargetId)
    Write-RuntimeLog $switchLog "AVATAR_SWITCH_BEGIN old_target=$TargetId mode=Switch"

    # --- PRECHECK ---------------------------------------------------------
    $entry = Get-AvatarEntryById $TargetId
    if (-not $entry) { Write-Output 'AVATAR_SWITCH_ABORT'; Write-RuntimeLog $switchLog "AVATAR_SWITCH_ABORT reason=target_not_registered id=$TargetId"; return }
    if (([string]$entry.state) -and ([string]$entry.state -ne 'ready')) {
        Write-Output 'AVATAR_SWITCH_ABORT'; Write-RuntimeLog $switchLog "AVATAR_SWITCH_ABORT reason=target_not_ready id=$TargetId state=$($entry.state)"; return
    }
    $status = Get-AvatarStatus $script:Facts
    $running = [string]$status.avatar_id
    if ($running -eq $TargetId) {
        Write-AvatarPointer $TargetId
        Write-Output 'AVATAR_SWITCH_ABORT'
        Write-RuntimeLog $switchLog "AVATAR_SWITCH_ABORT reason=already_running id=$TargetId"
        return
    }
    # Somebody else's process on 8010 is not ours to move: Stop-AvatarTree would refuse
    # to kill it and the new instance would fail to bind, so refuse up front instead.
    if (@($status.foreign_listener_pids).Count) {
        Write-Output 'AVATAR_SWITCH_ABORT'
        Write-RuntimeLog $switchLog ('AVATAR_SWITCH_ABORT reason=port_held_by_foreign_process pids={0}' -f ($status.foreign_listener_pids -join ','))
        return
    }
    # A live service whose person cannot be told apart is a live service we would be
    # killing blind. The old code asserted the port was FREE here, which can never hold
    # while something is listening; with the supervisor's status read there is no
    # reason to keep that, and refusing is the safer answer.
    if ($status.state -eq 'RUNNING' -and -not $running) {
        Write-Output 'AVATAR_SWITCH_ABORT'
        Write-RuntimeLog $switchLog 'AVATAR_SWITCH_ABORT reason=running_id_ambiguous'
        return
    }

    # --- RECORD + STOP ----------------------------------------------------
    $oldPid = [int]$status.launcher_pid
    if (-not $oldPid) { $oldPid = [int]$status.tracked_pid }
    $oldId = $running
    if (-not $oldId) { $oldId = Get-RegisteredActiveAvatarId }
    Write-RuntimeLog $switchLog "AVATAR_SWITCH_STOP old=$oldId old_pid=$oldPid"
    Stop-AvatarService | Out-Null

    # --- START ------------------------------------------------------------
    $started = $false
    try {
        $started = [bool](Start-AvatarService $TargetId)
        Write-RuntimeLog $switchLog "AVATAR_SWITCH_STARTED id=$TargetId ok=$started"
    } catch {
        Write-RuntimeLog $switchLog "AVATAR_SWITCH_START_FAILED id=$TargetId reason=$($_.Exception.Message)"
    }

    # --- VERIFY -----------------------------------------------------------
    # One merged path for "the start threw", "the start refused", "it came up but is
    # not the person we asked for" and "it came up and cannot answer": all four used to
    # need their own rollback limb and printed the same verdict anyway.
    $verify = 'START_FAILED'
    if ($started) { $verify = Test-SwitchVerify $TargetId $oldPid }
    if ($verify) {
        Write-RuntimeLog $switchLog "AVATAR_SWITCH_VERIFY_FAILED id=$TargetId detail=$verify"
        try { Stop-AvatarService | Out-Null } catch { Write-RuntimeLog $switchLog "AVATAR_SWITCH_ROLLBACK_STOP_FAILED $($_.Exception.Message)" }
        $why = Restore-PreviousAvatar $oldId $oldPid
        if (-not $why) {
            Write-Output 'AVATAR_SWITCH_FAILED ROLLBACK_OK'
            Write-RuntimeLog $switchLog "AVATAR_SWITCH_FAILED ROLLBACK_OK old=$oldId reason=$verify"
            return
        }
        Write-RuntimeLog $switchLog "AVATAR_SWITCH_FAILED ROLLBACK_FAILED reason=$why"
        Write-Output 'AVATAR_SWITCH_FAILED ROLLBACK_FAILED'
        return
    }

    # --- COMMIT -----------------------------------------------------------
    Write-AvatarPointer $TargetId
    Write-Output 'AVATAR_SWITCH_OK'
    Write-RuntimeLog $switchLog "AVATAR_SWITCH_OK old=$oldId new=$TargetId"
}


function Invoke-Sync {
    # The "同步一下" button: point the pointer at whatever 8010 really runs. No process
    # side effect at all, and it stays the pointer's only writer.
    $running = Get-RunningAvatarId
    if (-not $running) { Write-Output 'AVATAR_SWITCH_ABORT'; Write-RuntimeLog $switchLog 'AVATAR_SYNC_ABORT reason=service_not_running'; return }
    $entry = Get-AvatarEntryById $running
    if (-not $entry) { Write-Output 'AVATAR_SWITCH_ABORT'; Write-RuntimeLog $switchLog "AVATAR_SYNC_ABORT reason=not_registered id=$running"; return }
    Write-AvatarPointer $running
    Write-Output 'AVATAR_SWITCH_OK'
    Write-RuntimeLog $switchLog "AVATAR_SYNC_OK id=$running"
}


function Invoke-Restart {
    # The "重新启动" button after a failed rollback left the service down: start whoever
    # the pointer names, through the same idempotent primitive, and verify it.
    $target = Get-RegisteredActiveAvatarId
    if (-not $target) { Write-Output 'AVATAR_SWITCH_ABORT'; Write-RuntimeLog $switchLog 'AVATAR_RESTART_ABORT reason=no_pointer'; return }
    $oldPid = [int](Get-AvatarStatus $script:Facts).launcher_pid
    [void](Start-AvatarService $target)
    $verify = Test-SwitchVerify $target $oldPid
    if ($verify) { Write-Output 'AVATAR_SWITCH_FAILED ROLLBACK_FAILED'; Write-RuntimeLog $switchLog "AVATAR_RESTART_FAILED id=$target detail=$verify"; return }
    Write-Output 'AVATAR_SWITCH_OK'
    Write-RuntimeLog $switchLog "AVATAR_RESTART_OK id=$target"
}


# The Python scheduler parses these strings as its whole API, so an unexpected
# exception must not leave it reading a bare PowerShell error: say so in the contract's
# own vocabulary, then rethrow so the exit code still says failure.
try {
    switch ($Mode) {
        'Switch'  { Invoke-Switch $Target }
        'Sync'    { Invoke-Sync }
        'Restart' { Invoke-Restart }
    }
} catch {
    Write-RuntimeLog $switchLog "AVATAR_SWITCH_UNCAUGHT mode=$Mode target=$Target error=$($_.Exception.Message)"
    Write-Output 'AVATAR_SWITCH_FAILED ROLLBACK_FAILED'
    throw
}
