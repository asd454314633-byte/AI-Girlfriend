$ErrorActionPreference = 'Stop'
$script:AppRoot = Split-Path -Parent $PSScriptRoot
$script:RuntimeRoot = Join-Path $script:AppRoot 'runtime'
$script:NativeRoot = Join-Path $script:RuntimeRoot 'windows'
$script:LogRoot = Join-Path $script:AppRoot 'logs\runtime'
$script:PidFile = Join-Path $script:NativeRoot 'service_pids.json'
$script:S2SPython = Join-Path $script:RuntimeRoot 'core\venv\Scripts\python.exe'
$script:S2SPythonRuntime = (& $script:S2SPython -c 'import sys; print(sys._base_executable)').Trim()
$script:S2SExe = Join-Path $script:RuntimeRoot 'core\venv\Scripts\speech-to-speech.exe'
$script:AvatarPython = Join-Path $script:RuntimeRoot 'avatar\venv\Scripts\python.exe'
$script:UiPython = $script:S2SPython
$script:CudaBin = if($env:CUDA_PATH){Join-Path $env:CUDA_PATH 'bin'}else{Join-Path $script:AppRoot 'external\cuda\bin'}

New-Item -ItemType Directory -Force -Path $script:LogRoot,$script:NativeRoot | Out-Null

# ---- Provider manifest reader -------------------------------------------------
# The PowerShell lifecycle layer now reads the SAME manifests the Python
# (providers/registry.py) and browser (/api/voice-models) layers read, so
# registering a new TTS/LLM provider is a manifest edit, never a .ps1 edit.
# This mirrors providers/registry.py: load_manifest / provider_ids / flag_values /
# spec_for_flag / default_selection / labels / health_plan / worker_plan /
# launch_args. Nothing below names a specific provider: everything iterates.
# In-memory cache keyed by provider kind; a child pwsh process starts fresh.
$script:ProviderManifestCache = @{}

# PS 5.1 on this machine decodes a BOM-less UTF-8 file as ANSI (codepage 936).  A GBK
# lead byte then swallows the following ASCII byte whenever it is '{', '}' or '\' --
# which is exactly what breaks JSON parsing mid-file ("expected ':' or '}'"), and it
# only happens for manifests that carry Chinese labels.  Every JSON read below goes
# through this helper so the byte decoding is never left to the console codepage.
function Read-JsonText([string]$Path) {
    $full = [System.IO.Path]::GetFullPath($Path)
    $text = [System.IO.File]::ReadAllText($full, [System.Text.Encoding]::UTF8)
    if ($text.Length -gt 0 -and $text[0] -eq [char]0xFEFF) { $text = $text.Substring(1) }
    return $text
}

function Read-JsonFile([string]$Path) {
    return (ConvertFrom-Json -InputObject (Read-JsonText $Path))
}

# PS 5.1's `Set-Content -Encoding UTF8` prepends a BOM, and Python's json.loads on a
# 'utf-8' stream then refuses the file -- callers swallow that as an empty dict, which
# looks exactly like "no worker is running".  Write BOM-less UTF-8 explicitly.
function Write-JsonTextUtf8([string]$Path,[string]$Text) {
    $full = [System.IO.Path]::GetFullPath($Path)
    [System.IO.File]::WriteAllText($full, $Text, (New-Object System.Text.UTF8Encoding($false)))
}

# Atomic replace for a state file, with the temp name unique per write.  A *fixed*
# `foo.tmp` name is enough for one lingering handle -- an antivirus scan, or a sibling
# that died still holding it -- to make every LATER write fail for good, which is how
# `service_pids.json` / `active_tts_backend.txt` / `active_model.json` stop accepting
# updates while the UI keeps reporting success.  The BFF hit exactly this and moved to
# unique names (runtime/ui/app/voice_models_admin.py:_write_json_atomic); every writer
# of a Windows-side state file goes through here so the rule has one owner.
# Encoding 'Ascii' keeps the .txt selectors byte-identical to the old
# `Set-Content -Encoding ASCII -NoNewline`.
function Write-AtomicFile([string]$Path,[string]$Text,[ValidateSet('Utf8NoBom','Ascii')][string]$Encoding='Utf8NoBom') {
    $full = [System.IO.Path]::GetFullPath($Path)
    # .NET, not Split-Path: PS 5.1 has no -LiteralPath on Split-Path (PowerShell 6 added
    # it), and a helper every state write goes through must not die on the host version
    # this deployment runs -- that is exactly what the replay harness caught here.
    $tmp = Join-Path ([System.IO.Path]::GetDirectoryName($full)) (
        [System.IO.Path]::GetFileName($full) + '.' + $PID + '.' +
        [guid]::NewGuid().ToString('N').Substring(0, 12) + '.tmp')
    try {
        $codec = if ($Encoding -eq 'Ascii') { [System.Text.Encoding]::ASCII }
                 else { New-Object System.Text.UTF8Encoding($false) }
        [System.IO.File]::WriteAllText($tmp, $Text, $codec)
        Move-Item -LiteralPath $tmp -Destination $full -Force
    } finally {
        # A half-written temp must not survive this call either way, or the next
        # writer inherits a file nobody can delete.
        if (Test-Path -LiteralPath $tmp) { Remove-Item -LiteralPath $tmp -Force -ErrorAction SilentlyContinue }
    }
}


function Get-ProviderManifestPath([string]$Kind) {
    if ([string]::IsNullOrWhiteSpace($Kind)) { throw 'PROVIDER_KIND_EMPTY' }
    $dir = Join-Path $script:RuntimeRoot 'core\providers\manifests'
    return (Join-Path $dir ($Kind.ToLowerInvariant() + '.json'))
}

function Get-ProviderManifest([string]$Kind,[switch]$AllowMissing) {
    $k = $Kind.ToUpperInvariant()
    if ($script:ProviderManifestCache.ContainsKey($k)) { return $script:ProviderManifestCache[$k] }
    $path = Get-ProviderManifestPath $k
    if (-not (Test-Path -LiteralPath $path)) {
        if ($AllowMissing) { return $null }
        throw "PROVIDER_MANIFEST_MISSING kind=$Kind path=$path"
    }
    $obj = Read-JsonFile $path
    # 索引器存在性检查而非 .PSObject.Properties.Name：StrictMode 下空 JSON 对象
    # （{}）的 Properties 集合访问 .Name 会抛「找不到属性 Name」（PS 5.1 实测）。
    if (-not $obj -or -not ($obj.PSObject.Properties['providers'])) {
        throw "PROVIDER_MANIFEST_NO_PROVIDERS kind=$Kind path=$path"
    }
    $script:ProviderManifestCache[$k] = $obj
    return $obj
}

