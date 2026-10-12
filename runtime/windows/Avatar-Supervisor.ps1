# Avatar-Supervisor.ps1 -- the ONE lifecycle entry for the :8010 digital human.
#
# Why this file exists: four entry points (Start-AI-Girlfriend.ps1, the scheduled
# task wrapper, avatar_admin.py, Switch-Girlfriend-Avatar.ps1) each carried their own
# idea of "is 8010 up and who owns it". None of them agreed, because
# service_pids.json['avatar'] records the VENV LAUNCHER while the process that
# actually holds the socket is the bundled interpreter it spawned. This script is the
# single place that launches, stops, reads and records :8010; every other file now
# asks it.
#
# Contract:
#   * Actions: Start | Stop | Status | Switch | Hold | Ensure  (+ FixTask, the
#     task-settings repair, because the wrapper must not be idle-killed).
#   * One shared implementation: every start funnels through Invoke-AvatarLaunch,
#     every stop through Stop-AvatarTree, every read through Get-AvatarStatus. There
#     is exactly one call to Start-GirlfriendAvatarNative in this file and no
#     Start-Process at all -- the launch body stays in scripts/Avatar-Launch-Common.
#   * Three PIDs, kept distinct: owner_pid (the powershell that holds the child and
#     waits on it = the task wrapper), launcher_pid (the venv python.exe that
#     Start-Process returned), listener_pid (the process really owning the socket,
#     found by a Win32_Process parent->child walk AND cross-checked against
#     Get-NetTCPConnection and against the service's own startup log line /
#     /api/admin/runtime answer).
#   * State persists to runtime\windows\avatar_runtime_state.json, atomically (unique
#     temp name + Move-Item -Force via Write-AtomicFile), on every transition.
#   * Start is idempotent: a healthy listener on the requested person is
#     ALREADY_RUNNING and kills nothing. A healthy listener on a DIFFERENT person is a
#     Switch, and Start refuses.
#   * Stop kills only provably-owned processes: our listen marker in the command line
#     AND the executable inside this repo. Never by image name -- 8765/8090 share it.
#   * SOT is the state file: a deliberate Stop writes STOPPED and nothing here
#     re-raises the service. No retry loop, no watchdog, and the task keeps its empty
#     <Triggers/>.
#
# Dot-sourceable: the switch script and the task shim consume the primitives; the
# dispatcher only runs when this file is itself the entry point.

[CmdletBinding()]
param(
    [ValidateSet('Start', 'Stop', 'Status', 'Switch', 'Hold', 'Ensure', 'FixTask')]
    [string]$Action = 'Status',
    [string]$AvatarId = '',
    [int]$ReadyTimeoutSec = 180,
    [ValidateSet('Auto', 'Task', 'Direct')][string]$OwnerMode = 'Auto',
    [switch]$Json
)

$ErrorActionPreference = 'Stop'
$script:SourcedIn = ($MyInvocation.InvocationName -eq '.')
$script:ProjectRoot = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
$script:StateFile = Join-Path $script:ProjectRoot 'runtime\windows\avatar_runtime_state.json'
$script:TaskName = 'GirlfriendAvatar8010_Manual'
$script:OwnerScript = Join-Path $script:ProjectRoot 'runtime\windows\Run-Girlfriend-Avatar8010.ps1'
$script:SupervisorScript = Join-Path $script:ProjectRoot 'runtime\windows\Avatar-Supervisor.ps1'
$script:StateSchema = 'avatar-lifecycle/1'
$script:SupLog = 'avatar-supervisor.log'
$script:OwnerLog = 'avatar-owner.log'
$script:AvatarExitCode = 0

# The two libraries stay the launch body's only owner. Re-dot-sourcing them would only
# re-run their load-time setup, so guard on the function they publish.
if (-not (Get-Command -CommandType Function -Name 'Start-GirlfriendAvatarNative' -ErrorAction SilentlyContinue)) {
    . (Join-Path $script:ProjectRoot 'scripts\Runtime-Common.ps1')
    . (Join-Path $script:ProjectRoot 'scripts\Avatar-Launch-Common.ps1')
}

# ---------------------------------------------------------------------------
# facts
# ---------------------------------------------------------------------------

function Get-AvatarFacts {
    # Port, the flag carrying the avatar id, the flag carrying the port, the health
    # base url, the two interpreters Runtime-Common already publishes -- all read off
    # the AVATAR manifest row. No literal provider name, port or switch in this file.
    $spec = Get-AvatarProviderSpec
    $port = [int](Get-ProviderHealthPort $spec)
    if ($port -le 0) { throw 'AVATAR_PORT_UNDECLARED' }
    $idFlag = Get-AvatarIdFlag $spec
    $portFlag = Get-AvatarPortFlag $spec
    if (-not $idFlag -or -not $portFlag) { throw 'AVATAR_FLAGS_UNDECLARED' }
    $health = Get-ProviderProp $spec 'health'
    return @{
        project_root = [string]$script:AppRoot
        port         = $port
        id_flag      = $idFlag
        port_flag    = $portFlag
        stop_marker  = ('{0} {1}' -f $portFlag, $port)
        base_url     = [string](Get-ProviderProp $health 'base_url')
        launcher_exe = [string]$script:AvatarPython
        runtime_exe  = [string]$script:S2SPythonRuntime
        pid_key      = 'avatar'
        provider     = [string]$spec.id
        spec         = $spec
        app_dir      = (Join-Path $script:RuntimeRoot 'avatar\app')
    }
}

function Get-AvatarLaunchPlan([hashtable]$F, [string]$Id) {
    # Expanded a second time purely to RECORD argv/env in the state file (the
    # injection itself stays inside Start-GirlfriendAvatarNative). launch.env is the
    # manifest's only sanctioned place for AVATAR_SEGMENT_GATE.
    $ctx = Get-ProviderLaunchContext $F.spec
    if ($Id) { $ctx['AVATAR_ID'] = $Id }
    $plan = Expand-ProviderLaunch $F.spec $ctx
    $envMap = @{}
    foreach ($k in @($plan.env.Keys)) { $envMap[[string]$k] = [string]$plan.env[$k] }
    return [pscustomobject]@{ args = @($plan.args); env = $envMap }
}

function Resolve-AvatarTarget([string]$AvatarId, [hashtable]$F) {
    # The launch body's own priority, spelled out so the state file can say WHERE the
    # id came from: explicit param > active_avatar pointer (only when ready) >
    # manifest placeholder.
    if ($AvatarId) { return [pscustomobject]@{ id = $AvatarId; source = 'explicit' } }
    $pointer = Get-RegisteredActiveAvatarId
    if ($pointer) { return [pscustomobject]@{ id = [string]$pointer; source = 'pointer' } }
    $ctx = Get-ProviderLaunchContext $F.spec
    $ph = [string]$ctx['AVATAR_ID']
    if ($ph) { return [pscustomobject]@{ id = $ph; source = 'manifest_placeholder' } }
    throw 'AVATAR_TARGET_UNRESOLVABLE no_explicit_no_pointer_no_placeholder'
}

# ---------------------------------------------------------------------------
# state file
# ---------------------------------------------------------------------------

function Get-EmptyAvatarState {
    return [pscustomobject]@{
        schema            = $script:StateSchema
        avatar_id         = ''
        avatar_id_source  = ''
        state             = 'UNOWNED'
        generation        = 0
        owner_pid         = 0
        owner_mode        = ''
        supervisor_pid    = 0
        launcher_pid      = 0
        listener_pid      = 0
        listener_pids     = @()
        started_at        = ''
        exit_code         = $null
        exit_reason       = ''
        last_error        = ''
        death_at          = ''
        death_evidence    = ''
        task_name         = $script:TaskName
        desired_avatar_id = ''
        request_pid       = 0
        port              = 0
        env               = [pscustomobject]@{}
        log_paths         = [pscustomobject]@{}
        updated_at        = ''
    }
}

function Read-AvatarStateFile {
    # Never throws. An unparseable file is reported through $script:StateFileError --
    # :8010 status cannot depend on a JSON read succeeding. The shared-mode fallback is
    # for the window where the service itself has the file open.
    $script:StateFileError = ''
    if (-not (Test-Path -LiteralPath $script:StateFile)) { return $null }
    try { return (Read-JsonFile $script:StateFile) }
    catch {
        $text = Read-AvatarSharedText $script:StateFile 0
        if ($text) {
            try { return (ConvertFrom-Json -InputObject $text) } catch { $script:StateFileError = $_.Exception.Message }
        } else { $script:StateFileError = 'read denied' }
        return $null
    }
}

function Get-AvatarLogPaths {
    return [pscustomobject]@{
        stdout     = (Join-Path $script:LogRoot 'avatar-8010.stdout.log')
        stderr     = (Join-Path $script:LogRoot 'avatar-8010.stderr.log')
        heartbeat  = (Join-Path $script:LogRoot 'avatar-8010.heartbeat.log')
        exit_witness = (Join-Path $script:LogRoot 'avatar-8010.exitwitness.log')
        launch     = (Join-Path $script:LogRoot 'avatar-launch.log')
        owner      = (Join-Path $script:LogRoot $script:OwnerLog)
        supervisor = (Join-Path $script:LogRoot $script:SupLog)
        switch     = (Join-Path $script:LogRoot 'avatar-switch.log')
        state_file = $script:StateFile
    }
}

function Write-AvatarState([hashtable]$Patch, [hashtable]$F) {
    # Merge onto whatever is on disk (a STARTING write must not erase the previous
    # generation's history), stamp updated_at, land it atomically, then re-read it:
    # the file must never be left unparseable.
    if (-not $F) { $F = Get-AvatarFacts }
    $base = Read-AvatarStateFile
    if (-not $base) { $base = Get-EmptyAvatarState }
    if ($Patch) {
        foreach ($key in @($Patch.Keys)) { Set-ObjectValue $base $key $Patch[$key] }
    }
    Set-ObjectValue $base 'schema' $script:StateSchema
    Set-ObjectValue $base 'updated_at' ((Get-Date).ToString('o'))
    if (-not [string]$base.task_name) { Set-ObjectValue $base 'task_name' $script:TaskName }
    if ([int]$base.port -le 0) { Set-ObjectValue $base 'port' ([int]$F.port) }
    $supervisor = [pscustomobject]@{
        name     = (Split-Path -Leaf $script:SupervisorScript)
        pid      = $PID
        schema   = $script:StateSchema
        task     = $script:TaskName
        actions  = 'Start|Stop|Status|Switch|Hold|Ensure|FixTask'
    }
    Set-ObjectValue $base 'supervisor' $supervisor
    Set-ObjectValue $base 'supervisor_pid' $PID
    $json = $base | ConvertTo-Json -Depth 8
    # The move can lose a race with the service's own reader: Python's open() does not
    # share DELETE, so replacing the file it has open fails with a transient
    # access-denied. Retry the whole atomic write; a failed attempt still leaves the
    # PREVIOUS valid document in place (Write-AtomicFile cleans its own temp).
    $written = $false
    $lastWriteError = ''
    for ($attempt = 1; $attempt -le 4; $attempt++) {
        try {
            Write-AtomicFile $script:StateFile $json
            $written = $true
            break
        } catch {
            $lastWriteError = $_.Exception.Message
            Start-Sleep -Milliseconds 250
        }
    }
    if (-not $written) { throw ('STATE_FILE_WRITE_FAILED attempts=4 error=' + $lastWriteError) }
    $parsed = $null
    try { $parsed = Read-JsonFile $script:StateFile } catch { $parsed = $null }
    if (-not $parsed) { throw "STATE_FILE_UNPARSEABLE_AFTER_WRITE path=$($script:StateFile)" }
    return $parsed
}

# ---------------------------------------------------------------------------
# process identity -- the three PIDs
# ---------------------------------------------------------------------------

function Get-AvatarProcess([int]$Id) {
    if ($Id -le 0) { return $null }
    return (Get-CimInstance Win32_Process -Filter "ProcessId=$Id" -ErrorAction SilentlyContinue | Select-Object -First 1)
}

# OWNERSHIP REPAIR 2026-09-25: a PID alone cannot identify a generation. Every lifecycle
# write now records the process birth (CreationTime) next to the pid, and every admission
# decision re-checks both. These two helpers are the shared vocabulary.

function Get-AvatarBirthStringFromCim($Value) {
    # Get-CimInstance de-serializes CIM_DATETIME into a DateTime object on both
    # Windows PowerShell 5.1 and pwsh 7.x (verified on this machine: pwsh 7.6.6);
    # a raw DMTF string can still arrive from other CIM paths. Accept both shapes.
    if ($null -eq $Value) { return '' }
    if ($Value -is [DateTime]) { return $Value.ToString('o') }
    try { return ([System.Management.ManagementDateTimeConverter]::ToDateTime([string]$Value)).ToString('o') } catch { return '' }
}

function Get-AvatarProcessBirth([int]$Id) {
    # Birth of a live pid as a sortable local DateTime string ('o'), or '' when unknown.
    if ($Id -le 0) { return '' }
    $proc = Get-CimInstance Win32_Process -Filter "ProcessId=$Id" -ErrorAction SilentlyContinue | Select-Object -First 1
    if (-not $proc) { return '' }
    return (Get-AvatarBirthStringFromCim $proc.CreationDate)
}

function Test-AvatarRecordedGenerationProcess($Proc, $File) {
    # TRUE only when (pid, birth) matches what the state file recorded as THIS
    # generation's launcher or listener. A bare pid match with no recorded birth is
    # answered TRUE on purpose (an old document cannot prove the process is foreign,
    # and the reaper must not kill what it cannot disown). A pid match whose birth
    # DISAGREES is a PID reuse: FALSE, the process is not the recorded generation.
    if (-not $Proc -or -not $File) { return $false }
    $pidMatch = $false
    $expected = ''
    $fileLauncher = 0
    $fileListener = 0
    try { $fileLauncher = [int]$File.launcher_pid } catch { $fileLauncher = 0 }
    try { $fileListener = [int]$File.listener_pid } catch { $fileListener = 0 }
    $procId = 0
    try { $procId = [int]$Proc.ProcessId } catch { $procId = 0 }
    if ($fileLauncher -gt 0 -and $procId -eq $fileLauncher) {
        $pidMatch = $true
        if ($File.PSObject.Properties['launcher_start_time']) { $expected = [string]$File.launcher_start_time }
    }
    if (-not $pidMatch -and $fileListener -gt 0 -and $procId -eq $fileListener) {
        $pidMatch = $true
        if ($File.PSObject.Properties['listener_start_time']) { $expected = [string]$File.listener_start_time }
    }
    if (-not $pidMatch) { return $false }
    if (-not $expected) { return $true }
    $birth = ''
    try {
        $birth = ([System.Management.ManagementDateTimeConverter]::ToDateTime([string]$Proc.CreationDate)).ToString('o')
    } catch { $birth = '' }
    if (-not $birth) { return $true }
    try {
        return ([Math]::Abs(([DateTime]::Parse($expected) - [DateTime]::Parse($birth)).TotalSeconds) -le 5)
    } catch { return $true }
}

function Test-AvatarOwnerBirthMatches($Proc, [string]$ExpectedBirth) {
    # The owner twin of the rule above: the recorded owner pid is only OURS when the
    # process behind the pid was born when the file says it was. No expectation on file
    # (legacy document) -> TRUE, legacy behaviour. Birth unreadable -> TRUE (cannot
    # disprove). Birth readable and different -> FALSE, PID reuse.
    if (-not $Proc) { return $false }
    if (-not $ExpectedBirth) { return $true }
    $birth = ''
    try {
        $birth = ([System.Management.ManagementDateTimeConverter]::ToDateTime([string]$Proc.CreationDate)).ToString('o')
    } catch { $birth = '' }
    if (-not $birth) { return $true }
    try {
        return ([Math]::Abs(([DateTime]::Parse($ExpectedBirth) - [DateTime]::Parse($birth)).TotalSeconds) -le 5)
    } catch { return $true }
}

function Get-AvatarActorTag {
    # Who is asking for this kill, in the log's own words. Unelevated this box cannot
    # name the process that ended another one (no 4688/4689, ProcessStopTrace denied),
    # so the one half of that evidence we CAN produce is the record of every kill WE
    # decide on: pid plus the command line behind it. Without it, a reap by a concurrent
    # `-Action Start` and a reap by the Hold wrapper look identical in the log.
    $cmd = ''
    try {
        $self = Get-CimInstance Win32_Process -Filter ("ProcessId=" + $PID) -ErrorAction SilentlyContinue | Select-Object -First 1
        if ($self) { $cmd = [string]$self.CommandLine }
    } catch { $cmd = '' }
    if ($cmd.Length -gt 180) { $cmd = $cmd.Substring(0, 180) }
    if (-not $cmd) { $cmd = '<unreadable>' }
    return ('actor_pid={0} actor="{1}"' -f $PID, $cmd)
}

