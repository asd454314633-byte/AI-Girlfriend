# Avatar launch body -- the ONE place that knows how to bring the digital-human
# service up.
#
# It is the extract of the former `Start-AvatarNative` body in
# scripts/Start-AI-Girlfriend.ps1:11-54, copied sentence for sentence so the cold
# start and the switch share a single launch body: CUDA bin on PATH,
# PYTHONUNBUFFERED, launch.env injection and restore, the working directory that
# decides where ./data/avatars lands, service_pids bookkeeping and Wait-Tcp.
# Changing the CUDA version or a launch argument is an edit here, not two edits.
#
# The only thing added on top of the copied body is one line:
#     if ($AvatarId) { $ctx['AVATAR_ID'] = $AvatarId }
# i.e. an explicit target overrides the resolved placeholder value.
#
# Pure function library: no param block, no side effects at load time. Callers
# dot-source Runtime-Common.ps1 first (same process, same scope), so
# $script:RuntimeRoot / $script:AvatarPython / $script:CudaBin and every provider
# primitive below are already published.

function Get-AvatarRegistryPath {
    return (Join-Path $script:NativeRoot 'avatars.json')
}

function Get-AvatarPointerPath {
    # The "who did the user last pick" pointer is its own one-line ASCII file,
    # same shape and same single-writer rule as active_tts_backend.txt. Only the
    # switch script writes it; this library only reads it.
    return (Join-Path $script:NativeRoot 'active_avatar.txt')
}

function Get-AvatarRegistry {
    # $null when the file does not exist or does not parse. Every caller treats
    # $null as "the registry cannot answer", which is exactly the signal that
    # sends the launch body back to the manifest placeholder.
    $path = Get-AvatarRegistryPath
    if (-not (Test-Path -LiteralPath $path)) { return $null }
    try { return (Read-JsonFile $path) } catch { return $null }
}

function Get-AvatarEntryById([string]$Id) {
    if ([string]::IsNullOrWhiteSpace($Id)) { return $null }
    $reg = Get-AvatarRegistry
    if (-not $reg -or -not $reg.avatars) { return $null }
    foreach ($entry in @($reg.avatars)) {
        if ($entry -and ([string]$entry.id) -eq $Id) { return $entry }
    }
    return $null
}

function Get-RegisteredActiveAvatarId {
    # Priority 1 of the launch body: what the user last picked, provided that
    # entry is actually usable. Anything else -> empty string, and the caller
    # falls back to the manifest placeholder without complaining.
    # The pointer is its own one-line ASCII file (same shape and same
    # single-writer rule as active_tts_backend.txt); only the switch script
    # writes it, this library only reads it.
    $pointerPath = Get-AvatarPointerPath
    if (-not (Test-Path -LiteralPath $pointerPath)) { return '' }
    $active = ''
    try { $active = (Read-JsonText $pointerPath).Trim() } catch { return '' }
    if (-not $active) { return '' }
    $entry = Get-AvatarEntryById $active
    if (-not $entry) { return '' }
    $state = [string]$entry.state
    if ($state -and $state -ne 'ready') { return '' }
    return $active
}

function Get-AvatarIdFlag($Spec) {
    # The command-line switch that selects a digital human, read out of
    # launch.cli_args: the item BEFORE the '{AVATAR_ID}' placeholder. Nothing
    # here spells the switch, so a manifest rename needs no edit in this file.
    $args = @($Spec.launch.cli_args)
    for ($i = 0; $i -lt $args.Count; $i++) {
        if ([string]$args[$i] -eq '{AVATAR_ID}' -and $i -gt 0) { return [string]$args[$i - 1] }
    }
    return ''
}

function Get-AvatarPortFlag($Spec) {
    # Same trick for the listen port: the item BEFORE the declared port value.
    $port = [string](Get-ProviderHealthPort $Spec)
    $args = @($Spec.launch.cli_args)
    for ($i = 0; $i -lt $args.Count; $i++) {
        if ([string]$args[$i] -eq $port -and $i -gt 0) { return [string]$args[$i - 1] }
    }
    return ''
}