# StrictMode-safe optional property read: returns $null when the object itself is
# null or the property is absent. Manifest rows deliberately carry heterogeneous
# optional fields (worker_cli_args, launch.env, cli_flag_aliases, worker,
# placeholders, handler, health inner fields ...), and Set-StrictMode turns a
# direct missing-property read into PropertyNotFoundException -- the reported
# SWITCH_FAILED target=qwen3 ("worker_cli_args") was exactly that. Every OPTIONAL
# manifest access below goes through this one owner; declared-required fields
# (id, enabled, providers) keep their direct reads.
function Get-ProviderProp($Obj,[string]$Name) {
    if (-not $Obj) { return $null }
    $prop = $Obj.PSObject.Properties[$Name]
    if ($prop) { return $prop.Value }
    return $null
}

function Test-ProviderPlaceholderIdentifier([string]$Name) {
    return ($Name -cmatch '^[A-Za-z_][A-Za-z0-9_]*$')
}

function Get-ProviderIds([string]$Kind,[switch]$EnabledOnly,[switch]$UiOnly) {
    $out = New-Object System.Collections.Generic.List[string]
    foreach ($p in @((Get-ProviderManifest $Kind).providers)) {
        if ($EnabledOnly -and -not [bool]$p.enabled) { continue }
        if ($UiOnly) {
            $uv = $true
            if ($p.PSObject.Properties['ui_visible']) { $uv = [bool]$p.ui_visible }
            if (-not $uv) { continue }
        }
        if ($p.id) { $out.Add([string]$p.id) }
    }
    return $out.ToArray()
}

function Get-ProviderFlags([string]$Kind,[switch]$EnabledOnly) {
    # id (uin) handler.cli_flag_value (uin) handler.cli_flag_aliases -- mirrors
    # registry.flag_values + spec_for_flag's name set. Used to build the reverse
    # --tts inference regex and to resolve a captured token back to a provider id.
    $out = New-Object System.Collections.Generic.List[string]
    foreach ($p in @((Get-ProviderManifest $Kind).providers)) {
        if ($EnabledOnly -and -not [bool]$p.enabled) { continue }
        $names = @([string]$p.id)
        # handler / cli_flag_value / cli_flag_aliases 都是可选字段：StrictMode 下对
        # 缺失属性直接取值会抛 PropertyNotFoundException，统一走 Get-ProviderProp。
        $h = Get-ProviderProp $p 'handler'
        $flagValue = Get-ProviderProp $h 'cli_flag_value'
        if ($flagValue) { $names += [string]$flagValue }
        $aliases = Get-ProviderProp $h 'cli_flag_aliases'
        if ($aliases) { foreach ($a in @($aliases)) { $names += [string]$a } }
        foreach ($n in $names) { if ($n -and -not $out.Contains($n)) { $out.Add($n) } }
    }
    return $out.ToArray()
}

function Get-ProviderSpec([string]$Kind,[string]$IdOrFlag) {
    # Resolve by id, cli_flag_value or declared alias (registry.spec_for_flag).
    if ([string]::IsNullOrEmpty($IdOrFlag)) { return $null }
    foreach ($p in @((Get-ProviderManifest $Kind).providers)) {
        $names = New-Object System.Collections.Generic.List[string]
        $names.Add([string]$p.id)
        $h = Get-ProviderProp $p 'handler'
        $flagValue = Get-ProviderProp $h 'cli_flag_value'
        if ($flagValue) { $names.Add([string]$flagValue) }
        # 同上：cli_flag_aliases 可选，缺失不抛（LIVE_REGRESSION_01 STEP 3）。
        $aliases = Get-ProviderProp $h 'cli_flag_aliases'
        if ($aliases) { foreach ($a in @($aliases)) { $names.Add([string]$a) } }
        if ($names.Contains($IdOrFlag)) { return $p }
    }
    return $null
}

function Get-ProviderDefault([string]$Kind) {
    return [string](Get-ProviderManifest $Kind).default_selection
}

function Get-ProviderLabel($Spec) {
    if (-not $Spec) { return '' }
    $zh = Get-ProviderProp $Spec 'display_name_zh'
    if ($zh) { return [string]$zh }
    $en = Get-ProviderProp $Spec 'display_name'
    if ($en) { return [string]$en }
    return [string]$Spec.id
}

function Get-ProviderHandlerClass($Spec) {
    $cls = Get-ProviderProp (Get-ProviderProp $Spec 'handler') 'class'
    if ($cls) { return [string]$cls }
    return ''
}

function Get-ProviderSelectionFlag([string]$Kind) {
    # The command-line switch that selects a provider of this kind (e.g. "--tts"),
    # read from the manifest (health.command_line_flag, else launch.cli_args[0])
    # so no lifecycle script hardcodes it. health / launch and their inner fields
    # are optional per row (the avatar rows carry no health block at all), so every
    # access goes through the StrictMode-safe optional read.
    $mf = Get-ProviderManifest $Kind
    foreach ($p in @($mf.providers)) {
        $flag = Get-ProviderProp (Get-ProviderProp $p 'health') 'command_line_flag'
        if ($flag) { return [string]$flag }
    }
    foreach ($p in @($mf.providers)) {
        $cli = Get-ProviderProp (Get-ProviderProp $p 'launch') 'cli_args'
        if ($cli -and @($cli).Count) { return [string]@($cli)[0] }
    }
    throw "PROVIDER_SELECTION_FLAG_UNDECLARED kind=$Kind"
}

function Get-ProviderHealthPort($Spec) {
    $h = Get-ProviderProp $Spec 'health'
    if (-not $h) { return 0 }
    if (Get-ProviderProp $h 'port') { return [int]$h.port }
    $base = Get-ProviderProp $h 'base_url'
    if ($base) {
        $m = [regex]::Match([string]$base, ':(\d+)')
        if ($m.Success) { return [int]$m.Groups[1].Value }
    }
    return 0
}