function Test-AvatarOwnedWorker($Proc, [hashtable]$F) {
    # The only admission ticket for killing a python: our listen marker in the command
    # line AND the executable inside this repo. Both, always. 8765/8090 share the
    # image name 'python.exe' and would otherwise be fair game.
    if (-not $Proc) { return $false }
    $cmd = [string]$Proc.CommandLine
    if (-not $cmd) { return $false }
    if ($cmd -notlike ('*{0}*' -f $F.stop_marker)) { return $false }
    $exe = [string]$Proc.ExecutablePath
    if (-not $exe) { return $false }
    $root = ([string]$F.project_root).TrimEnd('\').ToLowerInvariant()
    if ($exe.ToLowerInvariant().StartsWith($root + [System.IO.Path]::DirectorySeparatorChar)) { return $true }
    return [bool](($exe -ieq $F.launcher_exe) -or ($exe -ieq $F.runtime_exe))
}

function Test-AvatarOwnedOwner($Proc) {
    # The owner admission ticket: a powershell/pwsh whose command line names THIS
    # wrapper, or names THIS supervisor WITH the Hold action. Reading the supervisor
    # (`-Action Status`) from a terminal must never be mistaken for the owner, and an
    # image name alone is never enough -- this box runs several powershells.
    if (-not $Proc) { return $false }
    $cmd = [string]$Proc.CommandLine
    if (-not $cmd) { return $false }
    $name = [string]$Proc.Name
    if ($name -inotmatch '^(powershell|pwsh)(\.exe)?$') { return $false }
    $ownerLeaf = Split-Path -Leaf $script:OwnerScript
    if ($cmd -like ('*{0}*' -f $ownerLeaf)) { return $true }
    $supLeaf = Split-Path -Leaf $script:SupervisorScript
    if ($cmd -like ('*{0}*' -f $supLeaf) -and $cmd -imatch '-\w*Action(\s+):?\s*"?\x27?Hold') { return $true }
    return $false
}

function Get-AvatarSocketOwners([hashtable]$F) {
    # What the OS says owns the LISTEN socket, split into ours / not ours.
    $mine = @()
    $foreign = @()
    $rows = @(Get-NetTCPConnection -LocalPort ([int]$F.port) -State Listen -ErrorAction SilentlyContinue)
    $seen = @()
    foreach ($row in $rows) {
        $ownerId = [int]$row.OwningProcess
        if ($ownerId -le 0) { continue }
        if ($seen -contains $ownerId) { continue }
        $seen += $ownerId
        $proc = Get-AvatarProcess $ownerId
        if (Test-AvatarOwnedWorker $proc $F) {
            $mine += [pscustomobject]@{ pid = $ownerId; source = 'nettcp'; exe = [string]$proc.ExecutablePath }
        } else {
            $foreign += [pscustomobject]@{ pid = $ownerId; exe = [string]$proc.ExecutablePath; cmd = [string]$proc.CommandLine }
        }
    }
    return [pscustomobject]@{ owned = @($mine); foreign = @($foreign); rows = @($rows) }
}

function Get-AvatarDescendantWorkers([int]$RootPid, [hashtable]$F) {
    # Parent->child walk (the library's own recursive helper), filtered through the
    # same identity assert, so a walk can never name a stranger.
    if ($RootPid -le 0) { return @() }
    $out = @()
    foreach ($item in @(Get-DescendantProcesses $RootPid)) {
        if (-not (Test-AvatarOwnedWorker $item $F)) { continue }
        $out += [pscustomobject]@{ pid = [int]$item.ProcessId; depth = [int]$item.Depth; source = 'tree'; exe = [string]$item.ExecutablePath }
    }
    return @($out)
}

function Get-AvatarOrphanWorkers([hashtable]$F) {
    # The re-exec'd interpreter that outlived its launcher: marker + repo executable +
    # parent already gone. The same three-condition rule the switch script has used in
    # production, now with one owner.
    #
    # OWNERSHIP REPAIR 2026-09-25 (generation safety): the reaper may only take
    # processes it can prove belong to a DEAD generation. A process the state file
    # still names as this generation's launcher or listener -- matched by pid AND
    # recorded birth -- is exactly the "launcher exited, listener legitimately alive"
    # shape, and reaping it because its parent happened to be gone is how a sweep
    # kills a healthy service. Such processes are spared here and reported in the
    # log; stop remains complete because the teardown reaches them through the
    # socket-owner and descendant walks instead. Cannot prove -> not killed.
    $out = @()
    $spared = @()
    $file = Read-AvatarStateFile
    foreach ($proc in @(Get-CimInstance Win32_Process -ErrorAction SilentlyContinue)) {
        if (-not (Test-AvatarOwnedWorker $proc $F)) { continue }
        if (-not (Get-Process -Id ([int]$proc.ParentProcessId) -ErrorAction SilentlyContinue)) {
            if (Test-AvatarRecordedGenerationProcess $proc $file) {
                $spared += [int]$proc.ProcessId
                continue
            }
            $out += [pscustomobject]@{ pid = [int]$proc.ProcessId; depth = 0; source = 'orphan'; exe = [string]$proc.ExecutablePath }
        }
    }
    if (@($spared).Count) {
        Write-RuntimeLog $script:SupLog ('ORPHAN_REAPER_SPARED pids={0} reason=recorded_current_generation pid+birth match state file; not proven to be a dead generation' -f (@($spared | Sort-Object -Unique) -join ','))
    }
    return @($out)
}

function Test-AvatarPortBindable([hashtable]$F) {
    # "Is 8010 actually free" is a bind test, not a hope.
    $sock = $null
    try {
        $sock = New-Object System.Net.Sockets.Socket([System.Net.Sockets.AddressFamily]::InterNetwork, [System.Net.Sockets.SocketType]::Stream, [System.Net.Sockets.ProtocolType]::Tcp)
        $endpoint = New-Object System.Net.IPEndPoint([System.Net.IPAddress]::Any, [int]$F.port)
        $sock.Bind($endpoint)
        return $true
    } catch { return $false }
    finally { if ($sock) { try { $sock.Close() } catch {} } }
}

function Wait-AvatarPortReleased([hashtable]$F, [int]$TimeoutSec = 30) {
    # The Switch precondition: no LISTEN row AND connect refused AND bindable. Returns
    # the measurement; never throws, so the caller decides what a failure means.
    $watch = [System.Diagnostics.Stopwatch]::StartNew()
    $info = $null
    while ($watch.Elapsed.TotalSeconds -lt $TimeoutSec) {
        $info = Get-AvatarSocketOwners $F
        $listening = @($info.rows).Count
        $tcp = Test-Tcp '127.0.0.1' ([int]$F.port) 400
        $bindable = Test-AvatarPortBindable $F
        if ($listening -eq 0 -and -not $tcp -and $bindable) {
            return [pscustomobject]@{ released = $true; listeners = 0; tcp = $tcp; bindable = $bindable; owned_pids = @(); foreign_pids = @(); waited_s = [math]::Round($watch.Elapsed.TotalSeconds, 1) }
        }
        Start-Sleep -Milliseconds 400
    }
    $info = Get-AvatarSocketOwners $F
    return [pscustomobject]@{
        released     = $false
        listeners    = @($info.rows).Count
        tcp          = (Test-Tcp '127.0.0.1' ([int]$F.port) 400)
        bindable     = (Test-AvatarPortBindable $F)
        owned_pids   = @($info.owned | ForEach-Object pid)
        foreign_pids = @($info.foreign | ForEach-Object pid)
        waited_s     = [math]::Round($watch.Elapsed.TotalSeconds, 1)
    }
}

# ---------------------------------------------------------------------------
# the task
# ---------------------------------------------------------------------------

function Get-AvatarTaskInfo {
    # Read-only view of what may or may not re-raise 8010. PS 5.1 trap: an empty
    # <Triggers/> reads back with a .Count that is not the number of triggers, so this
    # reports three independent reads and lets the XML settle it.
    $out = [pscustomobject]@{
        exists                = $false
        state                 = ''
        triggers_count_prop   = -1
        triggers_measure      = -1
        triggers_selfclosing  = $false
        trigger_bodies        = -1
        idle_stop_on_idle_end = $null
        idle_wait_timeout     = ''
        idle_duration         = ''
        execution_time_limit  = ''
        multiple_instances    = ''
        restart_count         = $null
        unified_engine        = $null
        principal_logon_type  = ''
        principal_user_id     = ''
        action_execute        = ''
        action_arguments      = ''
        action_workdir        = ''
    }
    $task = Get-ScheduledTask -TaskName $script:TaskName -ErrorAction SilentlyContinue
    if (-not $task) { return $out }
    $out.exists = $true
    $out.state = [string]$task.State
    try { $out.triggers_count_prop = [int]$task.Triggers.Count } catch { $out.triggers_count_prop = -1 }
    $out.triggers_measure = (@($task.Triggers) | Measure-Object).Count
    [xml]$doc = [string](Export-ScheduledTask -TaskName $script:TaskName)
    $out.triggers_selfclosing = ($doc.OuterXml -match '<Triggers\s*/>')
    $list = $doc.GetElementsByTagName('Trigger')
    $out.trigger_bodies = [int]$list.Count
    $settings = $task.Settings
    $out.execution_time_limit = [string]$settings.ExecutionTimeLimit
    $out.multiple_instances = [string]$settings.MultipleInstances
    $out.restart_count = $settings.RestartCount
    $out.unified_engine = [bool]$settings.UseUnifiedSchedulingEngine
    $idle = $settings.IdleSettings
    if ($idle) {
        $out.idle_stop_on_idle_end = [bool]$idle.StopOnIdleEnd
        $out.idle_wait_timeout = [string]$idle.WaitTimeout
        $out.idle_duration = [string]$idle.Duration
    }
    $principal = $task.Principal
    if ($principal) {
        $out.principal_logon_type = [string]$principal.LogonType
        $out.principal_user_id = [string]$principal.UserId
    }
    $action = @($task.Actions) | Select-Object -First 1
    if ($action) {
        $out.action_execute = [string]$action.Execute
        $out.action_arguments = [string]$action.Arguments
        $out.action_workdir = [string]$action.WorkingDirectory
    }
    return $out
}

function Resolve-AvatarActionPath([string]$Execute, [string]$WorkDir) {
    # Task Scheduler records the Action as 'powershell.exe', NOT a full path, so
    # Test-Path on that string is always false -- a plain 'is this file there' guard
    # would reject a perfectly good task (it did, on the first real Task-mode run).
    # Resolve it the way the scheduler resolves it: absolute paths as given, then
    # relative to the task's working directory, then PATH. '' means "nothing real
    # behind this Action" and the caller refuses to start through it -- the whole point
    # of the guard is that a name must never be trusted as a FilePath.
    if (-not $Execute) { return '' }
    if ([System.IO.Path]::IsPathRooted($Execute)) {
        if (Test-Path -LiteralPath $Execute) { return [string](Resolve-Path -LiteralPath $Execute).Path }
        return ''
    }
    if ($WorkDir) {
        $candidate = Join-Path $WorkDir $Execute
        if (Test-Path -LiteralPath $candidate) { return [string](Resolve-Path -LiteralPath $candidate).Path }
    }
    $app = Get-Command $Execute -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($app -and $app.Source) { return [string]$app.Source }
    return ''
}


function Set-AvatarTaskIdleSetting {
    # Task Scheduler must not be able to idle-kill the wrapper -- an idle-killed owner
    # is indistinguishable from a crash. The registered Settings object is MUTATED in
    # place rather than rebuilt: New-ScheduledTaskSettingsSet on this box has no
    # -UseUnifiedSchedulingEngine parameter at all, and rebuilding silently resets
    # fields nobody asked about. Only -Settings goes to Set-ScheduledTask, so the
    # Principal (InteractiveToken/Limited), the empty <Triggers/>, the Action and the
    # working directory stay as registered -- the two XML guards below fail loudly if
    # that ever stops being true.
    $task = Get-ScheduledTask -TaskName $script:TaskName -ErrorAction SilentlyContinue
    if (-not $task) { return [pscustomobject]@{ changed = $false; reason = 'TASK_MISSING' } }
    $idle = $task.Settings.IdleSettings
    $stopNow = $false
    $waitNow = ''
    if ($idle) { $stopNow = [bool]$idle.StopOnIdleEnd; $waitNow = [string]$idle.WaitTimeout }
    if (-not $stopNow -and $waitNow -eq 'PT0S') {
        return [pscustomobject]@{ changed = $false; reason = 'ALREADY_CORRECT'; stop_on_idle_end = $stopNow; wait_timeout = $waitNow }
    }
    $before = [string](Export-ScheduledTask -TaskName $script:TaskName)
    if (-not $idle) {
        $task.Settings | Add-Member -NotePropertyName IdleSettings -NotePropertyValue (New-ScheduledTaskSettingsSet).IdleSettings -Force
        $idle = $task.Settings.IdleSettings
    }
    $idle.StopOnIdleEnd = $false
    # Assign the CIM duration as the STRING 'PT0S'. [TimeSpan]::Zero is accepted by the
    # in-memory object but the exporter renders it as '00:00:00', which Task Scheduler
    # rejects on import ("XML document is not well formed or out-of-range value") and
    # the whole registration fails. Keep this file pure ASCII: PS 5.1 decodes a
    # BOM-less UTF-8 .ps1 as codepage 936 on this box.
    $idle.WaitTimeout = 'PT0S'
    $idle.RestartOnIdle = $false
    $null = Set-ScheduledTask -TaskName $script:TaskName -Settings $task.Settings
    $after = [string](Export-ScheduledTask -TaskName $script:TaskName)
    $beforeTriggers = [regex]::Match($before, '(?s)<Triggers.*?</Triggers>|<Triggers\s*/>').Value
    $afterTriggers = [regex]::Match($after, '(?s)<Triggers.*?</Triggers>|<Triggers\s*/>').Value
    if ($beforeTriggers -notmatch '<Triggers\s*/>' -or $afterTriggers -notmatch '<Triggers\s*/>') {
        throw ('TASK_TRIGGERS_CHANGED before=[{0}] after=[{1}]' -f $beforeTriggers, $afterTriggers)
    }
    foreach ($section in @('Actions', 'Principals')) {
        $pattern = '(?s)<' + $section + '[^>]*>.*?</' + $section + '>'
        $b = [regex]::Match($before, $pattern).Value
        $a = [regex]::Match($after, $pattern).Value
        if ($b -ne $a) { throw ('TASK_SECTION_CHANGED section={0} before=[{1}] after=[{2}]' -f $section, $b, $a) }
    }
    $info = Get-AvatarTaskInfo
    Write-RuntimeLog $script:SupLog ('TASK_IDLE_FIXED stop_on_idle_end={0} wait_timeout={1} triggers_measure={2} unified={3} multiple={4}' -f `
        [string]$info.idle_stop_on_idle_end, [string]$info.idle_wait_timeout, [string]$info.triggers_measure, [string]$info.unified_engine, [string]$info.multiple_instances)
    return [pscustomobject]@{ changed = $true; reason = 'APPLIED'; stop_on_idle_end = $info.idle_stop_on_idle_end; wait_timeout = $info.idle_wait_timeout }
}

# ---------------------------------------------------------------------------
# independent cross-checks (the service's own account of itself)
# ---------------------------------------------------------------------------

function Read-AvatarSharedText([string]$Path, [int]$MaxBytes) {
    # A log the service still holds open is NOT an error: [System.IO.File]::ReadAllText
    # asks for FileShare.Read and fails with "file is being used by another process",
    # while FileShare.ReadWrite reads it fine. Tail-clip so a fat log cannot decide the
    # shape of a status read.
    $fs = $null
    $sr = $null
    try {
        $fs = [System.IO.File]::Open($Path, [System.IO.FileMode]::Open, [System.IO.FileAccess]::Read, [System.IO.FileShare]::ReadWrite)
        if ($MaxBytes -gt 0 -and $fs.Length -gt $MaxBytes) {
            $null = $fs.Seek((-1 * $MaxBytes), [System.IO.SeekOrigin]::End)
        }
        $sr = New-Object System.IO.StreamReader($fs, [System.Text.Encoding]::UTF8)
        return $sr.ReadToEnd()
    } catch { return '' }
    finally {
        if ($sr) { try { $sr.Dispose() } catch {} }
        if ($fs) { try { $fs.Dispose() } catch {} }
    }
}

function Get-AvatarStartupLogIdentity([hashtable]$F) {
    # The service prints
    #   'avatar session lifecycle ready pid=<pid> listener=[...] max_session=...'
    # which is a read from INSIDE the process that no PID walk of mine can influence --
    # the only truly independent confirmation that listener_pid is the booting service
    # and not some other python on the box. Python logging lands on stderr; both files
    # are scanned and the last match wins (Start-Process truncates them at every launch,
    # so a match belongs to the current boot).
    $selfPid = 0
    $listeners = @()
    $line = ''
    foreach ($name in @('avatar-8010.stderr.log', 'avatar-8010.stdout.log')) {
        $path = Join-Path $script:LogRoot $name
        if (-not (Test-Path -LiteralPath $path)) { continue }
        $text = Read-AvatarSharedText $path 262144
        if (-not $text) { continue }
        foreach ($m in [regex]::Matches($text, 'avatar session lifecycle ready pid=(\d+) listener=(\[[^\]]*\])')) {
            $selfPid = [int]$m.Groups[1].Value
            $listeners = @([regex]::Matches($m.Groups[2].Value, '\d+') | ForEach-Object { [int]$_.Value })
            $line = $m.Value
        }
    }
    return [pscustomobject]@{ self_pid = $selfPid; listener_pids = @($listeners); line = $line }
}

function Get-AvatarRuntimeEndpoint([hashtable]$F) {
    # GET /api/admin/runtime. It is a brand new build, so a missing or unparsable
    # answer is a normal 'ENDPOINT_DOWN' -- never an error, never a stopped service.
    try {
        $raw = Get-HttpJson ($F.base_url.TrimEnd('/') + '/api/admin/runtime') 3
        if (-not $raw) { return [pscustomobject]@{ ok = $false; error = 'empty response' } }
        $data = $null
        if ($raw.PSObject.Properties['data']) { $data = $raw.data }
        $proc = $null
        if ($data -and $data.PSObject.Properties['process']) { $proc = $data.process }
        $gate = ''
        if ($data -and $data.PSObject.Properties['gate'] -and $data.gate) { $gate = [string]$data.gate.mode }
        $argvId = ''
        $listenport = 0
        $transport = ''
        if ($data) {
            if ($data.PSObject.Properties['avatar_id']) { $argvId = [string]$data.avatar_id }
            if ($data.PSObject.Properties['listenport']) { $listenport = [int]$data.listenport }
            if ($data.PSObject.Properties['transport']) { $transport = [string]$data.transport }
        }
        $echo = [pscustomobject]@{}
        if ($proc) {
            $echo = [pscustomobject]@{
                state        = [string]$proc.state
                avatar_id    = [string]$proc.avatar_id
                owner_pid    = [int]$proc.owner_pid
                launcher_pid = [int]$proc.launcher_pid
                listener_pid = [int]$proc.listener_pid
                generation   = [string]$proc.generation
                exit_code    = [string]$proc.exit_code
                state_file   = [string]$proc.state_file
                supervisor   = [string]$proc.supervisor
            }
        }
        return [pscustomobject]@{
            ok             = $true
            code           = $(try { [int]$raw.code } catch { -1 })
            argv_avatar_id = $argvId
            argv_port      = $listenport
            transport      = $transport
            gate_mode      = $gate
            active_sessions = $(if ($data) { $data.active_sessions } else { $null })
            max_session    = $(if ($data) { $data.max_session } else { $null })
            process_echo   = $echo
        }
    } catch {
        return [pscustomobject]@{ ok = $false; error = $_.Exception.Message }
    }
}

# ---------------------------------------------------------------------------
# THE status read
# ---------------------------------------------------------------------------

function Get-AvatarStatus([hashtable]$F) {
    # Every consumer (actions, the switch script, the UI, the test) reads 8010 through
    # this one function, so the three PIDs cannot be conflated in one place and not
    # another. Live process truth always beats the file: a dead launcher is never
    # reported as a live service.
    if (-not $F) { $F = Get-AvatarFacts }
    $file = Read-AvatarStateFile
    $declared = 'UNOWNED'
    if ($file) { $declared = [string]$file.state }
    $trackedPid = 0
    try { $trackedPid = [int](Read-ServicePids).avatar } catch { $trackedPid = 0 }

    # launcher_pid -- the venv python.exe Start-Process returned.
    $launcherPid = 0
    $launcherProc = Get-AvatarProcess $trackedPid
    if (Test-AvatarOwnedWorker $launcherProc $F) {
        $launcherPid = $trackedPid
    } elseif ($file -and [int]$file.launcher_pid -gt 0) {
        $candidate = Get-AvatarProcess ([int]$file.launcher_pid)
        if (Test-AvatarOwnedWorker $candidate $F) { $launcherPid = [int]$file.launcher_pid }
    }

    # listener_pid -- the socket owner per the OS; the parent->child walk is the
    # second read and also the "model still loading, port not bound yet" signal.
    $sockets = Get-AvatarSocketOwners $F
    $ownedListenerPids = @($sockets.owned | ForEach-Object pid)
    $walk = @(Get-AvatarDescendantWorkers $launcherPid $F)
    $walkPids = @($walk | ForEach-Object pid)
    if (-not $ownedListenerPids.Count -and $walkPids.Count) { $ownedListenerPids = $walkPids }
    $listenerPid = 0
    if ($ownedListenerPids.Count) { $listenerPid = [int]$ownedListenerPids[0] }
    $orphans = @(Get-AvatarOrphanWorkers $F)
    $orphanPids = @($orphans | ForEach-Object pid)

    # owner_pid -- the powershell holding the child. Identity = pid + cmdline + birth:
    # the file branch re-checks the recorded owner_start_time, so a reused pid that a
    # NEW generation's wrapper happens to hold cannot be adopted as this file's owner.
    $ownerPid = 0
    $ownerMode = ''
    $ownerPidReuseSuspect = 0
    if ($file -and [int]$file.owner_pid -gt 0) {
        $candidate = Get-AvatarProcess ([int]$file.owner_pid)
        if (Test-AvatarOwnedOwner $candidate) {
            $expectedOwnerBirth = ''
            if ($file.PSObject.Properties['owner_start_time']) { $expectedOwnerBirth = [string]$file.owner_start_time }
            if (Test-AvatarOwnerBirthMatches $candidate $expectedOwnerBirth) {
                $ownerPid = [int]$file.owner_pid; $ownerMode = [string]$file.owner_mode
            } else {
                # pid alive, cmdline matches, birth does not: a reused pid. Refuse it here
                # AND bar the scan below from re-adopting the very same process.
                $ownerPidReuseSuspect = [int]$file.owner_pid
            }
        }
    }
    if (-not $ownerPid) {
        foreach ($proc in @(Get-CimInstance Win32_Process -ErrorAction SilentlyContinue)) {
            # Never read OURSELVES as the owner we are waiting for. The task wrapper is
            # an owner the moment the scheduler starts it, and its first act is this same
            # status read: counting that self-match as 'someone is already starting :8010'
            # made Hold answer ALREADY_STARTING and exit without launching anything -- the
            # real reason the first Task-mode run timed out rather than failed to start.
            if ([int]$proc.ProcessId -eq $PID) { continue }
            if ($ownerPidReuseSuspect -gt 0 -and [int]$proc.ProcessId -eq $ownerPidReuseSuspect) { continue }
            if (Test-AvatarOwnedOwner $proc) { $ownerPid = [int]$proc.ProcessId; $ownerMode = 'TASK'; break }
        }
    }
    if ($ownerPid -and -not $ownerMode) { $ownerMode = 'TASK' }

    # which person, read off the live command line (the pointer never wins here)
    $avatarId = ''
    if ($launcherPid -gt 0 -and $launcherProc) {
        $tokens = @(([string]$launcherProc.CommandLine) -split '\s+')
        $pending = $false
        foreach ($token in $tokens) {
            if ($token -ieq $F.id_flag) { $pending = $true; continue }
            if ($pending) { $avatarId = ([string]$token).Trim('"'); break }
            $pending = $false
        }
    }
    $endpoint = Get-AvatarRuntimeEndpoint $F
    $logIdentity = Get-AvatarStartupLogIdentity $F
    if (-not $avatarId -and $endpoint.ok -and $endpoint.argv_avatar_id) { $avatarId = [string]$endpoint.argv_avatar_id }
    if (-not $avatarId -and $file) { $avatarId = [string]$file.avatar_id }

    $tcp = Test-Tcp '127.0.0.1' ([int]$F.port) 500
    $healthy = [bool]($tcp -and $endpoint.ok -and $listenerPid -gt 0)

    $resolved = $null
    try { $resolved = Resolve-AvatarTarget '' $F } catch { $resolved = $null }

    $running = $healthy
    $booting = [bool](-not $running -and ($ownerPid -gt 0 -or $launcherPid -gt 0 -or $walkPids.Count -gt 0 -or ($declared -eq 'STARTING' -and $listenerPid -gt 0)))
    $derived = 'STOPPED'
    if ($running) { $derived = 'RUNNING' }
    elseif ($booting) { $derived = 'STARTING' }
    elseif ($declared -eq 'STOPPING') { $derived = 'STOPPING' }
    elseif ($declared -eq 'FAILED') { $derived = 'FAILED' }
    elseif (-not $file) { $derived = 'UNOWNED' }

    # Cross-check: is the pid I walked to the same pid the process itself reports?
    $agreement = 'ENDPOINT_DOWN'
    if ($endpoint.ok -or $logIdentity.self_pid) {
        $reported = @()
        if ($logIdentity.self_pid) { $reported += $logIdentity.self_pid }
        $reported += @($logIdentity.listener_pids)
        if ($endpoint.ok -and $endpoint.process_echo -and [int]$endpoint.process_echo.listener_pid -gt 0) {
            $reported += [int]$endpoint.process_echo.listener_pid
        }
        $reported = @($reported | Where-Object { $_ -gt 0 } | Sort-Object -Unique)
        if (-not $reported.Count) { $agreement = 'NO_REPORT' }
        elseif ($listenerPid -gt 0 -and (@($reported) -contains $listenerPid)) { $agreement = 'MATCH' }
        elseif ($listenerPid -gt 0) { $agreement = ('MISMATCH walk={0} reported={1}' -f $listenerPid, ($reported -join ',')) }
        else { $agreement = ('NO_WALK_LISTENER reported={0}' -f ($reported -join ',')) }
    }

    return [pscustomobject]@{
        schema              = $script:StateSchema
        state               = $derived
        declared_state      = $declared
        state_file_present  = [bool]$file
        state_file_path     = $script:StateFile
        state_file_error    = [string]$script:StateFileError
        avatar_id           = $avatarId
        desired_avatar_id   = $(if ($file) { [string]$file.desired_avatar_id } else { '' })
        target_avatar_id    = $(if ($resolved) { [string]$resolved.id } else { '' })
        target_source       = $(if ($resolved) { [string]$resolved.source } else { 'unresolved' })
        port                = [int]$F.port
        owner_pid           = $ownerPid
        owner_alive         = [bool]($ownerPid -gt 0)
        owner_mode          = $ownerMode
        launcher_pid        = $launcherPid
        launcher_alive      = [bool]($launcherPid -gt 0)
        listener_pid        = $listenerPid
        listener_pids       = @($ownedListenerPids)
        walk_pids           = @($walkPids)
        orphan_pids         = @($orphanPids)
        foreign_listener_pids = @($sockets.foreign | ForEach-Object pid)
        tracked_pid         = $trackedPid
        tcp_open            = $tcp
        health_ok           = $healthy
        generation          = $(if ($file) { [int]$file.generation } else { 0 })
        started_at          = $(if ($file) { [string]$file.started_at } else { '' })
        exit_code           = $(if ($file) { $file.exit_code } else { $null })
        exit_reason         = $(if ($file) { [string]$file.exit_reason } else { '' })
        last_error          = $(if ($file) { [string]$file.last_error } else { '' })
        death_at            = $(if ($file) { [string]$file.death_at } else { '' })
        death_evidence      = $(if ($file) { [string]$file.death_evidence } else { '' })
        updated_at          = $(if ($file) { [string]$file.updated_at } else { '' })
        env                 = $(if ($file -and $file.PSObject.Properties['env']) { $file.env } else { $null })
        log_paths           = Get-AvatarLogPaths
        task                = Get-AvatarTaskInfo
        endpoint            = $endpoint
        log_identity        = $logIdentity
        pid_agreement       = $agreement
        port_bindable       = [bool](-not $running -and -not $booting -and (Test-AvatarPortBindable $F))
    }
}

# ---------------------------------------------------------------------------
# the ONE launch / the ONE stop
# ---------------------------------------------------------------------------

function Get-AvatarLock([int]$WaitSec) {
    # One lifecycle call at a time. Global\ first, because that is the namespace this
    # box already uses for the stack-wide startup mutex; fall back to the session
    # namespace rather than letting a missing privilege become a missing service.
    $mutex = $null
    try { $mutex = New-Object System.Threading.Mutex($false, 'Global\Avatar8010Supervisor') }
    catch {
        try { $mutex = New-Object System.Threading.Mutex($false, 'Avatar8010Supervisor') } catch { return $null }
    }
    $got = $false
    try { $got = $mutex.WaitOne($WaitSec * 1000) } catch { $got = $false }
    if (-not $got) { try { $mutex.Dispose() } catch {}; return $null }
    return $mutex
}

function Release-AvatarLock($Mutex) {
    if (-not $Mutex) { return }
    try { $Mutex.ReleaseMutex() } catch {}
    try { $Mutex.Dispose() } catch {}
}

# The mutex the CURRENT lifecycle call owns. It lives in one script-scoped place on
# purpose: a passive wait for another process (the task wrapper coming up, the port
# binding) must never be paid for with the lock, because the thing being waited for
# takes that same lock to do its own idempotent start. Both the dispatcher and the
# Hold owner route their lock through this single variable so 'waiting' and 'holding'
# can never disagree about who owns it.
$script:AvatarLock = $null

function Exit-AvatarLockForHandoff {
    # Hand the mutex over for the duration of a wait. $true means 'I really was holding
    # it', and only then may the caller take it back afterwards.
    if (-not $script:AvatarLock) { return $false }
    Release-AvatarLock $script:AvatarLock
    $script:AvatarLock = $null
    return $true
}

function Enter-AvatarLockAfterHandoff([int]$WaitSec) {
    $lock = Get-AvatarLock $WaitSec
    $script:AvatarLock = $lock
    if (-not $lock) {
        Write-RuntimeLog $script:SupLog ('LOCK_NOT_RETAKEN wait_sec={0} continuing unlocked' -f $WaitSec)
        return $false
    }
    return $true
}

function Invoke-AvatarLaunch([hashtable]$F, [string]$AvatarId, [int]$ReadyTimeoutSec, [string]$OwnerMode, [string]$Trigger, [string]$SourceHint = '') {
    # THE launch. Every start of :8010 in this repository comes through here, and the
    # only thing it does process-side is call Start-GirlfriendAvatarNative -- which is
    # itself the only Start-Process in the stack. OwnerMode 'Task' hands that job to
    # the durable owner (the scheduled task's powershell) instead of doing it here, so
    # an agent's or a terminal's exit cannot take the service with it.
    $target = Resolve-AvatarTarget $AvatarId $F
    if ($SourceHint -and $AvatarId -and [string]$target.id -eq [string]$AvatarId) {
        # The caller already resolved this id (from the pointer, or from a queued task
        # request) and is handing it over. Re-resolving it here would relabel a
        # pointer-driven start as 'explicit' and the state file would record a
        # provenance that never happened.
        $target = [pscustomobject]@{ id = $AvatarId; source = $SourceHint }
    }
    $plan = Get-AvatarLaunchPlan $F ([string]$target.id)
    $before = Read-AvatarStateFile
    $beforeGen = 0
    if ($before) { $beforeGen = [int]$before.generation }
    $mode = $OwnerMode
    if ($mode -eq 'Auto') {
        $taskInfo = Get-AvatarTaskInfo
        $mode = 'Direct'
        if ($taskInfo.exists) { $mode = 'Task' }
    }
    Write-RuntimeLog $script:SupLog ('LAUNCH_REQUEST trigger={0} id={1} source={2} mode={3} gen_before={4}' -f `
        $Trigger, $target.id, $target.source, $mode, $beforeGen)

    if ($mode -eq 'Task') {
        $taskInfo = Get-AvatarTaskInfo
        Write-Host ('TASK FILEPATH={0}' -f $taskInfo.action_execute)
        Write-Host ('TASK ARGS={0}' -f $taskInfo.action_arguments)
        Write-Host ('TASK WORKDIR={0}' -f $taskInfo.action_workdir)
        Write-Host ('TASK TITLE={0}' -f $script:TaskName)
        if (-not (Test-Path -LiteralPath $script:OwnerScript)) { throw ('TASK_WRAPPER_SCRIPT_MISSING path={0}' -f $script:OwnerScript) }
        # The Action's Execute is recorded as a BARE name ('powershell.exe'), which the
        # scheduler resolves through PATH -- Test-Path on that literal is always false,
        # and the first real Task-mode run proved this guard rejecting a perfectly good
        # task. Resolve first, then assert; and assert the SCRIPT the Action names,
        # because that is the FilePath that actually decides who owns :8010.
        $declaredExe = [string]$taskInfo.action_execute
        $resolvedExe = $declaredExe
        if ($declaredExe -and -not [System.IO.Path]::IsPathRooted($declaredExe)) {
            $app = Get-Command $declaredExe -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
            if ($app) { $resolvedExe = [string]$app.Source }
        }
        if (-not $resolvedExe -or -not (Test-Path -LiteralPath $resolvedExe)) {
            throw ('TASK_ACTION_FILEPATH_MISSING execute={0} resolved={1}' -f $declaredExe, $resolvedExe)
        }
        Write-Host ('TASK RESOLVED EXE={0}' -f $resolvedExe)
        $declaredScript = [regex]::Match([string]$taskInfo.action_arguments, '-File\s+"([^"]+)"').Groups[1].Value
        if (-not $declaredScript) {
            throw ('TASK_ACTION_SCRIPT_UNDECLARED arguments={0}' -f [string]$taskInfo.action_arguments)
        }
        if (-not (Test-Path -LiteralPath $declaredScript)) {
            throw ('TASK_ACTION_SCRIPT_MISSING path={0}' -f $declaredScript)
        }
        if ([System.IO.Path]::GetFullPath($declaredScript) -ine [System.IO.Path]::GetFullPath($script:OwnerScript)) {
            # A task pointed at some other script is not this service's owner, however
            # its name reads. Refuse rather than hand it an owner_pid.
            throw ('TASK_ACTION_SCRIPT_NOT_OURS task_named={0} expected={1}' -f $declaredScript, $script:OwnerScript)
        }
        Write-Host ('TASK SCRIPT_VERIFIED={0}' -f $declaredScript)
        # A wrapper we have only just stopped can still be reported as Running by the
        # scheduler for a moment, and this task is registered MultipleInstances=IgnoreNew
        # -- starting into that would silently do nothing and then time out. Wait it out
        # rather than kill anything.
        $settle = (Get-Date).AddSeconds(20)
        while ((Get-Date) -lt $settle) {
            $t = (Get-ScheduledTask -TaskName $script:TaskName -ErrorAction SilentlyContinue)
            if (-not $t) { break }
            if ([string]$t.State -ne 'Running') { break }
            Start-Sleep -Milliseconds 500
        }
        $t = (Get-ScheduledTask -TaskName $script:TaskName -ErrorAction SilentlyContinue)
        if ($t -and [string]$t.State -eq 'Running') {
            throw ('TASK_INSTANCE_STILL_RUNNING task={0} nothing was started' -f $script:TaskName)
        }
        $null = Write-AvatarState @{
            desired_avatar_id = [string]$target.id
            request_pid       = $PID
            state             = 'STARTING'
            owner_mode        = 'TASK'
            avatar_id         = [string]$target.id
            last_error        = ''
            exit_reason       = ''
        } $F
        Start-ScheduledTask -TaskName $script:TaskName
        # From here on we only WATCH another process do the starting, so the mutex goes
        # to that process: its Hold takes exactly this lock for its own idempotent start,
        # and a caller that held it across the wait made the wrapper run unloaned while a
        # third caller could walk in behind it. Re-acquired before returning.
        $handed = Exit-AvatarLockForHandoff
        $cur = $null
        $deadline = (Get-Date).AddSeconds($ReadyTimeoutSec)
        try {
            while ((Get-Date) -lt $deadline) {
                $cur = Read-AvatarStateFile
                if ($cur -and [int]$cur.generation -gt $beforeGen) {
                    if ([string]$cur.state -eq 'FAILED') {
                        throw ('TASK_LAUNCH_FAILED gen={0} last_error={1}' -f [int]$cur.generation, [string]$cur.last_error)
                    }
                }
                # The derived read, not the file's word: the launch writes RUNNING the
                # moment Start-Process returns, long before the port answers, and a caller
                # that believed that text would report a healthy service that is not there.
                $live = Get-AvatarStatus $F
                if ([int]$live.generation -gt $beforeGen -and [string]$live.state -eq 'RUNNING') { break }
                # OWNERSHIP REPAIR 2026-09-25 (AI_GIRLFRIEND_AVATAR_PROCESS_OWNERSHIP_FINAL_REPAIR_01):
                # the death verdict below is legal ONLY once the owner has also died. During the
                # T2->T4 start window the state file still carries launcher_pid=0, service_pids
                # was just zeroed by the stop, :8010 is not bound yet and a fresh CIM read can
                # miss the newborn launcher -- three transient "not visible" reads that are NOT
                # a death. Judging death from them made the watcher throw TASK_CHILD_DIED while
                # the wrapper it queued was mid-launch (gen 40/43/44/45 on 2026-09-25 20:11-20:28:
                # every TASK_CHILD_DIED timestamped BEFORE its own LAUNCH_OK), and the switch's
                # rollback then killed the healthy new generation it had just started. The
                # owner (the task wrapper powershell running Hold) is alive exactly while the
                # start is in flight, so its death is the missing precondition.
                if ([int]$live.generation -gt $beforeGen -and -not $live.owner_alive -and -not $live.launcher_alive -and -not $live.listener_alive) {
                    throw ('TASK_CHILD_DIED gen={0} owner_pid={1} exit_reason={2} last_error={3}' -f `
                        [int]$live.generation, [int]$live.owner_pid, [string]$live.exit_reason, [string]$live.last_error)
                }
                Start-Sleep -Seconds 2
            }
        } finally {
            if ($handed) { $null = Enter-AvatarLockAfterHandoff 60 }
        }
        if (-not $cur -or [int]$cur.generation -le $beforeGen) {
            throw ('TASK_LAUNCH_TIMEOUT gen_before={0} timeout_s={1}' -f $beforeGen, $ReadyTimeoutSec)
        }
        # $cur is the last read taken INSIDE the watch loop, and that loop breaks on the
        # derived status -- so $cur can still be holding the pre-launch file, where
        # launcher_pid and listener_pid are both 0. Reporting those zeros is what turned
        # a start that had just succeeded into STARTED_UNHEALTHY with waited_s=182, and
        # it is why the caller could not say which person was now running. The live read
        # is the only thing worth returning: it merges the socket owner and the process
        # tree instead of trusting one line of JSON.
        $done = Get-AvatarStatus $F
        $repGen = [int]$cur.generation
        if ([int]$done.generation -gt 0) { $repGen = [int]$done.generation }
        $repLauncher = [int]$done.launcher_pid
        if (-not $repLauncher) { $repLauncher = [int]$cur.launcher_pid }
        $repListener = [int]$done.listener_pid
        if (-not $repListener) { $repListener = [int]$cur.listener_pid }
        Write-RuntimeLog $script:SupLog ('LAUNCH_OK gen={0} launcher_pid={1} listener_pid={2} owner_pid={3} mode=Task' -f `
            $repGen, $repLauncher, $repListener, [int]$done.owner_pid)
        return [pscustomobject]@{ launcher_pid = $repLauncher; listener_pid = $repListener; generation = $repGen; owner_pid = [int]$done.owner_pid; mode = 'Task' }
    }

    Write-Host ('LAUNCH FILEPATH={0}' -f $F.launcher_exe)
    Write-Host ('LAUNCH FILEPATH_EXISTS={0}' -f [string](Test-Path -LiteralPath $F.launcher_exe))
    Write-Host ('LAUNCH ARGS={0}' -f (@($plan.args) -join ' '))
    Write-Host ('LAUNCH WORKDIR={0}' -f $F.app_dir)
    Write-Host ('LAUNCH TITLE={0} gen_next={1}' -f $Trigger, ($beforeGen + 1))
    if (-not (Test-Path -LiteralPath $F.launcher_exe)) { throw ('EXECUTABLE_MISSING path={0}' -f $F.launcher_exe) }
    $gen = $beforeGen + 1
    $null = Write-AvatarState @{
        schema            = $script:StateSchema
        state             = 'STARTING'
        generation        = $gen
        avatar_id         = [string]$target.id
        avatar_id_source  = [string]$target.source
        owner_pid         = $PID
        owner_mode        = 'DIRECT'
        supervisor_pid    = $PID
        launcher_pid      = 0
        listener_pid      = 0
        listener_pids     = @()
        started_at        = (Get-Date).ToString('o')
        exit_code         = $null
        exit_reason       = ''
        last_error        = ''
        death_at          = ''
        death_evidence    = ''
        port              = [int]$F.port
        task_name         = $script:TaskName
        env               = $plan.env
        log_paths         = (Get-AvatarLogPaths)
        desired_avatar_id = [string]$target.id
    } $F
    $launcherPid = 0
    try {
        $null = Save-AvatarPreviousChildLogs $F $gen
        $launcherPid = [int](Start-GirlfriendAvatarNative -AvatarId ([string]$target.id))
    } catch {
        $msg = $_.Exception.Message
        $null = Write-AvatarState @{ state = 'FAILED'; last_error = $msg; exit_reason = 'START_FAILED'; launcher_pid = 0; listener_pid = 0; listener_pids = @() } $F
        Write-RuntimeLog $script:SupLog ('LAUNCH_FAILED gen={0} mode=Direct error={1}' -f $gen, $msg)
        throw
    }
    $procObj = $null
    try {
        $procObj = Get-Process -Id $launcherPid -ErrorAction Stop
        # Touch the handle while alive: ExitCode on a re-attached process is empty on
        # this box, which is exactly why exit_code has always read back blank.
        $null = $procObj.Handle
    } catch { $procObj = $null }
    $found = Get-AvatarSocketOwners $F
    $listenerPids = @($found.owned | ForEach-Object pid)
    if (-not $listenerPids.Count) { $listenerPids = @(Get-AvatarDescendantWorkers $launcherPid $F | ForEach-Object pid) }
    $listenerPid = 0
    if ($listenerPids.Count) { $listenerPid = [int]$listenerPids[0] }
    # 'RUNNING' used to be written here, on the strength of Start-Process having returned
    # -- while the interpreter was still loading a several-second checkpoint and had not
    # bound 8010 at all. The document now says what is true: a process exists, no service
    # yet. RUNNING lands only after the readiness wait below has actually seen it.
    $null = Write-AvatarState @{ state = 'STARTING'; launcher_pid = $launcherPid; listener_pid = $listenerPid; listener_pids = @($listenerPids); exit_reason = '' } $F
    Write-RuntimeLog $script:SupLog ('LAUNCH_OK gen={0} launcher_pid={1} listener_pid={2} mode=Direct' -f $gen, $launcherPid, $listenerPid)
    $ready = $false
    $live = $null
    $began = Get-Date
    $readyDeadline = $began.AddSeconds($ReadyTimeoutSec)
    $handedReady = Exit-AvatarLockForHandoff
    try {
        while ((Get-Date) -lt $readyDeadline) {
            $live = Get-AvatarStatus $F
            if ([string]$live.state -eq 'RUNNING') { $ready = $true; break }
            if (-not $live.launcher_alive -and -not $live.listener_alive -and -not $live.owner_alive) { break }
            Start-Sleep -Seconds 2
        }
    } finally {
        if ($handedReady) { $null = Enter-AvatarLockAfterHandoff 60 }
    }
    if ($ready) {
        $null = Write-AvatarState @{
            state             = 'RUNNING'
            launcher_pid      = [int]$live.launcher_pid
            listener_pid      = [int]$live.listener_pid
            listener_pids     = @($live.listener_pids)
            started_at        = (Get-Date).ToString('o')
            exit_reason       = ''
            last_error        = ''
            desired_avatar_id = ''
        } $F
    }
    Write-RuntimeLog $script:SupLog ('LAUNCH_READY gen={0} ready={1} waited_s={2} launcher_pid={3} listener_pid={4} state={5}' -f `
        $gen, [string]$ready, [int]((Get-Date) - $began).TotalSeconds, `
        $(if ($live) { [int]$live.launcher_pid } else { $launcherPid }), `
        $(if ($live) { [int]$live.listener_pid } else { $listenerPid }), `
        $(if ($live) { [string]$live.state } else { 'UNKNOWN' }))
    return [pscustomobject]@{ launcher_pid = $launcherPid; listener_pid = $listenerPid; generation = $gen; owner_pid = $PID; mode = 'Direct'; process = $procObj; ready = $ready }
}

function Stop-AvatarTree([hashtable]$F, [string]$Reason) {
    # THE teardown, in this order: our owner wrapper (so it cannot later record a crash
    # we asked for), then the tracked launcher tree through the production helper, then
    # a sweep for the socket owner and proven orphans, then a MEASUREMENT of the port.
    # Every kill is behind an identity assert; nothing is ever killed by image name.
    if (-not $F) { $F = Get-AvatarFacts }
    if (-not $Reason) { $Reason = 'STOP_REQUESTED' }
    $status = Get-AvatarStatus $F
    $before = Read-AvatarStateFile
    $gen = 0
    if ($before) { $gen = [int]$before.generation }
    $null = Write-AvatarState @{ state = 'STOPPING'; exit_reason = $Reason; last_error = '' } $F
    $killed = @()
    $notes = @()

    if ($status.owner_pid -gt 0) {
        $taskInfo = Get-AvatarTaskInfo
        if ($taskInfo.exists -and $taskInfo.state -eq 'Running') {
            try { $null = Stop-ScheduledTask -TaskName $script:TaskName; $notes += 'Stop-ScheduledTask' }
            catch { $notes += ('Stop-ScheduledTask failed: ' + $_.Exception.Message) }
        }
        $ownerProc = Get-AvatarProcess ([int]$status.owner_pid)
        if (Test-AvatarOwnedOwner $ownerProc) {
            Write-RuntimeLog $script:SupLog ('KILL_DECISION pid={0} role=owner reason={1} {2}' -f `
                [string]$status.owner_pid, $Reason, (Get-AvatarActorTag))
            try {
                Stop-Process -Id ([int]$status.owner_pid) -Force -ErrorAction Stop
                $killed += [pscustomobject]@{ pid = [int]$status.owner_pid; role = 'owner' }
            } catch { $notes += ('owner kill failed: ' + $_.Exception.Message) }
        } else { $notes += ('owner pid left alone, identity assert failed: ' + [string]$status.owner_pid) }
    }

    # service_pids.json['avatar'] is the launcher; the helper kills its whole tree
    # deepest-first and zeroes the bookkeeping. Its asserts (expected exe + the listen
    # marker) are the same ones used everywhere else here.
    $orphans = @(Get-AvatarOrphanWorkers $F)
    try {
        Stop-NativeProcess 'avatar' $F.launcher_exe $F.stop_marker $orphans
        $notes += 'Stop-NativeProcess ok'
    } catch { $notes += ('Stop-NativeProcess: ' + $_.Exception.Message) }

    # The fact-2/3 case the helper cannot see: the tracked launcher already gone, so it
    # no-ops and its re-exec'd child keeps holding 8010.
    $deadline = (Get-Date).AddSeconds(25)
    while ((Get-Date) -lt $deadline) {
        $found = Get-AvatarSocketOwners $F
        $targets = @()
        $targets += @($found.owned | ForEach-Object pid)
        $targets += @(Get-AvatarOrphanWorkers $F | ForEach-Object pid)
        if ($status.launcher_pid -gt 0) { $targets += @(Get-AvatarDescendantWorkers ([int]$status.launcher_pid) $F | ForEach-Object pid) }
        if ($status.listener_pid -gt 0) { $targets += @(Get-AvatarDescendantWorkers ([int]$status.listener_pid) $F | ForEach-Object pid) }
        $targets = @($targets | Where-Object { $_ -gt 0 } | Sort-Object -Unique)
        if (-not $targets.Count -and -not (@($found.rows).Count)) { break }
        foreach ($pidValue in $targets) {
            $proc = Get-AvatarProcess ([int]$pidValue)
            if (-not (Test-AvatarOwnedWorker $proc $F)) {
                $notes += ('refused kill, identity assert failed: ' + [string]$pidValue)
                Write-RuntimeLog $script:SupLog ('KILL_REFUSED pid={0} reason=identity_assert_failed {1}' -f `
                    [string]$pidValue, (Get-AvatarActorTag))
                continue
            }
            Write-RuntimeLog $script:SupLog ('KILL_DECISION pid={0} role=worker reason={1} {2}' -f `
                [string]$pidValue, $Reason, (Get-AvatarActorTag))
            try {
                Stop-Process -Id ([int]$pidValue) -Force -ErrorAction Stop
                $killed += [pscustomobject]@{ pid = [int]$pidValue; role = 'worker' }
            } catch { $notes += ('worker kill failed pid=' + [string]$pidValue + ': ' + $_.Exception.Message) }
        }
        Start-Sleep -Milliseconds 500
    }

    $portState = Wait-AvatarPortReleased $F 30
    # Zero the tracked bookkeeping if the process it named is a corpse -- otherwise the
    # next reader of service_pids.json repeats today's bug.
    try {
        $pids = Read-ServicePids
        $tracked = [int]$pids.avatar
        if ($tracked -gt 0 -and -not (Get-Process -Id $tracked -ErrorAction SilentlyContinue)) {
            Set-ObjectValue $pids 'avatar' 0
            Set-ServiceOwner $pids 'avatar' $null
            Save-ServicePids $pids
            $notes += ('cleared stale service_pids avatar=' + [string]$tracked)
        }
    } catch { $notes += ('service_pids cleanup failed: ' + $_.Exception.Message) }

    if (-not $portState.released) {
        $msg = ('PORT_NOT_RELEASED listeners={0} tcp={1} bindable={2} owned={3} foreign={4}' -f `
            $portState.listeners, $portState.tcp, $portState.bindable, ($portState.owned_pids -join ','), ($portState.foreign_pids -join ','))
        $null = Write-AvatarState @{ state = 'FAILED'; last_error = $msg; exit_reason = 'STOP_INCOMPLETE' } $F
        throw $msg
    }
    $null = Write-AvatarState @{
        state             = 'STOPPED'
        owner_pid         = 0
        owner_mode        = ''
        launcher_pid      = 0
        listener_pid      = 0
        listener_pids     = @()
        exit_reason       = $Reason
        last_error        = ''
        generation        = $gen
        desired_avatar_id = ''
        request_pid       = 0
    } $F
    Write-RuntimeLog $script:SupLog ('STOP_DONE reason={0} killed={1} port_waited_s={2} notes={3}' -f `
        $Reason, (@($killed | ForEach-Object pid) -join ','), $portState.waited_s, ($notes -join ' | '))
    return [pscustomobject]@{
        killed        = @($killed | ForEach-Object pid)
        owner_stopped = [bool]($status.owner_pid -gt 0)
        launcher_pid  = $status.launcher_pid
        listener_pid  = $status.listener_pid
        port          = $portState
        notes         = @($notes)
        generation    = $gen
    }
}

function Adopt-AvatarOwnership([hashtable]$F, $Status, [string]$Reason) {
    # A healthy listener nobody recorded (missing or stale state file) is still OUR
    # service once it passes the identity asserts: write what is true instead of
    # reporting UNOWNED -- or worse, restarting it. Touches no process.
    $plan = Get-AvatarLaunchPlan $F ([string]$Status.avatar_id)
    $null = Write-AvatarState @{
        schema           = $script:StateSchema
        state            = 'RUNNING'
        avatar_id        = [string]$Status.avatar_id
        avatar_id_source = 'observed'
        owner_pid        = [int]$Status.owner_pid
        owner_mode       = $(if ($Status.owner_pid -gt 0) { 'TASK' } else { 'ORPHANED' })
        supervisor_pid   = $PID
        launcher_pid     = [int]$Status.launcher_pid
        listener_pid     = [int]$Status.listener_pid
        listener_pids    = @($Status.listener_pids)
        exit_reason      = $Reason
        last_error       = ''
        port             = [int]$F.port
        env              = $plan.env
        log_paths        = (Get-AvatarLogPaths)
    } $F
    Write-RuntimeLog $script:SupLog ('OWNERSHIP_ADOPTED reason={0} launcher_pid={1} listener_pid={2}' -f `
        $Reason, $Status.launcher_pid, $Status.listener_pid)
}

# ---------------------------------------------------------------------------
# actions
# ---------------------------------------------------------------------------

function Invoke-AvatarStart([hashtable]$F, [string]$AvatarId, [int]$ReadyTimeoutSec, [string]$OwnerMode, [string]$Trigger) {
    # Idempotent by construction, and it never kills a live service: a healthy listener
    # on the requested person is a no-op, a healthy listener on a different person is
    # reported as a Switch and refused.
    $status = Get-AvatarStatus $F
    $target = Resolve-AvatarTarget $AvatarId $F
    if ($status.state -eq 'RUNNING') {
        if ([string]$status.avatar_id -eq [string]$target.id) {
            if (-not $status.state_file_present -or $status.declared_state -ne 'RUNNING') { Adopt-AvatarOwnership $F $status 'ALREADY_RUNNING' }
            Write-RuntimeLog $script:SupLog ('START_NOOP_ALREADY_RUNNING id={0} launcher_pid={1} listener_pid={2}' -f `
                $target.id, $status.launcher_pid, $status.listener_pid)
            return [pscustomobject]@{ verdict = 'ALREADY_RUNNING'; id = [string]$target.id; status = (Get-AvatarStatus $F) }
        }
        Write-RuntimeLog $script:SupLog ('START_REFUSES_SWITCH wanted={0} running={1}' -f $target.id, $status.avatar_id)
        return [pscustomobject]@{ verdict = 'SWITCH_REQUIRED'; id = [string]$target.id; running_id = [string]$status.avatar_id; status = $status }
    }
    if (@($status.foreign_listener_pids).Count) {
        throw ('PORT_HELD_BY_FOREIGN_PROCESS port={0} pids={1}; nothing was killed' -f `
            [int]$F.port, (@($status.foreign_listener_pids) -join ','))
    }
    if ($status.state -eq 'STARTING' -and [string]$status.avatar_id -eq [string]$target.id) {
        return [pscustomobject]@{ verdict = 'ALREADY_STARTING'; id = [string]$target.id; status = $status }
    }
    # A listener that answers nothing but still owns the socket WOULD make the new
    # instance fail to bind, and it passed the identity asserts, so on paper it is ours
    # to reap. On paper is not enough: "not answering the 3s health GET yet" is exactly
    # what a listener mid-model-load looks like, and a concurrent Start (or the Hold
    # wrapper's own revival) that reaps on that reading kills the service it was asked to
    # bring up -- silently, with exit code 0, because Stop-Process -Force is
    # TerminateProcess. So the decision is made on OWNERSHIP, never on a health snapshot:
    #   * orphans (parent gone, nothing names them) are always ours;
    #   * an unwell listener is ours only when no live owner claims it, or its own record
    #     already says it gave up (FAILED / STOPPED / STOPPING / no file);
    #   * anything else is somebody else's in-flight start: say so and step back.
    $reapPids = @($status.orphan_pids)
    $reapReason = 'orphan'
    if (@($status.listener_pids).Count -and -not $status.health_ok) {
        $declaredNow = [string]$status.declared_state
        $abandoned = ($declaredNow -in @('FAILED', 'STOPPED', 'STOPPING', 'UNOWNED', ''))
        # OWNERSHIP REPAIR 2026-09-25 (generation safety): a listener the state file
        # still records as THIS generation's own (pid + birth match) is not a relic --
        # it is the "owner died mid-load" or "launcher exited, listener alive" shape,
        # and killing it from a concurrent Start would murder a service that may be
        # seconds from healthy. Only a listener the file does NOT claim (pid reuse,
        # foreign residue, no file at all) is fair game, and only when no owner claims
        # it either. A file that already gave up (abandoned) stays reapable.
        $recordedListener = $false
        if (-not $abandoned) {
            $fileNow = Read-AvatarStateFile
            foreach ($lp in @($status.listener_pids)) {
                $lpProc = Get-AvatarProcess ([int]$lp)
                if (Test-AvatarRecordedGenerationProcess $lpProc $fileNow) { $recordedListener = $true; break }
            }
        }
        if ($abandoned -or (-not $recordedListener -and $status.owner_pid -le 0)) {
            $reapPids += @($status.listener_pids)
            $reapReason = 'unwell_listener_unowned'
        } else {
            Write-RuntimeLog $script:SupLog ('PRE_START_REFUSES_LIVE_OWNED_LISTENER listeners={0} owner_pid={1} declared={2} recorded_generation={3} -- {4}' -f `
                (@($status.listener_pids) -join ','), [int]$status.owner_pid, $declaredNow, [string]$recordedListener, `
                'a live owner that has not given up gets to finish its own load; nothing was killed')
            return [pscustomobject]@{ verdict = 'ALREADY_STARTING'; id = [string]$target.id; status = $status }
        }
    }
    $reapPids = @($reapPids | Where-Object { $_ -gt 0 } | Sort-Object -Unique)
    if (@($reapPids).Count) {
        Write-RuntimeLog $script:SupLog ('PRE_START_OWNED_REAP orphans={0} listeners={1} reason={2} targets={3} {4}' -f `
            (@($status.orphan_pids) -join ','), (@($status.listener_pids) -join ','), $reapReason, `
            ($reapPids -join ','), (Get-AvatarActorTag))
        foreach ($pidValue in $reapPids) {
            $proc = Get-AvatarProcess ([int]$pidValue)
            if (-not (Test-AvatarOwnedWorker $proc $F)) {
                Write-RuntimeLog $script:SupLog ('KILL_REFUSED pid={0} reason=identity_assert_failed {1}' -f `
                    [string]$pidValue, (Get-AvatarActorTag))
                continue
            }
            Write-RuntimeLog $script:SupLog ('KILL_DECISION pid={0} role=pre_start_reap {1}' -f `
                [string]$pidValue, (Get-AvatarActorTag))
            try { Stop-Process -Id ([int]$pidValue) -Force -ErrorAction Stop }
            catch { Write-RuntimeLog $script:SupLog ('PRE_START_REAP_UNKILLED pid={0} {1}' -f [string]$pidValue, $_.Exception.Message) }
        }
        $null = Wait-AvatarPortReleased $F 20
    }
    $started = Invoke-AvatarLaunch $F $target.id $ReadyTimeoutSec $OwnerMode $Trigger
    $after = Get-AvatarStatus $F
    $verdict = 'STARTED'
    if ($after.state -ne 'RUNNING') { $verdict = 'STARTED_UNHEALTHY' }
    return [pscustomobject]@{ verdict = $verdict; id = [string]$target.id; launch = $started; status = $after }
}

function Invoke-AvatarStop([hashtable]$F, [string]$Reason) {
    $status = Get-AvatarStatus $F
    if (-not $status.tcp_open -and $status.launcher_pid -eq 0 -and $status.owner_pid -eq 0 -and $status.listener_pid -eq 0) {
        $null = Write-AvatarState @{
            state = 'STOPPED'; owner_pid = 0; owner_mode = ''; launcher_pid = 0
            listener_pid = 0; listener_pids = @(); exit_reason = 'NOTHING_RUNNING'; desired_avatar_id = ''
        } $F
        Write-RuntimeLog $script:SupLog 'STOP_NOOP_NOTHING_RUNNING'
        return [pscustomobject]@{ verdict = 'ALREADY_STOPPED'; status = (Get-AvatarStatus $F) }
    }
    $stopped = Stop-AvatarTree $F $Reason
    return [pscustomobject]@{ verdict = 'STOPPED'; stop = $stopped; status = (Get-AvatarStatus $F) }
}

function Invoke-AvatarSwitchBody([hashtable]$F, [string]$TargetId, [int]$ReadyTimeoutSec, [string]$OwnerMode, [string]$Trigger) {
    # A->B: stop A, PROVE 8010 is released, only then start B. Never both at once. The
    # active_avatar pointer stays the switch script's business (one writer).
    $entry = Get-AvatarEntryById $TargetId
    if (-not $entry) { return [pscustomobject]@{ verdict = 'TARGET_NOT_REGISTERED'; id = $TargetId } }
    $entryState = [string]$entry.state
    if ($entryState -and $entryState -ne 'ready') { return [pscustomobject]@{ verdict = 'TARGET_NOT_READY'; id = $TargetId; entry_state = $entryState } }
    $status = Get-AvatarStatus $F
    $oldId = [string]$status.avatar_id
    if (-not $oldId) { $oldId = [string]$status.target_avatar_id }
    if ($status.state -eq 'RUNNING' -and [string]$status.avatar_id -eq [string]$TargetId) {
        return [pscustomobject]@{ verdict = 'ALREADY_RUNNING'; id = $TargetId; old = $oldId; status = $status }
    }
    if (@($status.foreign_listener_pids).Count) {
        throw ('PORT_HELD_BY_FOREIGN_PROCESS port={0} pids={1}; nothing was killed' -f `
            [int]$F.port, (@($status.foreign_listener_pids) -join ','))
    }
    if ($status.state -eq 'RUNNING' -or $status.state -eq 'STARTING' -or $status.state -eq 'STOPPING') {
        $reason = 'SWITCH_AWAY'
        if ($status.state -eq 'STARTING') { $reason = 'SWITCH_AWAY_HALF_STARTED' }
        $stopped = Stop-AvatarTree $F $reason
        if (-not $stopped.port.released) {
            return [pscustomobject]@{ verdict = 'PORT_NOT_RELEASED'; id = $TargetId; old = $oldId; stop = $stopped }
        }
    } else {
        $proof = Wait-AvatarPortReleased $F 10
        if (-not $proof.released) {
            return [pscustomobject]@{ verdict = 'PORT_NOT_RELEASED'; id = $TargetId; old = $oldId; stop = $proof }
        }
    }
    $started = Invoke-AvatarLaunch $F $TargetId $ReadyTimeoutSec $OwnerMode ('SWITCH:' + $Trigger)
    $after = Get-AvatarStatus $F
    $verdict = 'SWITCHED'
    if ($after.state -ne 'RUNNING') { $verdict = 'SWITCHED_UNHEALTHY' }
    elseif ([string]$after.avatar_id -ne [string]$TargetId) { $verdict = 'SWITCHED_WRONG_ID' }
    Write-RuntimeLog $script:SupLog ('SWITCH_DONE verdict={0} old={1} new={2} launcher_pid={3} listener_pid={4}' -f `
        $verdict, $oldId, $TargetId, $after.launcher_pid, $after.listener_pid)
    return [pscustomobject]@{ verdict = $verdict; id = $TargetId; old = $oldId; launch = $started; status = $after }
}

function Invoke-AvatarSwitch([hashtable]$F, [string]$TargetId, [int]$ReadyTimeoutSec, [string]$OwnerMode) {
    if (-not $TargetId) { throw 'SWITCH_TARGET_EMPTY' }
    return (Invoke-AvatarSwitchBody $F $TargetId $ReadyTimeoutSec $OwnerMode 'SWITCH')
}

function Invoke-AvatarEnsure([hashtable]$F, [string]$AvatarId, [int]$ReadyTimeoutSec, [string]$OwnerMode) {
    # "Make it running with the currently active avatar, idempotently." What
    # Start-AI-Girlfriend.ps1, the task wrapper and the UI all ask for. A different
    # person on the port converges through the SAME Switch primitive.
    $target = Resolve-AvatarTarget $AvatarId $F
    $status = Get-AvatarStatus $F
    if ($status.state -eq 'RUNNING') {
        if ([string]$status.avatar_id -eq [string]$target.id) {
            if (-not $status.state_file_present -or $status.declared_state -ne 'RUNNING') { Adopt-AvatarOwnership $F $status 'ENSURE_ALREADY_RUNNING' }
            return [pscustomobject]@{ verdict = 'ALREADY_RUNNING'; id = [string]$target.id; status = (Get-AvatarStatus $F) }
        }
        $sw = Invoke-AvatarSwitchBody $F ([string]$target.id) $ReadyTimeoutSec $OwnerMode 'ENSURE'
        return [pscustomobject]@{ verdict = $sw.verdict; id = [string]$target.id; old = $sw.old; switch = $sw; status = $sw.status }
    }
    $st = Invoke-AvatarStart $F ([string]$target.id) $ReadyTimeoutSec $OwnerMode 'ENSURE'
    return [pscustomobject]@{ verdict = $st.verdict; id = [string]$target.id; launch = $st.launch; status = $st.status }
}

function Save-AvatarPreviousChildLogs([hashtable]$F, [int]$NextGen) {
    # Start-Process -RedirectStandardOutput/Error TRUNCATES on every launch, so the next
    # start destroys the dying generation's own words. Both unexplained deaths (06:48:02
    # and 07:22:44) were read back as "the last stderr line is this process's startup
    # banner" -- which is what a truncated file holding nothing looks like -- and any real
    # shutdown text was gone before anyone could look. Copy the files aside before launch.
    #
    # The archive is named by the PID the file itself reports (app.py logs 'pid=NNNN'),
    # not by a generation counter: a counter would mislabel the copy whenever a generation
    # died without starting a process, and the whole point of this file is to be readable
    # next to a dossier that names pids.
    $logs = Get-AvatarLogPaths
    foreach ($k in @('stdout', 'stderr')) {
        # $logs is the same object the dossier reads with dotted access; indexing it with
        # a string variable yields $null on PS 5.1, and a null -LiteralPath is what made
        # the first gen-9 launch fail inside Save-AvatarPreviousChildLogs.
        $src = [string]$logs.$k
        if (-not $src -or -not (Test-Path -LiteralPath $src)) { continue }
        $item = $null
        try { $item = Get-Item -LiteralPath $src } catch { continue }
        if (-not $item -or [long]$item.Length -le 0) { continue }
        $dir = Split-Path -Parent $src
        $base = [System.IO.Path]::GetFileNameWithoutExtension($src)
        $pidTag = 'gen' + [Math]::Max(0, $NextGen - 1)
        try {
            $found = Select-String -LiteralPath $src -Pattern 'pid=(\d+)' -AllMatches -ErrorAction Stop |
                     Select-Object -First 1
            if ($found -and $found.Matches[0].Groups[1].Value) { $pidTag = 'pid' + $found.Matches[0].Groups[1].Value }
        } catch {}
        $dst = Join-Path $dir ($base + '.' + $pidTag + '.log')
        if (Test-Path -LiteralPath $dst) {
            $same = $false
            try { $same = [long](Get-Item -LiteralPath $dst).Length -eq [long]$item.Length } catch {}
            if ($same) { continue }        # already preserved, nothing new to learn
        }
        try {
            Copy-Item -LiteralPath $src -Destination $dst -Force -ErrorAction Stop
            Write-RuntimeLog $script:SupLog ('CHILD_LOG_ARCHIVED tag={0} file={1} bytes={2} dst={3}' -f `
                $pidTag, $src, [long]$item.Length, (Split-Path -Leaf $dst))
        } catch {
            Write-RuntimeLog $script:SupLog ('CHILD_LOG_ARCHIVE_FAILED tag={0} error={1}' -f `
                $pidTag, $_.Exception.Message)
        }
    }
    # A generation per file, so retention is by count and never by clock.
    try {
        $dir = Split-Path -Parent ([string]$logs.stderr)
        foreach ($base in @('avatar-8010.stdout', 'avatar-8010.stderr')) {
            $old = @(Get-ChildItem -LiteralPath $dir -Filter ($base + '.p*.log') -ErrorAction SilentlyContinue |
                     Sort-Object LastWriteTime -Descending | Select-Object -Skip 12)
            foreach ($f in $old) { try { Remove-Item -LiteralPath $f.FullName -Force -ErrorAction Stop } catch {} }
        }
    } catch {}
}

function Get-AvatarLogTail([string]$Path, [int]$Lines) {
    # A witness must never be the thing that breaks the recording, so every read here is
    # best-effort and returns nothing rather than throwing.
    if (-not $Path -or -not (Test-Path -LiteralPath $Path)) { return @() }
    try {
        return @(Get-Content -LiteralPath $Path -Tail $Lines -ErrorAction Stop |
            Where-Object { [string]$_ -ne '' })
    } catch { return @() }
}

function Get-AvatarLogAgeSec([string]$Path) {
    # -1 means the file is not there at all, which is itself evidence (a service that
    # never wrote its heartbeat is a different failure from one that stopped writing it).
    if (-not $Path -or -not (Test-Path -LiteralPath $Path)) { return -1 }
    try { return [int]((Get-Date) - (Get-Item -LiteralPath $Path).LastWriteTime).TotalSeconds } catch { return -1 }
}

function Get-AvatarGatePhase([string]$Path, [int]$Lines) {
    # Was the terminate gate (runtime\avatar\app\server\terminate_gate.py) holding a shield
    # when the service stopped logging?  The gate writes one aggregate 'arm' line and one
    # aggregate 'disarm' line per teardown window, plus one 'target' line per process
    # object; only the aggregate pair is counted here.
    #
    #     ARMED_AT_DEATH        -- the newest gate line is an arm with no matching disarm:
    #        the process vanished while a FRESH OpenProcess(PROCESS_TERMINATE) on it and on
    #        its launcher was being refused.  Access checks happen at open time, so that
    #        positively EXCLUDES 'someone opened a new handle and terminated us', and leaves
    #        a native self-exit, a handle opened before the window, or the venv launcher's
    #        kill-on-close job -- three shapes that need different fixes.
    #     RELEASED_BEFORE_DEATH -- the shield was given back cleanly, so this death happened
    #        outside a teardown window and the gate has no reading on it.
    #     GATE_NEVER_LOGGED     -- gate off (AVATAR_TERMINATE_GATE=0), or a trail file that
    #        predates the instrument.  Said out loud rather than left as an empty field.
    #
    # The newest line decides, not a difference of counts: counting arms and disarms over a
    # fixed tail invents a phantom unmatched arm whenever a window straddles the edge.
    $out = [ordered]@{ phase = 'GATE_NEVER_LOGGED'; count = 0; last = ''; lines_read = 0 }
    $tail = @(Get-AvatarLogTail $Path $Lines)
    $out['lines_read'] = @($tail).Count
    $gate = @($tail | Where-Object {
        ([string]$_ -match 'TERMINATE_GATE (arm|disarm) ') -and ([string]$_ -notmatch ' target ') })
    $out['count'] = @($gate).Count
    if (@($gate).Count -eq 0) { return [pscustomobject]$out }
    $last = [string]$gate[@($gate).Count - 1]
    if ($last.Length -gt 200) { $last = $last.Substring(0, 200) }
    $out['last'] = $last
    if ($last -match 'TERMINATE_GATE arm ') { $out['phase'] = 'ARMED_AT_DEATH' }
    else { $out['phase'] = 'RELEASED_BEFORE_DEATH' }
    return [pscustomobject]$out
}

# ---------------------------------------------------------------------------
# killer tripwire -- name the process that ends our child, while it is running
# ---------------------------------------------------------------------------
# The dossier can prove WHICH of the three exits happened but never WHO did it, and
# an unelevated box has no process-termination audit to read. What it does have is a
# second-granularity view of the process list: a killer that is a separate process
# (taskkill, wmic, another powershell running Stop-Process, an agent's cleanup) has to
# exist in the table for at least a moment, and its PARENT names the thing that ran
# it. Sightings are logged the instant they happen, so a death minutes later still
# has a shortlist attached to it.
#
# Deliberately wide on command line, narrow on identity: our own pid, the pid we are
# watching and the wrapper that launched it are excluded, because the supervisor's own
# Stop path matches every one of these patterns and would otherwise be the loudest
# false positive in the log.
$script:KillerSightings = New-Object System.Collections.ArrayList
$script:KillerSeenPids = @{}

function Get-AvatarKillerSightings([int]$SelfPid, [int]$LauncherPid, [int]$ListenerPid) {
    $skip = @($SelfPid, $LauncherPid, $ListenerPid) | Where-Object { $_ -gt 0 }
    $wql = "SELECT ProcessId,ParentProcessId,Name,CommandLine FROM Win32_Process WHERE " +
           "Name='taskkill.exe' OR Name='wmic.exe' OR Name='tskill.exe' OR Name='psexec.exe' " +
           "OR Name='kill.exe' OR Name='handle.exe' OR " +
           "CommandLine LIKE '%Stop-Process%' OR CommandLine LIKE '%TerminateProcess%' " +
           "OR CommandLine LIKE '%PROCESS_TERMINATE%' OR CommandLine LIKE '%Avatar-Supervisor%' " +
           "OR CommandLine LIKE '%Run-Girlfriend-Avatar8010%'"
    $rows = @()
    try {
        $rows = @(Get-CimInstance -ClassName Win32_Process -Filter $wql -ErrorAction Stop)
    } catch {
        return @()
    }
    foreach ($r in $rows) {
        $p = [int]$r.ProcessId
        if ($skip -contains $p) { continue }
        if ($script:KillerSeenPids.ContainsKey($p)) { continue }
        $script:KillerSeenPids[$p] = $true
        $cmd = [string]$r.CommandLine
        if ($cmd.Length -gt 200) { $cmd = $cmd.Substring(0, 200) }
        $parentName = ''
        try {
            $pp = @(Get-CimInstance -ClassName Win32_Process -Filter ("ProcessId=" + [int]$r.ParentProcessId) -ErrorAction Stop)
            if ($pp.Count) { $parentName = [string]$pp[0].Name }
        } catch { $parentName = '?' }
        $entry = ('pid={0} ppid={1}/{2} name={3} cmd="{4}"' -f $p, [int]$r.ParentProcessId, `
                  $parentName, [string]$r.Name, $cmd)
        [void]$script:KillerSightings.Add(('{0}|{1}' -f (Get-Date -Format 'HH:mm:ss'), $entry))
        while ($script:KillerSightings.Count -gt 40) { $script:KillerSightings.RemoveAt(0) }
        Write-RuntimeLog $script:OwnerLog ('AVATAR_TRIPWIRE_SIGHTING at={0} {1}' -f `
            (Get-Date -Format 'yyyy-MM-dd HH:mm:ss.fff'), $entry)
    }
    return @($script:KillerSightings)
}

# Unelevated, this box cannot tell us who called TerminateProcess (no 4688/4689
# auditing, Win32_ProcessStopTrace access-denied -- both measured). What it CAN tell us
# is the shape of the second the process disappeared: a targeted kill takes exactly our
# two pids, while a machine-wide reaper takes a crowd. So the hold loop keeps a short
# rolling ring of whole-process snapshots (Get-Process only, no CIM: cheap enough for a
# 2s cadence) and the dossier diffs the last snapshot against the world as it now is.
$script:ProcRing = New-Object System.Collections.ArrayList
$script:ProcRingKeepSec = 120

function Update-AvatarProcessRing {
    $snap = @{}
    try {
        foreach ($p in @(Get-Process -ErrorAction SilentlyContinue)) {
            $snap[[int]$p.Id] = [string]$p.ProcessName
        }
    } catch { return $null }
    $now = Get-Date
    [void]$script:ProcRing.Add(@{ at = $now; all = $snap })
    while ($script:ProcRing.Count -gt 2 -and `
           ($now - [datetime]$script:ProcRing[0].at).TotalSeconds -gt $script:ProcRingKeepSec) {
        $script:ProcRing.RemoveAt(0)
    }
    return $snap
}

function Get-AvatarProcessDelta([int]$SelfPid, [int]$LauncherPid, [int]$ListenerPid) {
    # Diff the newest ring entry (taken while the child was, or had just been, alive)
    # against a fresh read. Returns a one-line summary safe to drop into the evidence.
    $base = $null
    if ($script:ProcRing.Count -gt 0) { $base = $script:ProcRing[$script:ProcRing.Count - 1] }
    if (-not $base) { return 'delta=no_baseline' }
    $nowSnap = Update-AvatarProcessRing
    if (-not $nowSnap) { return 'delta=read_failed' }
    $mine = @($SelfPid, $LauncherPid, $ListenerPid) | Where-Object { $_ -gt 0 }
    # Baseline = the newest snapshot in which the service was still alive. Without this
    # the last 2s poll can already contain the death, and the diff would show nothing.
    for ($ri = $script:ProcRing.Count - 1; $ri -ge 0; $ri--) {
        $cand = $script:ProcRing[$ri]
        if ($ListenerPid -gt 0 -and $cand.all.ContainsKey([int]$ListenerPid)) { $base = $cand; break }
        if ($LauncherPid -gt 0 -and $cand.all.ContainsKey([int]$LauncherPid)) { $base = $cand; break }
    }
    if (-not $base) { $base = $script:ProcRing[$script:ProcRing.Count - 1] }
    $vanished = New-Object System.Collections.ArrayList
    foreach ($k in $base.all.Keys) {
        if (-not $nowSnap.ContainsKey([int]$k)) {
            $nm = [string]$base.all[$k]
            if ($mine -contains [int]$k) { $nm = $nm + '(ours)' }
            [void]$vanished.Add(('{0}:{1}' -f $k, $nm))
        }
    }
    $appeared = New-Object System.Collections.ArrayList
    foreach ($k in $nowSnap.Keys) {
        if (-not $base.all.ContainsKey([int]$k)) { [void]$appeared.Add(('{0}:{1}' -f $k, [string]$nowSnap[$k])) }
    }
    $vList = @($vanished | Select-Object -First 12)
    $aList = @($appeared | Select-Object -First 12)
    return ('delta_since={0} vanished={1} vanished_list="{2}" appeared={3} appeared_list="{4}"' -f `
        [int]((Get-Date) - [datetime]$base.at).TotalSeconds, @($vanished).Count, ($vList -join ','), `
        @($appeared).Count, ($aList -join ','))
}

# ---------------------------------------------------------------------------
# exit-status forensics: one cached handle per pid, and a measured fingerprint table
# ---------------------------------------------------------------------------
$script:CachedProc = @{}

function Get-AvatarCachedProc([int]$ProcPid) {
    # A process's exit status can only be read through a handle that was taken WHILE it
    # was alive; after the last handle closes, the object and its status are gone. The
    # owner has always cached exactly one such handle -- and it was the LAUNCHER's,
    # because that is the pid `Start-GirlfriendAvatarNative` returns. Measured on this
    # box (process census 2026-09-25 09:57): the venv trampoline is a REAL parent, not a
    # re-exec (owner powershell -> venv python -> core python listener, three distinct
    # pids, listener ppid == launcher pid). So the number historically recorded as
    # 'exit_code' is the *trampoline's* exit status, and what the trampoline decides to
    # return when its child dies is the trampoline's business. Caching a handle for every
    # pid the moment we learn it is what turns 'exit_code=0' from a statement about the
    # launcher into a statement about the process that was serving :8010.
    if ($ProcPid -le 0) { return $null }
    if ($script:CachedProc.ContainsKey($ProcPid)) { return $script:CachedProc[$ProcPid] }
    $p = $null
    try {
        $p = Get-Process -Id $ProcPid -ErrorAction Stop
        $null = $p.Handle   # forces the handle open; see the note at the launch site
        $script:CachedProc[$ProcPid] = $p
    } catch { $p = $null }
    return $p
}

$script:ExitNoHandle = -999999   # 'we never held it' -- a DIFFERENT fact from 'it left 0'

function Get-AvatarExitCodeOf([int]$ProcPid, $ProcObj) {
    if ($ProcObj) { try { return [int]$ProcObj.ExitCode } catch { return $script:ExitNoHandle } }
    $p = Get-AvatarCachedProc $ProcPid
    if (-not $p) { return $script:ExitNoHandle }
    try { return [int]$p.ExitCode } catch { return $script:ExitNoHandle }
}

function Get-AvatarExitFingerprint([int]$Code) {
    # Every row below is MEASURED on this machine against a real python child process, not
    # read out of documentation: runtime/debug/kill_fingerprint/20260925_095118 and
    # _095311 (kill_fingerprint_matrix.py). The table is keyed by the unsigned hex string
    # because .NET hands ExitCode back as Int32 -- 0xFFFFFFFF arrives as -1 -- and keying
    # by signed decimal is how a hand-converted NTSTATUS silently points at the wrong
    # exception (0xC000013A is -1073741510, not -1073741822; both were tried here).
    #   0x00000000  TerminateProcess(h, 0)          -- explicit zero, or the victim's own
    #                                                 os._exit(0) (see SELF_EXIT below)
    #   0xFFFFFFFF  Stop-Process -Force, [Process]::Kill()   -- and neither takes the
    #                                                 child tree with it (measured)
    #   0x00000001  taskkill /F, taskkill /T /F, TerminateProcess(h, 1)
    #   0xC000013A  CTRL_C_EVENT / STATUS_ALERTED
    #   0xC0000005  STATUS_ACCESS_VIOLATION
    #   0xC0000409  STATUS_STACK_BUFFER_OVERRUN (__fastfail: abort(), __security_cookie)
    #   0xC0000374  STATUS_HEAP_CORRUPTION
    #   0xC00000FD  STATUS_STACK_OVERFLOW
    $hex = '0x{0:X8}' -f $Code
    if ($Code -eq $script:ExitNoHandle) { return 'NO_HANDLE' }
    $table = @{
        '0x00000000' = 'TERMINATE_ZERO'
        '0xFFFFFFFF' = 'KILL_FORCE_STYLE'
        '0x00000001' = 'TASKKILL_STYLE'
        '0xC000013A' = 'CTRL_C_OR_ALERTED'
        '0xC0000005' = 'ACCESS_VIOLATION'
        '0xC0000409' = 'FASTFAIL_STACK_BUFFER_OVERRUN'
        '0xC0000374' = 'HEAP_CORRUPTION'
        '0xC00000FD' = 'STACK_OVERFLOW'
        '0x00000003' = 'ABORT'
        '0xC00002C4' = 'DEBUGGER_DETACHED'
        '0x40010004' = 'DBG_TERMINATE_PROCESS'
    }
    if ($table.ContainsKey($hex)) { return ($hex + ' ' + $table[$hex]) }
    return ($hex + ' UNMAPPED')
}

function Get-AvatarDeathDossier([hashtable]$F, [int]$LauncherPid, [int]$ListenerPid, $ProcObj, $Since) {
    # The box cannot answer 'who called TerminateProcess': Security 4688/4689 are not
    # audited here, and Win32_ProcessStopTrace is access-denied unelevated (both measured,
    # see runtime/tests/probes/forensic_window_0609.ps1 and probe_wmi_stop_trace.ps1). So
    # instead of guessing, take everything that IS observable in the same second the
    # process vanishes and classify which of the three exits it matches. The service
    # writes the other half of that evidence (app.py: console-control witness + heartbeat).
    $exitCode = $null
    if ($ProcObj) { try { $exitCode = [int]$ProcObj.ExitCode } catch { $exitCode = $null } }
    # Two processes, two statuses, and only the listener's is the death being diagnosed.
    # The launcher's is kept beside it on purpose: when they DISAGREE, the disagreement
    # says the trampoline left on its own after its child was already gone (or was taken
    # itself), which is a different incident from the interpreter dropping off.
    $launcherCode = Get-AvatarExitCodeOf $LauncherPid $ProcObj
    $listenerCode = Get-AvatarExitCodeOf $ListenerPid $null
    if ($listenerCode -ne $script:ExitNoHandle) { $exitCode = $listenerCode }
    elseif ($launcherCode -ne $script:ExitNoHandle) { $exitCode = $launcherCode }
    $logs = Get-AvatarLogPaths
    $stderrTail = Get-AvatarLogTail ([string]$logs.stderr) 6
    $beatTail = Get-AvatarLogTail ([string]$logs.heartbeat) 2
    $witTail = Get-AvatarLogTail ([string]$logs.exit_witness) 12
    # The witness file is append-only, so cut it at the last 'witness-installed' line --
    # every generation writes exactly one, at its own start. Everything after it belongs
    # to the generation that just died; anything before it must not be blamed on this
    # death (and a faulthandler dump carries no pid, so a pid filter would drop it).
    $witMine = @($witTail)
    for ($wi = @($witTail).Count - 1; $wi -ge 0; $wi--) {
        if ([string]$witTail[$wi] -match 'witness-installed') {
            $witMine = @($witTail[($wi + 1)..($witTail.Count - 1)])
            if ($wi -ge $witTail.Count - 1) { $witMine = @() }
            break
        }
    }
    $stderrAge = Get-AvatarLogAgeSec ([string]$logs.stderr)
    $beatAge = Get-AvatarLogAgeSec ([string]$logs.heartbeat)
    $blob = (($stderrTail + $beatTail + $witMine) -join ' ')
    $witAll = ($witMine -join ' ')

    # The app's own log is where the last words are. ``HumanPlayer.force_stop`` now marks
    # every statement it steps through ('HPLAYER_FS enter/quit_set/join_done/...') and the
    # session reaper marks every release step, both through utils.logger, which writes
    # livetalking.log relative to the service CWD (runtime\avatar\app -- utils/logger.py).
    # The line that is NOT followed by another one is the statement the process died on,
    # which is a much better answer than 'it stopped logging'. Read as evidence only, never
    # folded into $blob: this file is append-only across generations and a 'Traceback'
    # from three generations ago must not be blamed on the death being classified.
    $appLogPath = Join-Path $script:ProjectRoot 'runtime\avatar\app\livetalking.log'
    $appTail = Get-AvatarLogTail $appLogPath 80
    $fsLines = @($appTail | Where-Object { [string]$_ -match 'HPLAYER_FS|HPLAYER_THREADS|session release step|force_stop' })
    $fsLast = ''
    if ($fsLines.Count) { $fsLast = [string]$fsLines[$fsLines.Count - 1] }
    if ($fsLast.Length -gt 190) { $fsLast = $fsLast.Substring(0, 190) }
    $fsStage = ''
    if ($fsLast -match 'HPLAYER_FS (\S+)') { $fsStage = $Matches[1] }
    $appAge = Get-AvatarLogAgeSec $appLogPath

    # ── was the terminate gate holding a shield when this died? ─────────────────────
    # See Get-AvatarGatePhase below for what the three phases mean; it is a function so
    # that the reading can be tested against a synthetic log without killing anything.
    $gate = Get-AvatarGatePhase $appLogPath 400
    $gatePhase = [string]$gate.phase
    $gateLast = [string]$gate.last
    $gateCount = [int]$gate.count

    # Order matters: a console teardown also stops the heartbeat, so the most specific
    # signature has to be tested first. The exit-witness markers are the most specific of
    # all, because the dying process writes them itself (app.py _install_exit_witness):
    # faulthandler dumps on a native fault, 'received ... draining' is a stop the service
    # was asked for, and python-exit-hook runs only when the interpreter reaches its exit
    # hooks. 'C-level exit() without Python finalizing' was dropped from the ladder after
    # it was measured to crash the exit path instead of witnessing it
    # (logs/runtime/regression/crt_diff.txt) -- that one case stays UNKNOWN rather than
    # being bought with a shutdown fault. Nothing in the list at all is what a foreign
    # TerminateProcess looks like -- and that is now a positive finding, not an inference.
    #
    #   SELF_EXIT / SELF_KILL  (added first, above everything): the os._exit / os.kill
    #   wrappers in app.py. A hard self-exit is the ONE way a process can end itself and
    #   still look from outside exactly like a foreign kill -- no atexit list, no
    #   faulthandler dump, no CRT flush. Silence here is now a proof of its own: it
    #   positively EXCLUDES self-exit, which is the discrimination the whole table below
    #   was built to make and could not make before.
    $reason = 'CHILD_EXIT_TERMINATED_UNDIAGNOSED'
    if ($witAll -match 'SELF_EXIT|SELF_KILL') { $reason = 'CHILD_EXIT_HARD_SELF_EXIT' }
    elseif ($blob -match 'avatar witnessed CTRL_') { $reason = 'CHILD_EXIT_CONSOLE_TEARDOWN' }
    elseif ($witAll -match 'Fatal Python error|Current thread 0x') { $reason = 'CHILD_EXIT_NATIVE_FAULT' }
    elseif ($blob -match 'received SIG|reason=signal_|avatar runtime stopped') { $reason = 'CHILD_EXIT_SELF_STOP' }
    elseif ($witAll -match 'python-exit-hook') { $reason = 'CHILD_EXIT_PYTHON_FINALIZED' }
    elseif ($blob -match 'Traceback|Fatal Python|memory error|out of memory|Segmentation|access violation') { $reason = 'CHILD_EXIT_APP_FAULT' }
    if ($reason -eq 'CHILD_EXIT_TERMINATED_UNDIAGNOSED') {
        # Nothing the process could write about itself said anything, so what is left is
        # the exit status, and the status has a MEASURED vocabulary (see
        # Get-AvatarExitFingerprint). Appending it to the label is the whole point: a
        # reader of the document can then tell 'killed with TerminateProcess(h,0)' from
        # 'Stop-Process -Force' from 'taskkill /F' without knowing anything about the box.
        $codeForFp = $listenerCode
        if ($codeForFp -eq $script:ExitNoHandle) { $codeForFp = $launcherCode }
        if ($codeForFp -ne $script:ExitNoHandle) {
            $reason = ('CHILD_EXIT_TERMINATED_UNDIAGNOSED/' + (Get-AvatarExitFingerprint $codeForFp))
            if ($codeForFp -eq 0) {
                $reason += '_NO_SELF_EXIT_HOOK'
            }
        }
    }

    $portOpen = $false
    try {
        $portOpen = [bool](Get-NetTCPConnection -LocalPort ([int]$F.port) -State Listen -ErrorAction SilentlyContinue)
    } catch { $portOpen = $false }
    $freeMb = -1
    try {
        $os = Get-CimInstance Win32_OperatingSystem -ErrorAction Stop
        $freeMb = [int]([long]$os.FreePhysicalMemory / 1024)
    } catch {}
    $werNew = -1
    try {
        $since = (Get-Date).AddMinutes(-15)
        $dirs = @('C:\ProgramData\Microsoft\Windows\WER\ReportArchive',
                  'C:\ProgramData\Microsoft\Windows\WER\ReportQueue',
                  (Join-Path $env:LOCALAPPDATA 'CrashDumps'))
        $werNew = 0
        foreach ($d in $dirs) {
            if (-not (Test-Path -LiteralPath $d)) { continue }
            $werNew += @(Get-ChildItem -LiteralPath $d -Recurse -File -ErrorAction SilentlyContinue |
                Where-Object { $_.LastWriteTime -gt $since }).Count
        }
    } catch { $werNew = -1 }
    $taskEv = -1
    $taskEvDetail = ''
    try {
        # Count only what the scheduler did DURING this generation: a rolling 3-minute
        # window counted the Start of the very instance we are diagnosing and made every
        # dossier look like the scheduler had been busy.
        $from = (Get-Date).AddMinutes(-3)
        $ev = @(Get-WinEvent -FilterHashtable @{
                LogName     = 'Microsoft-Windows-TaskScheduler/Operational'
                StartTime   = $from
                ProviderName = 'Microsoft-Windows-TaskScheduler'
            } -ErrorAction Stop)
        $ours = @($ev | Where-Object { $_.Message -like ('*' + $script:TaskName + '*') })
        if ($Since) { $ours = @($ours | Where-Object { $_.TimeCreated -gt $Since }) }
        $taskEv = @($ours).Count
        foreach ($e in @($ours | Select-Object -Last 3)) {
            $taskEvDetail += ('[{0} id={1}] ' -f $e.TimeCreated.ToString('HH:mm:ss'), [int]$e.Id)
        }
    } catch { $taskEv = 0 }

    $lastStderr = if ($stderrTail.Count) { [string]$stderrTail[-1] } else { '' }
    if ($lastStderr.Length -gt 160) { $lastStderr = $lastStderr.Substring(0, 160) }
    $lastBeat = if ($beatTail.Count) { [string]$beatTail[-1] } else { '' }
    if ($lastBeat.Length -gt 120) { $lastBeat = $lastBeat.Substring(0, 120) }
    $witLast = if ($witMine.Count) { [string]$witMine[-1] } else { '' }
    if ($witLast.Length -gt 150) { $witLast = $witLast.Substring(0, 150) }
    $witAge = Get-AvatarLogAgeSec ([string]$logs.exit_witness)
    $sight = @($script:KillerSightings)
    $sightLast = ''
    if ($sight.Count) { $sightLast = [string]$sight[-1] }
    if ($sightLast.Length -gt 220) { $sightLast = $sightLast.Substring(0, 220) }
    $delta = Get-AvatarProcessDelta $PID $LauncherPid $ListenerPid
    $evidence = ('exit_code={0} exit_hex_listener={1} exit_hex_launcher={2} listener_handle_held={3} ' +
        'launcher_pid={4} listener_pid={5} stderr_age_s={6} beat_age_s={7} app_log_age_s={8} ' +
        'last_release_stage="{9}" last_release_line="{10}" ' +
        'port_still_open={11} free_mb={12} wer_files_15m={13} task_events_during_hold={14} ' +
        'witness_age_s={15} stderr_last="{16}" beat_last="{17}" witness_last="{18}" ' +
        'killer_sightings={19} killer_last="{20}" {21} task_events="{22}" ' +
        'gate_phase={23} gate_lines_in_tail={24} gate_last="{25}"') -f `
        $exitCode, (Get-AvatarExitFingerprint $listenerCode), (Get-AvatarExitFingerprint $launcherCode), `
        [string](($script:CachedProc.ContainsKey([int]$ListenerPid))), `
        $LauncherPid, $ListenerPid, $stderrAge, $beatAge, $appAge, $fsStage, $fsLast, `
        [string]$portOpen, $freeMb, $werNew, $taskEv, $witAge, $lastStderr, $lastBeat, `
        $witLast, $sight.Count, $sightLast, $delta, $taskEvDetail, `
        $gatePhase, $gateCount, $gateLast
    return [pscustomobject]@{ exit_code = $exitCode; exit_reason = $reason; evidence = $evidence }
}

# Revival policy for the durable owner. Two things have to hold at once: a deliberate
# Stop/Switch is terminal (SOT wins, no revival, ever), and an UNREQUESTED death must not
# turn "the person vanished" into "restart it" -- which is the answer this task exists to
# remove. The first build bounded revival to 3 per rolling 30 minutes and then left the
# service FAILED on purpose. In the field that is indistinguishable from having given up,
# and the cap itself became the outage. So the budget is now a BACKOFF, not a ceiling:
# 3s, 9s, 27s, 81s, 243s, then every HoldReviveMaxWaitSec, for as long as the owner lives,
# with the streak reset by any hold that lasted HoldStableSec.
#     * a child that never owned a process is still not revived: that is a broken
#       environment, and hammering it buries the reason under retries.
#     * every attempt logs its reason and its wait, so a killer on a fixed period shows up
#       as a rhythm in the log instead of as a mystery.
# Revival is a FLOOR, not a fix -- it is what keeps an unattributable external kill from
# being user-visible while the ownership bugs behind the death are settled elsewhere.
if (-not $script:HoldReviveLimit) { $script:HoldReviveLimit = 3 }
if (-not $script:HoldReviveWindowSec) { $script:HoldReviveWindowSec = 1800 }
if (-not $script:HoldReviveMaxWaitSec) { $script:HoldReviveMaxWaitSec = 300 }
if (-not $script:HoldStableSec) { $script:HoldStableSec = 120 }

function Wait-AvatarReviveBackoff([int]$Seconds) {
    # Sleep in slices and bail the moment the ask changes. An owner that is waiting to
    # re-hold must never come back and resurrect a service somebody shut down during that
    # wait, and it must not step in front of a Start that another pid has just queued.
    $until = (Get-Date).AddSeconds($Seconds)
    while ((Get-Date) -lt $until) {
        $file = Read-AvatarStateFile
        if ($file) {
            $st = [string]$file.state
            if ($st -eq 'STOPPING' -or $st -eq 'STOPPED') { return 'STOP_REQUESTED_DURING_BACKOFF' }
            if ([string]$file.desired_avatar_id) { return 'START_QUEUED_DURING_BACKOFF' }
        }
        Start-Sleep -Milliseconds 500
    }
    return ''
}

function Invoke-AvatarHold([hashtable]$F, [string]$AvatarId, [int]$ReadyTimeoutSec) {
    # The long-lived owner mode, and the only thing the task Action runs.
    $revivals = New-Object System.Collections.ArrayList
    $attempt = 0
    $nextId = $AvatarId
    while ($true) {
        $one = Invoke-AvatarHoldOnce $F $nextId $ReadyTimeoutSec
        $reason = [string]$one.exit_reason
        $declared = [string]$one.verdict
        $heldFor = $null
        if ($one.PSObject.Properties['held_s']) { $heldFor = [int]$one.held_s }

        $terminal = ($declared -eq 'STOPPED' -or $reason -like 'STOP_REQUESTED*' -or `
                     $reason -like 'SWITCH_AWAY*')
        if ($terminal) { return $one }
        if ($declared -ne 'FAILED') { return $one }
        if (-not [int]$one.ever_held) {
            # Never owned a process: this is a start that did not start. Re-holding it
            # would only bury the reason under retries.
            Write-RuntimeLog $script:OwnerLog ('AVATAR_OWNER_NO_REVIVE reason={0} -- nothing ever ran, not a death to revive' -f $reason)
            return $one
        }

        $now = Get-Date
        while ($revivals.Count -and ($now - [datetime]$revivals[0]).TotalSeconds -gt $script:HoldReviveWindowSec) {
            $revivals.RemoveAt(0)
        }
        if ($heldFor -and $heldFor -ge $script:HoldStableSec) {
            # It stood up and held by itself for a while: the next death is a new incident,
            # not the tail of this one, so the backoff starts over from 3s.
            $null = $revivals.Clear()
            $attempt = 0
        }
        $attempt++
        [void]$revivals.Add($now)
        $waitSec = [int][Math]::Pow(3, $attempt)
        if ($waitSec -gt $script:HoldReviveMaxWaitSec) { $waitSec = $script:HoldReviveMaxWaitSec }
        Write-RuntimeLog $script:OwnerLog ('AVATAR_OWNER_REVIVE attempt={0} held_s={1} reason={2} next_in_s={3} revivals_in_window={4} window_s={5} ceiling_s={6} -- durable owner: it keeps asking until a Start is answered or a Stop is asked for' -f `
            $attempt, $heldFor, $reason, $waitSec, $revivals.Count, $script:HoldReviveWindowSec, $script:HoldReviveMaxWaitSec)
        $changed = Wait-AvatarReviveBackoff $waitSec
        if ($changed) {
            Write-RuntimeLog $script:OwnerLog ('AVATAR_OWNER_REVIVE_ABANDONED pending_wait_s={0} because={1} -- the ask changed while we were backing off, and the ask always wins' -f `
                $waitSec, $changed)
            return $one
        }
        # Re-resolve who should be running: the pointer is the source of truth, and a
        # Switch that moved on while we were dead must not be undone by this revival.
        $nextId = ''
    }
}

function Invoke-AvatarHoldOnce([hashtable]$F, [string]$AvatarId, [int]$ReadyTimeoutSec) {
    # One ownership term: start idempotently, record this process as owner, block until
    # the service is really gone, classify the death and write the verdict. Reviving an
    # unrequested death is NOT done here -- see Invoke-AvatarHold, the only caller.
    $null = Set-AvatarTaskIdleSetting
    $target = Resolve-AvatarTarget $AvatarId $F
    if (-not $AvatarId) {
        # A queued -Action Start (Task mode) request outranks the pointer: the wrapper
        # is the one that launches, so it has to know who it is launching.
        $pre = Read-AvatarStateFile
        if ($pre -and [string]$pre.desired_avatar_id) {
            $entry = Get-AvatarEntryById ([string]$pre.desired_avatar_id)
            $entryState = [string]$entry.state
            if ($entry -and (-not $entryState -or $entryState -eq 'ready')) {
                $target = [pscustomobject]@{ id = [string]$pre.desired_avatar_id; source = 'state_request' }
            }
        }
    }
    Write-RuntimeLog $script:OwnerLog ('AVATAR_OWNER_START pid={0} target={1} source={2} state_file_present={3}' -f `
        $PID, $target.id, $target.source, [string](Test-Path -LiteralPath $script:StateFile))
    $lock = Get-AvatarLock 30
    $script:AvatarLock = $lock
    if (-not $lock) { Write-RuntimeLog $script:OwnerLog ('AVATAR_OWNER_START_UNLOANNED pid={0} reason=busy' -f $PID) }
    $result = $null
    try {
        $result = Invoke-AvatarStart $F ([string]$target.id) $ReadyTimeoutSec 'Direct' 'HOLD'
    } finally {
        # Release BEFORE waiting: the wrapper must not lock out the very Stop that is
        # going to take the service down.
        Release-AvatarLock $script:AvatarLock
        $script:AvatarLock = $null
    }
    $status = $result.status
    $launcherPid = [int]$status.launcher_pid
    $listenerPid = [int]$status.listener_pid
    $waitPid = $launcherPid
    if (-not $waitPid) { $waitPid = $listenerPid }
    $procObj = $null
    if ($waitPid -gt 0) {
        try { $procObj = Get-Process -Id $waitPid -ErrorAction Stop; $null = $procObj.Handle } catch { $procObj = $null }
    }
    # ...and take one for the listener too, at the only moment it is guaranteed alive.
    $null = Get-AvatarCachedProc $launcherPid
    $null = Get-AvatarCachedProc $listenerPid
    if (-not $launcherPid -and -not $listenerPid) {
        # Nothing to hold YET. Somebody else's start may still be in flight (this wrapper
        # was queued by a Task-mode Ensure, which is exactly the case), and an owner that
        # exits here leaves :8010 running with no parent and no exit record. Wait a bound
        # time for a process this wrapper can name -- it does not launch anything itself.
        $appearDeadline = (Get-Date).AddSeconds($ReadyTimeoutSec)
        $appearBegan = Get-Date
        while ((Get-Date) -lt $appearDeadline) {
            $seen = Get-AvatarStatus $F
            if ([int]$seen.launcher_pid -gt 0 -or [int]$seen.listener_pid -gt 0) {
                $launcherPid = [int]$seen.launcher_pid
                $listenerPid = [int]$seen.listener_pid
                $waitPid = $(if ($launcherPid) { $launcherPid } else { $listenerPid })
                try { $procObj = Get-Process -Id $waitPid -ErrorAction Stop; $null = $procObj.Handle } catch { $procObj = $null }
                break
            }
            Start-Sleep -Seconds 2
        }
        Write-RuntimeLog $script:OwnerLog ('AVATAR_OWNER_APPEARED pid={0} launcher_pid={1} listener_pid={2} waited_s={3}' -f `
            $PID, $launcherPid, $listenerPid, [int]((Get-Date) - $appearBegan).TotalSeconds)
    }
    # Did this term ever own a real process? The revival policy needs the difference
    # between "it ran and died" and "it never started": reviving the second turns a
    # broken environment into a silent restart loop that hides the reason.
    $everHeld = 0
    if ($launcherPid -gt 0 -or $listenerPid -gt 0) { $everHeld = 1 }
    # Say what the launch actually produced. Claiming RUNNING here no matter what the
    # start reported is how a wrapper that never launched anything (the old self-match)
    # left the document asserting a service nobody had started.
    $holdState = [string]$status.state
    if ($holdState -notin @('RUNNING', 'STARTING', 'FAILED')) { $holdState = 'STARTING' }
    # OWNERSHIP REPAIR 2026-09-25: record the birth of every pid this ownership term
    # names, so stop/reaper decisions can re-verify (pid, CreationTime) instead of
    # trusting a number that a later generation may reuse.
    $ownerBirth = Get-AvatarProcessBirth $PID
    $launcherBirth = Get-AvatarProcessBirth $launcherPid
    $listenerBirth = Get-AvatarProcessBirth $listenerPid
    $null = Write-AvatarState @{
        state          = $holdState; owner_pid = $PID; owner_mode = 'HOLD'; supervisor_pid = $PID
        launcher_pid   = $launcherPid; listener_pid = $listenerPid
        owner_start_time = $ownerBirth; launcher_start_time = $launcherBirth; listener_start_time = $listenerBirth
        avatar_id      = [string]$target.id; started_at = (Get-Date).ToString('o')
        exit_reason    = ''; last_error = $(if ($holdState -eq 'FAILED') { [string]$status.last_error } else { '' })
        desired_avatar_id = ''
    } $F
    Write-RuntimeLog $script:OwnerLog ('AVATAR_OWNER_WAIT owner_pid={0} launcher_pid={1} listener_pid={2} wait_pid={3} verdict={4}' -f `
        $PID, $launcherPid, $listenerPid, $waitPid, $result.verdict)

    # Block while EITHER half lives. The launcher can exit while its re-exec'd child
    # keeps the port (fact 2), and an owner that quit there would stop being the tree's
    # parent and lose the service's whole reason for having an owner.
    #
    # 250ms for the cheap process probe, so the death is dated to a quarter of a second
    # instead of a two-second blur; the expensive status read (a full CIM process scan)
    # stays on a ~2s cadence every 8th tick. The ALIVE line once a minute is the wrapper
    # proving it was still there -- without it, a child that died took the timeline with
    # it, because the only other line in this log was the exit.
    $tick = 0
    $heldSince = Get-Date
    $null = Update-AvatarProcessRing
    while ($true) {
        $tick++
        $launcherAlive = [bool]($launcherPid -gt 0 -and (Get-Process -Id $launcherPid -ErrorAction SilentlyContinue))
        $listenerAlive = [bool]($listenerPid -gt 0 -and (Get-Process -Id $listenerPid -ErrorAction SilentlyContinue))
        if (($tick % 8) -eq 0) {
            $current = Get-AvatarStatus $F
            if ($current.listener_pid -gt 0) {
                # The listener pid can change under us (a Start that another pid queued,
                # or a revival). Cache the handle the first time we see each one, or the
                # exit status of the process that actually served :8010 is unreadable at
                # the moment we need it.
                $null = Get-AvatarCachedProc ([int]$current.listener_pid)
                $listenerAlive = $true; $listenerPid = [int]$current.listener_pid
            }
            $nowFile = Read-AvatarStateFile
            if ($nowFile -and [string]$nowFile.state -eq 'STOPPING') { break }
            # Tripwire: while we hold, any separate process that could end the child is
            # named and logged the moment it exists. One filtered CIM query on the ~2s
            # cadence is the only way an unelevated box gets near "who killed it".
            $null = Get-AvatarKillerSightings $PID $launcherPid $listenerPid
            # ...and the ring is how we learn the *shape* of the death a second later:
            # one pair of pids missing = targeted, a crowd missing = a machine-wide reap.
            $null = Update-AvatarProcessRing
            if ($tick % 1200 -eq 0) {
                Write-RuntimeLog $script:OwnerLog ('AVATAR_OWNER_ALIVE owner_pid={0} launcher_pid={1} listener_pid={2} held_s={3}' -f `
                    $PID, $launcherPid, $listenerPid, [int]((Get-Date) - $heldSince).TotalSeconds)
            }
        }
        if (-not $launcherAlive -and -not $listenerAlive) { break }
        Start-Sleep -Milliseconds 250
    }
    # The dossier is collected in the same second as the disappearance, because every one
    # of its parts (log mtimes, port state, WER, the task's own events) is a race against
    # whatever else touches them next.
    $dossier = Get-AvatarDeathDossier $F $launcherPid $listenerPid $procObj $heldSince
    $exitCode = $dossier.exit_code
    $declared = 'STOPPED'
    $reason = $dossier.exit_reason
    $finalFile = Read-AvatarStateFile
    if ($finalFile -and [string]$finalFile.state -eq 'STOPPING') { $reason = 'STOP_REQUESTED' }
    elseif ($finalFile -and [string]$finalFile.exit_reason -like 'SWITCH_AWAY*') { $reason = [string]$finalFile.exit_reason }
    elseif ($finalFile -and [string]$finalFile.state -eq 'FAILED') { $declared = 'FAILED'; $reason = [string]$finalFile.last_error }
    elseif ([int]$exitCode -ne 0) { $declared = 'FAILED'; $reason = ('CHILD_EXIT_NONZERO ' + $dossier.exit_reason) }
    else {
        # Exit code 0 is NOT evidence of a clean stop, and -- corrected 2026-09-25 by
        # measurement (runtime/debug/kill_fingerprint/20260925_095118) -- it is not
        # evidence of a kill by our own tools either. The comment that used to sit here
        # claimed TerminateProcess(pid, 0) was "what Stop-Process -Force and taskkill /F
        # hand back". It is not: Stop-Process -Force and [Diagnostics.Process]::Kill()
        # leave 0xFFFFFFFF, and taskkill /F leaves 0x00000001. A zero read straight from
        # the listener's own handle therefore means one of exactly two things, and both
        # are failures: either an actor passed uExitCode 0 deliberately, or this process
        # called os._exit(0) on itself. app.py now wraps os._exit and os.kill precisely so
        # the witness file separates those two, and the dossier label says which way it
        # fell. Stamping this STOPPED is how :8010 got to look 'stopped' while the person
        # simply vanished.
        $declared = 'FAILED'
    }
    $myGen = 0
    if ($result.launch) { $myGen = [int]$result.launch.generation }
    if ($finalFile -and $myGen -gt 0 -and [int]$finalFile.generation -ne $myGen -and $declared -eq 'STOPPED') {
        # Somebody else already owned the file again (a Switch moved on); do not stamp
        # our verdict over theirs.
        Write-RuntimeLog $script:OwnerLog ('AVATAR_OWNER_EXIT_SKIPPED gen_theirs={0} gen_ours={1}' -f `
            [int]$finalFile.generation, $myGen)
    } else {
        $null = Write-AvatarState @{
            state             = $declared; owner_pid = 0; owner_mode = ''
            launcher_pid      = 0; listener_pid = 0; listener_pids = @()
            exit_code         = $exitCode; exit_reason = $reason
            last_error        = $(if ($declared -eq 'FAILED') { $reason } else { '' })
            death_at          = (Get-Date).ToString('o')
            death_evidence    = $dossier.evidence
            desired_avatar_id = ''
        } $F
    }
    try {
        $pids = Read-ServicePids
        $tracked = [int]$pids.avatar
        if ($tracked -gt 0 -and -not (Get-Process -Id $tracked -ErrorAction SilentlyContinue)) {
            Set-ObjectValue $pids 'avatar' 0
            Set-ServiceOwner $pids 'avatar' $null
            Save-ServicePids $pids
        }
    } catch {}
    Write-RuntimeLog $script:OwnerLog ('AVATAR_OWNER_EXIT owner_pid={0} launcher_pid={1} listener_pid={2} exit_code={3} reason={4} state={5} evidence={6}' -f `
        $PID, $launcherPid, $listenerPid, $exitCode, $reason, $declared, $dossier.evidence)
    return [pscustomobject]@{ verdict = $declared; exit_code = $exitCode; exit_reason = $reason; launcher_pid = $launcherPid; listener_pid = $listenerPid; ever_held = $everHeld; held_s = [int]((Get-Date) - $heldSince).TotalSeconds }
}

# ---------------------------------------------------------------------------
# dispatch
# ---------------------------------------------------------------------------

function Invoke-AvatarSupervisorAction([string]$ActionName, [string]$TargetId, [int]$TimeoutSec, [string]$Mode, [bool]$AsJson) {
    # Single dispatcher. The payload goes to the pipeline, the exit code to
    # $script:AvatarExitCode -- mixing both into one return value would make
    # 'exit (Invoke-...)' fail.
    $script:AvatarExitCode = 0
    $payload = $null
    $lock = $null
    try {
        $F = Get-AvatarFacts
        switch ($ActionName) {
            'Status' { $payload = Get-AvatarStatus $F }
            'FixTask' { $payload = [pscustomobject]@{ fix = (Set-AvatarTaskIdleSetting); task = (Get-AvatarTaskInfo) } }
            'Hold' { $payload = Invoke-AvatarHold $F $TargetId $TimeoutSec }
            default {
                # One variable for the whole call, because the launch code handed inside
                # may have released it and taken a NEW object back. $lock below would be a
                # disposed handle whose ReleaseMutex() silently throws in the catch.
                $lock = Get-AvatarLock 120
                $script:AvatarLock = $lock
                if (-not $lock) { throw 'SUPERVISOR_BUSY another lifecycle call is in progress' }
                switch ($ActionName) {
                    'Start' { $payload = Invoke-AvatarStart $F $TargetId $TimeoutSec $Mode 'START' }
                    'Ensure' { $payload = Invoke-AvatarEnsure $F $TargetId $TimeoutSec $Mode }
                    'Switch' { $payload = Invoke-AvatarSwitch $F $TargetId $TimeoutSec $Mode }
                    'Stop' { $payload = Invoke-AvatarStop $F 'STOP_REQUESTED' }
                }
            }
        }
        if ($ActionName -in @('Start', 'Ensure', 'Switch')) {
            $v = [string]$payload.verdict
            if ($v -in @('SWITCH_REQUIRED', 'TARGET_NOT_REGISTERED', 'TARGET_NOT_READY', 'PORT_NOT_RELEASED',
                         'STARTED_UNHEALTHY', 'SWITCHED_UNHEALTHY', 'SWITCHED_WRONG_ID')) { $script:AvatarExitCode = 2 }
        }
    } catch {
        $msg = $_.Exception.Message
        try { Write-RuntimeLog $script:SupLog ('ACTION_FAILED action={0} error={1}' -f $ActionName, $msg) } catch {}
        $script:AvatarExitCode = 1
        $payload = [pscustomobject]@{ verdict = 'FAILED'; action = $ActionName; error = $msg; status = (Get-AvatarStatus) }
    } finally {
        # $script:AvatarLock, not $lock: a Task/Direct wait may have handed the original
        # object over and re-acquired a fresh one, and releasing the stale handle would
        # leave the one we really own held forever.
        Release-AvatarLock $script:AvatarLock
        $script:AvatarLock = $null
    }
    if ($AsJson) {
        # The JSON is the machine-readable contract. On a GBK console a Chinese
        # exception message inside it would otherwise land as undecodable bytes for
        # the parser on the other side of the pipe.
        try { [Console]::OutputEncoding = [System.Text.Encoding]::UTF8 } catch {}
        Write-Output ($payload | ConvertTo-Json -Depth 8 -Compress)
    } else {
        Write-Host ($payload | ConvertTo-Json -Depth 8 | Out-String)
    }
    return
}

if (-not $script:SourcedIn) {
    # NOT '$null = ...': the payload travels the output stream, and discarding it is
    # exactly how a -Json caller ends up parsing an empty string. The exit code rides
    # on $script:AvatarExitCode because both would otherwise share one return value.
    Invoke-AvatarSupervisorAction -ActionName $Action -TargetId $AvatarId -TimeoutSec $ReadyTimeoutSec -Mode $OwnerMode -AsJson ([bool]$Json)
    exit $script:AvatarExitCode
}