function Get-AvatarProviderSpec {
    $providerId = Get-ProviderDefault 'AVATAR'
    if (-not $providerId) { $providerId = (@(Get-ProviderIds 'AVATAR' -EnabledOnly) | Select-Object -First 1) }
    $spec = Get-ProviderSpec 'AVATAR' $providerId
    if (-not $spec) { throw "AVATAR_PROVIDER_UNDECLARED id=$providerId" }
    return $spec
}

function Start-GirlfriendAvatarNative {
    param([string]$AvatarId)

    # The avatar command line used to be assembled here word by word, which meant adding a
    # second digital-human provider forced an edit in this lifecycle script. It now comes from
    # the AVATAR manifest row (launch.cli_args + launch.placeholders), the same way the TTS and
    # STT fragments are produced, and an unresolved placeholder fails loudly instead of being
    # hand-filled.
    $providerId = Get-ProviderDefault 'AVATAR'
    if (-not $providerId) { $providerId = (@(Get-ProviderIds 'AVATAR' -EnabledOnly) | Select-Object -First 1) }
    $spec = Get-ProviderSpec 'AVATAR' $providerId
    if (-not $spec) { throw "AVATAR_PROVIDER_UNDECLARED id=$providerId" }
    $ctx = Get-ProviderLaunchContext $spec

    # The ONE addition: an explicit target wins over whatever the manifest
    # placeholder (or the registry) resolved to. With no target the resolution
    # order is 1) avatars.json active_avatar when that entry is ready,
    # 2) the manifest placeholder already sitting in $ctx -- and 1 failing is
    # silent, so a cold start behaves byte for byte as it did before this file
    # existed. The fallback is still recorded, so "it restarted with someone
    # else" never becomes an unexplainable ghost.
    $resolvedId = [string]$AvatarId
    if (-not $resolvedId) {
        $resolvedId = Get-RegisteredActiveAvatarId
        if (-not $resolvedId) {
            $resolvedId = [string]$ctx['AVATAR_ID']
            Write-RuntimeLog 'avatar-launch.log' "AVATAR_COLD_START_FALLBACK using=$resolvedId reason=registry_unavailable_or_empty"
        }
    }
    if ($resolvedId) { $ctx['AVATAR_ID'] = $resolvedId }

    $plan = Expand-ProviderLaunch $spec $ctx
    if (@($plan.missing_placeholders).Count) {
        throw "AVATAR_PLACEHOLDER_UNRESOLVED provider=$providerId missing=$(($plan.missing_placeholders | Sort-Object -Unique) -join ',')"
    }
    $port = [int]$spec.worker.port
    if ($port -le 0) { throw "AVATAR_PORT_UNDECLARED provider=$providerId" }
    Assert-PortFree $port
    $app = Join-Path $script:RuntimeRoot 'avatar\app'
    $args = @($plan.args)
    $old = $env:PYTHONUNBUFFERED
    $oldPath = $env:PATH
    $env:PYTHONUNBUFFERED = '1'
    $env:PATH = "$($script:CudaBin);$oldPath"
    # launch.env is read off the same manifest row as the CLI args (see
    # Expand-ProviderLaunch), so a worker that decides behaviour from an import-time
    # environment switch stays replayable. Every name is handed back to whatever this
    # process had before, including 'was never set'.
    $envRestore = @{}
    foreach ($name in @($plan.env.Keys)) {
        if ($null -eq $name) { continue }
        $key = [string]$name
        $envRestore[$key] = [Environment]::GetEnvironmentVariable($key, 'Process')
        [Environment]::SetEnvironmentVariable($key, [string]$plan.env[$key], 'Process')
    }
    try { $pidValue = Start-NativeProcess 'avatar' $script:AvatarPython $args $app "avatar-$port" }
    finally {
        [Environment]::SetEnvironmentVariable('PYTHONUNBUFFERED', $old, 'Process')
        [Environment]::SetEnvironmentVariable('PATH', $oldPath, 'Process')
        foreach ($key in @($envRestore.Keys)) {
            [Environment]::SetEnvironmentVariable($key, $envRestore[$key], 'Process')
        }
    }
    Wait-Tcp '127.0.0.1' $port 240 $pidValue
    Write-RuntimeLog 'avatar-launch.log' "AVATAR_START_OK id=$resolvedId pid=$pidValue"
    return $pidValue
}