function Get-ProviderHealthHost($Spec) {
    # The host half of the same declared fact: health.host, else parsed out of
    # health.base_url, else loopback. Nothing here names a provider.
    $h = Get-ProviderProp $Spec 'health'
    if (-not $h) { return '127.0.0.1' }
    $hostName = Get-ProviderProp $h 'host'
    if ($hostName) { return [string]$hostName }
    $base = Get-ProviderProp $h 'base_url'
    if ($base) {
        $m = [regex]::Match([string]$base, '^(?:[A-Za-z][A-Za-z0-9+.-]*://)?([^/:?#]+)')
        if ($m.Success) { return $m.Groups[1].Value }
    }
    return '127.0.0.1'
}

function Get-ProviderReadyTimeout($Spec) {
    # How long a worker may take to prove itself ready, from health.ready_timeout_s.
    $t = Get-ProviderProp (Get-ProviderProp $Spec 'health') 'ready_timeout_s'
    if ($t) { return [int]$t }
    return 900
}

function Get-ProviderLaunchContext($Spec) {
    # Build the placeholder context that Expand-ProviderLaunch consumes out of the
    # provider row itself: every name in launch.placeholders whose declared value
    # resolves to a real path on this machine. Values that are prose notes about an
    # externally installed asset deliberately do NOT enter the context, so the
    # expander reports them in missing_placeholders and the caller fails loudly
    # instead of handing a sentence to a model as an argument. launch / placeholders
    # are optional per row; the StrictMode-safe read keeps a row without them from
    # throwing PropertyNotFoundException.
    $ctx = @{}
    $placeholders = Get-ProviderProp (Get-ProviderProp $Spec 'launch') 'placeholders'
    if (-not $placeholders) { return $ctx }
    # 注意：PS 5.1 里 ConvertFrom-Json '{}' 是非 null 的空 PSCustomObject，而
    # StrictMode 下对空对象的 .PSObject.Properties.Name 直接抛「找不到属性 Name」
    # （非空对象不会 -- 5 个 TTS 测试行 placeholders:{} 全部踩中）。所以这里枚举
    # 成员对象本身，逐成员读 .Name/.Value：空集合自然跳过，成员恒有这两个属性。
    foreach ($prop in @($placeholders.PSObject.Properties)) {
        $name = [string]$prop.Name
        $declared = [string]$prop.Value
        if ([string]::IsNullOrWhiteSpace($declared)) { continue }
        $candidate = $declared.Replace('/', [System.IO.Path]::DirectorySeparatorChar)
        if (-not [System.IO.Path]::IsPathRooted($candidate)) { $candidate = Join-Path $script:AppRoot $candidate }
        try { $full = [System.IO.Path]::GetFullPath($candidate) } catch { continue }
        if (Test-Path -LiteralPath $full) { $ctx[[string]$name] = $full; continue }
        # A placeholder is not always a path. `AVATAR_ID: wav2lip256_avatar1` is a plain model
        # identifier that lives inside the provider's own asset set, and refusing it here used to
        # push the lifecycle script back into hand-writing the value (Start-AI-Girlfriend.ps1
        # assembled `--avatar_id wav2lip256_avatar1` by hand for exactly that reason). Accept a
        # single safe token verbatim; prose notes still fail the shape test and stay out.
        if ($declared -cmatch '^[\w][\w.-]{0,63}$') { $ctx[[string]$name] = $declared }
    }
    return $ctx
}

function Get-ExternalProviderWorkers([string]$Kind) {
    $out = New-Object System.Collections.Generic.List[object]
    foreach ($p in @((Get-ProviderManifest $Kind).providers)) {
        if ([bool]$p.enabled -and ((Get-ProviderProp $p 'resident') -eq 'external_worker')) { $out.Add($p) }
    }
    return $out.ToArray()
}

function Get-WorkerPidKeys() {
    $keys = New-Object System.Collections.Generic.List[string]
    foreach ($kind in @('TTS','LLM','STT','AVATAR','TOOL')) {
        $mf = Get-ProviderManifest $kind -AllowMissing
        if (-not $mf) { continue }
        foreach ($p in @($mf.providers)) {
            $workerProp = $p.PSObject.Properties['worker']
            $worker = if ($workerProp) { $workerProp.Value } else { $null }
            if ($worker -and $worker.PSObject.Properties['pid_key']) {
                $k = [string]$worker.pid_key
                if (-not $keys.Contains($k)) { $keys.Add($k) }
            }
        }
    }
    return $keys.ToArray()
}

function Get-ProviderMaxResident([string]$Kind) {
    $mf = Get-ProviderManifest $Kind
    $inv = Get-ProviderProp $mf 'invariants'
    if ($inv -and $inv.PSObject.Properties['max_resident_provider_instances']) {
        return [int]$inv.max_resident_provider_instances
    }
    return 0
}

function Expand-ProviderLaunchItems([object[]]$Items,[hashtable]$Context,[System.Collections.Generic.List[string]]$Missing) {
    $out = New-Object System.Collections.Generic.List[string]
    if (-not $Items) { return $out.ToArray() }
    foreach ($item in $Items) {
        $text = [string]$item
        $whole = ($text.Length -ge 2 -and $text.StartsWith('{') -and $text.EndsWith('}'))
        $key = ''
        if ($whole) {
            $key = $text.Substring(1, $text.Length - 2)
            if (-not (Test-ProviderPlaceholderIdentifier $key)) { $whole = $false }
        }
        if ($whole) {
            if ($Context.ContainsKey($key)) { $out.Add([string]$Context[$key]) }
            else { $Missing.Add($key); $out.Add($text) }
            continue
        }
        foreach ($name in ($Context.Keys | Sort-Object { $_.Length } -Descending)) {
            $text = $text.Replace('{' + $name + '}', [string]$Context[$name])
        }
        foreach ($m in [regex]::Matches($text, '\{([A-Za-z0-9_]+)\}')) { $Missing.Add($m.Groups[1].Value) }
        $out.Add($text)
    }
    return $out.ToArray()
}

function Expand-ProviderLaunch($Spec,[hashtable]$Context) {
    # Substitute launch.cli_args / worker_cli_args / requires / env from $Context. An
    # unsatisfied placeholder is REPORTED in missing_placeholders (never dropped),
    # mirroring registry.launch_args(). Callers must assert the list is empty.
    # All four are OPTIONAL per-row fields: qwen3's TTS row declares neither
    # worker_cli_args nor env, and Set-StrictMode turns a direct missing-property
    # read into PropertyNotFoundException -- the reported
    # SWITCH_FAILED target=qwen3 root cause. Every read below is the optional-safe
    # one; a missing field keeps its empty default instead of throwing.
    if (-not $Context) { $Context = @{} }
    $launch = Get-ProviderProp $Spec 'launch'
    $missing = New-Object System.Collections.Generic.List[string]
    $cli = @(); $wk = @(); $req = @(); $envMap = @{}
    if ($launch) {
        $cliArgs = Get-ProviderProp $launch 'cli_args'
        if ($cliArgs) { $cli = Expand-ProviderLaunchItems @($cliArgs) $Context $missing }
        $wkArgs = Get-ProviderProp $launch 'worker_cli_args'
        if ($wkArgs) { $wk = Expand-ProviderLaunchItems @($wkArgs) $Context $missing }
        $reqArgs = Get-ProviderProp $launch 'requires'
        if ($reqArgs) { $req = Expand-ProviderLaunchItems @($reqArgs) $Context $missing }
        $envObj = Get-ProviderProp $launch 'env'
        if ($envObj) {
            # launch.env is the only sanctioned place for a switch the worker reads at
            # import time (e.g. AVATAR_SEGMENT_GATE). Setting $env: by hand inside a
            # lifecycle script would make the value unreplayable from the manifest.
            # Same StrictMode trap as Get-ProviderLaunchContext: an EMPTY {} env block
            # throws on .PSObject.Properties.Name, so enumerate the member objects
            # themselves and read .Name/.Value per member (an empty set skips).
            $names = New-Object System.Collections.Generic.List[string]
            $values = New-Object System.Collections.Generic.List[string]
            foreach ($envProp in @($envObj.PSObject.Properties)) {
                $names.Add([string]$envProp.Name)
                $values.Add([string]$envProp.Value)
            }
            # @() here is load-bearing: a function returning a 1-element array hands the
            # caller a scalar, and indexing that scalar yields its first CHARACTER
            # ("enforce" -> "e"), which is exactly what this probe caught at 10:2x.
            $expanded = @(Expand-ProviderLaunchItems @($values) $Context $missing)
            for ($i = 0; $i -lt $names.Count; $i++) { $envMap[$names[$i]] = [string]$expanded[$i] }
        }
    }
    return [pscustomobject]@{
        provider = [string]$Spec.id
        args = $cli
        worker_args = $wk
        requires = $req
        env = $envMap
        missing_placeholders = @($missing | Sort-Object -Unique)
    }
}

function Format-ProviderHookName([string]$Verb,[string]$Key) {
    # Dispatch by manifest worker.pid_key (NOT by a hardcoded provider id):
    # pid_key "fish" -> "Start-Fish" / "Stop-Fish". A new provider hooks in by
    # shipping a Start-/Wait-/Stop-<PascalCase(pid_key)> function, or by relying
    # on the generic path in Start-ProviderWorker when it declares no such hook.
    if ([string]::IsNullOrEmpty($Key)) { return $Verb }
    return $Verb + '-' + $Key.Substring(0,1).ToUpper() + $Key.Substring(1)
}

function Test-ProviderReady($Spec,[int]$S2sPid,[object]$Pool) {
    # One readiness verdict from health.* only (registry.health_plan). Returns
    # 'RUNNING' / 'STOPPED'. Non-blocking; Status and Wait-ProviderReady share it.
    # The worker block and every health inner field are optional per row (qwen3's
    # TTS row declares worker:null; some rows carry no worker block at all), so all
    # accesses below are the StrictMode-safe optional reads -- a missing field
    # degrades to the STOPPED logic, never a PropertyNotFoundException.
    $h = Get-ProviderProp $Spec 'health'
    $scheme = if ($h) { [string](Get-ProviderProp $h 'scheme') } else { '' }
    switch ($scheme) {
        'tcp' {
            $state = Read-ServicePids
            $pidKey = [string](Get-ProviderProp (Get-ProviderProp $Spec 'worker') 'pid_key')
            $hostName = Get-ProviderHealthHost $Spec
            if ($pidKey -and [int]$state.$pidKey -gt 0 -and (Test-Tcp $hostName (Get-ProviderHealthPort $Spec) 500)) { return 'RUNNING' }
            return 'STOPPED'
        }
        'http' {
            $state = Read-ServicePids
            $pidKey = [string](Get-ProviderProp (Get-ProviderProp $Spec 'worker') 'pid_key')
            if (-not ($pidKey -and [int]$state.$pidKey -gt 0)) { return 'STOPPED' }
            $base = [string](Get-ProviderProp $h 'base_url')
            try {
                $eps = Get-ProviderProp $h 'endpoints'
                foreach ($ep in @($eps)) {
                    if (-not $ep) { continue }
                    $headers = @{}
                    $auth = Get-ProviderProp $ep 'auth'
                    if ($auth -and ([string]$auth).StartsWith('bearer:')) {
                        $credFile = Get-ProviderProp $h 'credentials_file'
                        if (-not $credFile) { return 'STOPPED' }
                        $credPath = Join-Path $script:AppRoot (([string]$credFile).Replace('/','\'))
                        $key = (Read-JsonText $credPath).Trim()
                        $headers = @{ Authorization = "Bearer $key" }
                    }
                    $resp = Invoke-RestMethod -Uri ($base + [string](Get-ProviderProp $ep 'path')) -Headers $headers -TimeoutSec 5 -Proxy $null
                    foreach ($tw in @((Get-ProviderProp $ep 'true_when'))) {
                        if (-not $tw) { continue }
                        if (-not [bool]$resp.$([string]$tw)) { return 'STOPPED' }
                    }
                }
                return 'RUNNING'
            } catch { return 'STOPPED' }
        }
        'in_process_cli_probe' {
            if ($S2sPid -le 0) { return 'STOPPED' }
            $proc = Get-CimInstance Win32_Process -Filter "ProcessId=$S2sPid" -ErrorAction SilentlyContinue
            if (-not $proc) { return 'STOPPED' }
            $flag = [string](Get-ProviderProp $h 'command_line_flag')
            $expect = [string](Get-ProviderProp $h 'expected_value')
            if ($flag -and $proc.CommandLine -match ("{0}(?:\s+|=){1}(?:\s|$)" -f [regex]::Escape($flag), [regex]::Escape($expect))) {
                if ($Pool -and [int]$Pool.size -eq 1) { return 'RUNNING' }
                return 'STOPPED'
            }
            return 'STOPPED'
        }
        default { return 'STOPPED' }
    }
}

function Wait-ProviderReady($Spec,[int]$TimeoutSec,[int]$ProcessId=0) {
    # Blocking form of Test-ProviderReady for the generic start path. Timeouts and
    # process-exit are surfaced from the declared health plan, never from a
    # provider-specific wait function.
    $timeout = if ($TimeoutSec -gt 0) { $TimeoutSec } else { Get-ProviderReadyTimeout $Spec }
    $deadline = (Get-Date).AddSeconds($timeout)
    while ((Get-Date) -lt $deadline) {
        if ($ProcessId -gt 0 -and -not (Get-Process -Id $ProcessId -ErrorAction SilentlyContinue)) {
            throw "PROCESS_EXITED_BEFORE_READY pid=$ProcessId"
        }
        if ((Test-ProviderReady $Spec $ProcessId (Get-HttpJson 'http://127.0.0.1:8765/v1/pool' 3)) -eq 'RUNNING') { return }
        Start-Sleep -Seconds 3
    }
    throw "PROVIDER_READY_TIMEOUT provider=$([string]$Spec.id) timeout_s=$timeout"
}

function Wait-ProviderPortClosed($Spec) {
    $port = Get-ProviderHealthPort $Spec
    if ($port -le 0) { return }
    $hostName = Get-ProviderHealthHost $Spec
    $deadline = (Get-Date).AddSeconds(30)
    while ((Get-Date) -lt $deadline) {
        if (-not (Test-Tcp $hostName $port 300)) { return }
        Start-Sleep -Milliseconds 500
    }
    throw "PORT_STILL_OPEN port=$port"
}

function Start-ProviderWorker($Spec,[hashtable]$Context) {
    # Provider-agnostic start entry point. Dispatch is by manifest worker.pid_key:
    # a provider that ships Start-<Pascal(pid_key)> owns its launch body (class A --
    # the only place its own asset paths may live) and is handed this row plus the
    # resolved launch context; if it also ships Wait-<Pascal(pid_key)> the hook is
    # called from here with the declared ready timeout, so no caller names it.
    # Providers with no hook are launched by the generic tail below. The worker
    # block itself is optional (qwen3's TTS row declares worker:null) and pid_key
    # is optional inside it, so both go through the StrictMode-safe read.
    $wk = Get-ProviderProp $Spec 'worker'
    $pidKey = [string](Get-ProviderProp $wk 'pid_key')
    if (-not $pidKey) { return }
    $hook = Format-ProviderHookName 'Start' $pidKey
    if (Get-Command -CommandType Function -Name $hook -ErrorAction SilentlyContinue) {
        $started = & $hook $Spec $Context
        $waitHook = Format-ProviderHookName 'Wait' $pidKey
        if ($started -and (Get-Command -CommandType Function -Name $waitHook -ErrorAction SilentlyContinue)) {
            & $waitHook $Spec ([int]$started) (Get-ProviderReadyTimeout $Spec)
        }
        return $started
    }
    # Generic external-worker path (only reached by providers with no bespoke hook).
    $plan = Expand-ProviderLaunch $Spec $Context
    if (@($plan.missing_placeholders).Count) { throw "PROVIDER_WORKER_PLACEHOLDER_MISSING provider=$pidKey missing=$($plan.missing_placeholders -join ',')" }
    $exe = [string](Get-ProviderProp $wk 'executable_suffix')
    if ($exe -notmatch '[\\/]') { throw "PROVIDER_WORKER_EXECUTABLE_UNDECLARED provider=$pidKey suffix=$exe (manifest must give a repo-relative path or the provider must ship a Start-<Pascal(pid_key)> hook)" }
    $exePath = Join-Path $script:AppRoot $exe
    $workdir = if ($Context -and $Context.ContainsKey('WORKDIR')) { [string]$Context['WORKDIR'] } else { $script:AppRoot }
    $started = Start-NativeProcess $pidKey $exePath $plan.worker_args $workdir $pidKey
    Wait-ProviderReady $Spec 0 $started
    return $started
}

function Get-ProviderWorkerOrphans($Spec) {
    # A python worker launched through the venv's python.exe re-execs into the
    # bundled runtime interpreter; if the launcher dies first that child is orphaned
    # and keeps holding the model in VRAM. Sweep for it with declared facts only:
    # worker.command_marker plus the port declared in health. A provider with no
    # command_marker (a native .exe worker) has no such child, so it is skipped.
    $marker = [string](Get-ProviderProp (Get-ProviderProp $Spec 'worker') 'command_marker')
    if (-not $marker) { return @() }
    $port = Get-ProviderHealthPort $Spec
    $portPattern = if ($port -gt 0) { "*--port $port*" } else { $null }
    return @(Get-CimInstance Win32_Process -ErrorAction SilentlyContinue | Where-Object {
        $_.ExecutablePath -ieq $script:S2SPythonRuntime -and
        $_.CommandLine -like "*$marker*" -and (-not $portPattern -or $_.CommandLine -like $portPattern)
    })
}

function Stop-ProviderWorker($Spec) {
    # Provider-agnostic teardown. Same pid_key dispatch as start; a provider that
    # ships its own Stop-<Pascal(pid_key)> hook runs unchanged, everything else is
    # torn down from declared fields alone: worker.{pid_key,command_marker,
    # executable_suffix} for the identity asserts and the orphan sweep, then the
    # health port for the closed-port wait. The worker block is optional per row
    # (qwen3 declares worker:null) -- the StrictMode-safe read keeps a row without
    # one from throwing before the "nothing to stop" early return.
    $wk = Get-ProviderProp $Spec 'worker'
    $pidKey = [string](Get-ProviderProp $wk 'pid_key')
    if (-not $pidKey) { return }
    $state = Read-ServicePids
    if ([int]$state.$pidKey -le 0) { return }
    $hook = Format-ProviderHookName 'Stop' $pidKey
    if (Get-Command -CommandType Function -Name $hook -ErrorAction SilentlyContinue) {
        & $hook $Spec $state
    } else {
        $trackPid = [int]$state.$pidKey
        $orphans = @(Get-ProviderWorkerOrphans $Spec)
        $treeIds = @(@($trackPid) + @($orphans | ForEach-Object ProcessId) | Sort-Object -Unique)
        Write-GpuMemorySnapshot ('before-' + $pidKey + '-process-exit') $treeIds
        # Expected interpreter: the declared worker executable when the manifest gives
        # a repo-relative path, else the executable recorded when it was started.
        $exe = ''
        $exeSuffix = [string](Get-ProviderProp $wk 'executable_suffix')
        if ($exeSuffix -and $exeSuffix -match '[\\/]') {
            $exe = Join-Path $script:AppRoot $exeSuffix
        } else {
            $owner = Get-ServiceOwner $state $pidKey
            if ($owner) { $exe = [string]$owner.executablePath }
        }
        $marker = [string](Get-ProviderProp $wk 'command_marker')
        Stop-NativeProcess $pidKey $exe $marker $orphans
        Write-GpuMemorySnapshot ('after-' + $pidKey + '-process-exit') $treeIds
    }
    Wait-ProviderPortClosed $Spec
}

function Read-ServicePids {
    # Non-provider keys keep their historical literals and semantics; the provider
    # worker keys (previously a hardcoded fish/higgs pair) now come from every
    # manifest's worker.pid_key. Backward compatible: an on-disk file missing a key
    # gains it as 0 instead of crashing.
    $state = $null
    if (Test-Path -LiteralPath $script:PidFile) {
        try { $state = Read-JsonFile $script:PidFile } catch { $state = $null }
    }
    if (-not $state) {
        $state = [pscustomobject]@{ s2s=0; avatar=0; ui=0; llmStartedByStart=$false; owners=[pscustomobject]@{}; llmOwnership=$null }
    }
    foreach ($key in (Get-WorkerPidKeys)) {
        if (-not $state.PSObject.Properties[$key]) { $state | Add-Member -NotePropertyName $key -NotePropertyValue 0 }
    }
    return $state
}

function Save-ServicePids($State) {
    Write-AtomicFile $script:PidFile ($State | ConvertTo-Json -Depth 5)
}

function Get-ProcessStartStamp([int]$ProcessId) {
    try { return (Get-Process -Id $ProcessId -ErrorAction Stop).StartTime.ToUniversalTime().ToString('o') }
    catch { return '' }
}

function Get-ServiceOwner($State,[string]$Name) {
    if ($State.PSObject.Properties['owners'] -and $State.owners -and $State.owners.PSObject.Properties[$Name]) {
        return $State.owners.$Name
    }
    return $null
}

function Set-ServiceOwner($State,[string]$Name,$Owner) {
    if (-not $State.PSObject.Properties['owners']) { $State | Add-Member -NotePropertyName owners -NotePropertyValue ([pscustomobject]@{}) }
    if ($State.owners.PSObject.Properties[$Name]) { $State.owners.$Name = $Owner }
    else { $State.owners | Add-Member -NotePropertyName $Name -NotePropertyValue $Owner }
}

function Assert-TrackedProcessIdentity($State,[string]$Name,$Process,[string]$ExpectedExecutable,[string]$CommandMarker='') {
    if (-not $Process) { return }
    if ($Process.ExecutablePath -ine $ExpectedExecutable) {
        throw "PID_OWNERSHIP_MISMATCH service=$Name pid=$($Process.ProcessId) exe=$($Process.ExecutablePath)"
    }
    if ($CommandMarker -and $Process.CommandLine -notlike "*$CommandMarker*") {
        throw "PID_COMMAND_MISMATCH service=$Name pid=$($Process.ProcessId)"
    }
    $owner = Get-ServiceOwner $State $Name
    if ($owner -and [int]$owner.pid -eq [int]$Process.ProcessId -and $owner.startTimeUtc) {
        $actual = Get-ProcessStartStamp ([int]$Process.ProcessId)
        $expectedTime=$owner.startTimeUtc
        if($expectedTime -isnot [datetime]) {
            $expectedTime=[datetime]::Parse([string]$expectedTime,[Globalization.CultureInfo]::InvariantCulture,[Globalization.DateTimeStyles]::RoundtripKind)
        }
        $expectedStamp=$expectedTime.ToUniversalTime().ToString('o')
        if ($actual -and $actual -ne $expectedStamp) {
            throw "PID_REUSED service=$Name pid=$($Process.ProcessId) expected_start=$expectedStamp actual_start=$actual"
        }
    }
}

function Get-DescendantProcesses([int]$ParentId,[int]$Depth=1) {
    $all=Get-CimInstance Win32_Process -ErrorAction SilentlyContinue
    foreach($child in @($all | Where-Object { [int]$_.ParentProcessId -eq $ParentId })) {
        [pscustomobject]@{ ProcessId=[int]$child.ProcessId; ParentProcessId=[int]$child.ParentProcessId; Depth=$Depth; ExecutablePath=[string]$child.ExecutablePath; CommandLine=[string]$child.CommandLine }
        Get-DescendantProcesses ([int]$child.ProcessId) ($Depth+1)
    }
}

function Write-GpuMemorySnapshot([string]$Stage,[int[]]$ProcessIds=@()) {
    $smi=Get-Command 'nvidia-smi.exe' -ErrorAction SilentlyContinue
    if(-not $smi) { Write-RuntimeLog 'tts-switch.log' "GPU_SNAPSHOT_UNAVAILABLE stage=$Stage"; return }
    $memory=(& $smi.Source --query-gpu=memory.used,memory.free --format=csv,noheader 2>$null) -join ';'
    $apps=@(& $smi.Source --query-compute-apps=pid,used_gpu_memory --format=csv,noheader 2>$null)
    $owned=@($apps | Where-Object {
        $line=[string]$_
        foreach($id in $ProcessIds) { if($line -match "^\s*$id\s*,") { return $true } }
        return $false
    }) -join ';'
    Write-RuntimeLog 'tts-switch.log' "GPU_SNAPSHOT stage=$Stage memory_used_free='$memory' tracked_tts_process_memory='$owned'"
}

function Set-ObjectValue($Object,[string]$Name,$Value) {
    if ($Object.PSObject.Properties[$Name]) { $Object.$Name = $Value }
    else { $Object | Add-Member -NotePropertyName $Name -NotePropertyValue $Value }
}

function Write-RuntimeLog([string]$Name,[string]$Message) {
    $line = "[$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss.fff')] $Message"
    Write-Host $line
    Add-Content -LiteralPath (Join-Path $script:LogRoot $Name) -Value $line -Encoding UTF8
}

function Get-HttpJson([string]$Uri,[int]$TimeoutSec=3) {
    return Invoke-RestMethod -Uri $Uri -TimeoutSec $TimeoutSec -Proxy $null
}

function Test-Http([string]$Uri) {
    try { Get-HttpJson $Uri 3 | Out-Null; return $true } catch { return $false }
}

function Test-Tcp([string]$HostName,[int]$Port,[int]$TimeoutMs=1000) {
    $client = New-Object System.Net.Sockets.TcpClient
    try {
        $task = $client.ConnectAsync($HostName,$Port)
        return $task.Wait($TimeoutMs) -and $client.Connected
    } catch { return $false } finally { $client.Close() }
}

function Wait-Tcp([string]$HostName,[int]$Port,[int]$TimeoutSec,[int]$ProcessId=0) {
    $deadline = (Get-Date).AddSeconds($TimeoutSec)
    while ((Get-Date) -lt $deadline) {
        if ($ProcessId -gt 0 -and -not (Get-Process -Id $ProcessId -ErrorAction SilentlyContinue)) {
            throw "PROCESS_EXITED_BEFORE_PORT_READY pid=$ProcessId port=$Port"
        }
        if (Test-Tcp $HostName $Port 500) { return }
        Start-Sleep -Milliseconds 500
    }
    throw "PORT_START_TIMEOUT host=$HostName port=$Port timeout_s=$TimeoutSec"
}

function Wait-S2S([string]$ExpectedBackend,[int]$TimeoutSec=900,[int]$ProcessId=0) {
    $deadline = (Get-Date).AddSeconds($TimeoutSec)
    while ((Get-Date) -lt $deadline) {
        if ($ProcessId -gt 0 -and -not (Get-Process -Id $ProcessId -ErrorAction SilentlyContinue)) {
            throw "S2S_PROCESS_EXITED pid=$ProcessId"
        }
        try {
            $pool = Get-HttpJson 'http://127.0.0.1:8765/v1/pool' 3
            $proc = Get-CimInstance Win32_Process -Filter "ProcessId=$ProcessId" -ErrorAction SilentlyContinue
            # The flag spelling comes from the TTS manifest (health.command_line_flag), not from
            # a literal here, so a provider that names its selector differently still matches.
            $flag = Get-ProviderSelectionFlag 'TTS'
            if ($pool.size -eq 1 -and $pool.in_use -eq 0 -and $proc -and $flag -and
                $proc.CommandLine -match "$([regex]::Escape($flag))(?:\s+|=)$([regex]::Escape($ExpectedBackend))(?:\s|$)") { return $pool }
        } catch {}
        Start-Sleep -Seconds 3
    }
    throw "S2S_START_TIMEOUT expected=$ExpectedBackend timeout_s=$TimeoutSec"
}

function Assert-PortFree([int]$Port) {
    $listeners = @(Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue)
    if ($listeners.Count -gt 0) {
        $owners = ($listeners | Select-Object -ExpandProperty OwningProcess -Unique) -join ','
        throw "PORT_IN_USE_NON_NATIVE port=$Port owners=$owners; legacy service was left untouched"
    }
}

function Quote-ProcessArgument([string]$Value) {
    if ($Value -match '[\s"]') { return '"' + $Value.Replace('"','\"') + '"' }
    return $Value
}

function Start-NativeProcess([string]$Name,[string]$Executable,[string[]]$Arguments,[string]$WorkingDirectory,[string]$LogStem) {
    if (-not (Test-Path -LiteralPath $Executable)) { throw "EXECUTABLE_MISSING path=$Executable" }
    $state = Read-ServicePids
    $current = [int]$state.$Name
    if ($current -gt 0 -and (Get-Process -Id $current -ErrorAction SilentlyContinue)) {
        $existing = Get-CimInstance Win32_Process -Filter "ProcessId=$current" -ErrorAction SilentlyContinue
        Assert-TrackedProcessIdentity $state $Name $existing $Executable
        return $current
    }

    $line = (($Arguments | ForEach-Object { Quote-ProcessArgument ([string]$_) }) -join ' ')
    $port=if($Name -eq 's2s'){8765}else{0}
    foreach($providerId in (Get-ProviderIds 'TTS' -EnabledOnly)){
        $spec=Get-ProviderSpec 'TTS' $providerId
        if((Get-ProviderProp (Get-ProviderProp $spec 'worker') 'pid_key') -eq $Name){$port=Get-ProviderHealthPort $spec}
    }
    $specDir=Join-Path $script:NativeRoot 'native_console_specs'
    New-Item -ItemType Directory -Force -Path $specDir|Out-Null
    $sid=[Security.Principal.WindowsIdentity]::GetCurrent().User.Value
    & icacls.exe $specDir /inheritance:r /grant:r "*${sid}:(OI)(CI)F"|Out-Null
    if($LASTEXITCODE -ne 0){throw 'NATIVE_SPEC_ACL_FAILED'}
    $launchSpec=Join-Path $specDir ($Name+'_'+[guid]::NewGuid().ToString('N')+'.json')
    $pidFile=$launchSpec+'.pid.json'
    Write-JsonTextUtf8 $launchSpec (@{executable=$Executable;argument_line=$line;workdir=$WorkingDirectory;pid_file=$pidFile;title="AI Girlfriend $Name $port http://127.0.0.1:$port"}|ConvertTo-Json -Depth 5)
    Start-Process -FilePath (Get-Command pwsh.exe -ErrorAction Stop).Source -WindowStyle Normal -ArgumentList @('-NoLogo','-NoProfile','-File',(Join-Path $script:AppRoot 'scripts\Native-Service-Console.ps1'),'-Spec',$launchSpec)|Out-Null
    $deadline=(Get-Date).AddSeconds(10)
    while(-not (Test-Path -LiteralPath $pidFile) -and (Get-Date) -lt $deadline){Start-Sleep -Milliseconds 100}
    if(-not (Test-Path -LiteralPath $pidFile)){throw 'VISIBLE_LAUNCH_PID_TIMEOUT'}
    $child=Read-JsonFile $pidFile
    $proc=Get-Process -Id ([int]$child.pid) -ErrorAction Stop
    Set-ObjectValue $state $Name $proc.Id
    $started = Get-CimInstance Win32_Process -Filter "ProcessId=$($proc.Id)" -ErrorAction SilentlyContinue
    # OWNERSHIP REPAIR 2026-09-25: the pid stored under 'avatar' is the VENV LAUNCHER
    # (Start-Process's return value), NOT the process holding :8010 -- that is the
    # re-exec'd listener it spawns, recorded in runtime\windows\avatar_runtime_state.json
    # as listener_pid. The role field makes that relationship explicit so no reader
    # repeats the "tracked pid vs socket owner" bug this project already paid for.
    Set-ServiceOwner $state $Name ([pscustomobject]@{
        pid=$proc.Id; startTimeUtc=(Get-ProcessStartStamp $proc.Id)
        executablePath=if($started){[string]$started.ExecutablePath}else{$Executable}
        commandLine=if($started){[string]$started.CommandLine}else{$line}
        createdBy='AI_GIRLFRIEND_WINDOWS_NATIVE'
        role='launcher'
        consoleSpec=$launchSpec
        listenerAuthority='runtime/windows/avatar_runtime_state.json:listener_pid'
    })
    Save-ServicePids $state
    return $proc.Id
}

function Stop-NativeProcess([string]$Name,[string]$ExpectedExecutable,[string]$CommandMarker='',[object[]]$ExtraOwned=@()) {
    if($Name -eq 'ui'){
        & (Join-Path $script:AppRoot 'scripts\Stop-UI7860.ps1')
        if(-not $?){throw 'UI_MANAGED_STOP_FAILED'}
        return
    }
    # $ExtraOwned lets the caller hand in processes this service provably owns but
    # that are no longer in the tracked pid's process tree (an orphaned worker
    # child); they are force-killed with the tree and counted in the final
    # still-running check. Callers that pass nothing behave exactly as before.
    $state = Read-ServicePids
    $owner=Get-ServiceOwner $state $Name
    if($owner -and $owner.PSObject.Properties['consoleSpec'] -and $owner.consoleSpec){
        if([string]$owner.consoleSpec -notlike "$($script:NativeRoot)\native_console_specs\*"){throw 'FOREIGN_CONSOLE_SPEC'}
        [IO.File]::WriteAllText(([string]$owner.consoleSpec+'.stop'),'planned lifecycle stop')
    }
    $pidValue = [int]$state.$Name
    if ($pidValue -le 0) { return }
    $proc = Get-CimInstance Win32_Process -Filter "ProcessId=$pidValue" -ErrorAction SilentlyContinue
    if ($proc) {
        Assert-TrackedProcessIdentity $state $Name $proc $ExpectedExecutable $CommandMarker
        $descendants=@(Get-DescendantProcesses $pidValue)
        $treeIds=@(@($descendants | ForEach-Object ProcessId)+@($ExtraOwned | ForEach-Object ProcessId) | Sort-Object -Unique)
        foreach($child in @($descendants | Sort-Object Depth -Descending)) {
            Stop-Process -Id $child.ProcessId -Force -ErrorAction SilentlyContinue
        }
        foreach($orphan in @($ExtraOwned)) {
            Stop-Process -Id ([int]$orphan.ProcessId) -Force -ErrorAction SilentlyContinue
        }
        Stop-Process -Id $pidValue -ErrorAction Stop
        try { Wait-Process -Id $pidValue -Timeout 20 -ErrorAction Stop } catch {
            Stop-Process -Id $pidValue -Force -ErrorAction SilentlyContinue
        }
        foreach($childId in $treeIds) { try { Wait-Process -Id ([int]$childId) -Timeout 15 -ErrorAction SilentlyContinue } catch {} }
        $stillRunning=@($treeIds | Where-Object { Get-Process -Id ([int]$_) -ErrorAction SilentlyContinue })
        if($stillRunning.Count -or (Get-Process -Id $pidValue -ErrorAction SilentlyContinue)) {
            throw "OWNED_PROCESS_TREE_STILL_RUNNING service=$Name pids=$($stillRunning -join ',')"
        }
    }
    Set-ObjectValue $state $Name 0
    Set-ServiceOwner $state $Name $null
    Save-ServicePids $state
}

function Stop-S2S {
    $state=Read-ServicePids
    $owner=Get-ServiceOwner $state 's2s'
    if($owner -and $owner.PSObject.Properties['consoleSpec'] -and $owner.consoleSpec){
        if([string]$owner.consoleSpec -notlike "$($script:NativeRoot)\native_console_specs\*"){throw 'FOREIGN_CONSOLE_SPEC'}
        [IO.File]::WriteAllText(([string]$owner.consoleSpec+'.stop'),'planned lifecycle stop')
    }
    $pidValue=[int]$state.s2s
    if($pidValue -le 0) { return }
    $tracked=Get-CimInstance Win32_Process -Filter "ProcessId=$pidValue" -ErrorAction SilentlyContinue
    Assert-TrackedProcessIdentity $state 's2s' $tracked $script:S2SExe
    $descendants=@(Get-DescendantProcesses $pidValue)
    $workers=@(Get-CimInstance Win32_Process -ErrorAction SilentlyContinue | Where-Object {
        $_.ExecutablePath -ieq $script:S2SPythonRuntime -and
        $_.CommandLine -like "*$($script:S2SExe)*" -and $_.CommandLine -like '*--ws_port 8765*'
    })
    $treeIds=@(@($descendants | ForEach-Object ProcessId)+@($workers | ForEach-Object ProcessId) | Sort-Object -Unique)
    Write-GpuMemorySnapshot 'before-unload' $treeIds
    foreach($worker in @($descendants | Sort-Object Depth -Descending)) { Stop-Process -Id $worker.ProcessId -Force -ErrorAction SilentlyContinue }
    foreach($worker in $workers) { Stop-Process -Id $worker.ProcessId -Force -ErrorAction SilentlyContinue }
    if($tracked) { Stop-Process -Id $pidValue -Force -ErrorAction SilentlyContinue }
    foreach($workerId in $treeIds) { try { Wait-Process -Id ([int]$workerId) -Timeout 15 -ErrorAction SilentlyContinue } catch {} }
    if($tracked) { Wait-Process -Id $pidValue -Timeout 15 -ErrorAction SilentlyContinue }
    $stillRunning = @($treeIds | Where-Object { Get-Process -Id ([int]$_) -ErrorAction SilentlyContinue })
    if ($tracked -and (Get-Process -Id $pidValue -ErrorAction SilentlyContinue)) { $stillRunning += $pidValue }
    if ($stillRunning.Count) { throw "S2S_PROCESS_TREE_STILL_RUNNING pids=$($stillRunning -join ',')" }
    Write-GpuMemorySnapshot 'after-s2s-process-exit' $treeIds
    Set-ObjectValue $state 's2s' 0
    Set-ServiceOwner $state 's2s' $null
    Save-ServicePids $state
}

# There is deliberately no per-provider Stop-<provider> function in this file. A
# worker that needs a bespoke teardown ships Stop-<Pascal(worker.pid_key)> in the
# lifecycle scope that owns it and is found by Format-ProviderHookName above; every
# other external worker is torn down by Stop-ProviderWorker from its declared
# fields alone, so adding a provider never means editing this file.
