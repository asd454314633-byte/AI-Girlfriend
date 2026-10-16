# Shadow validation E: 10 consecutive avatar A<->B switches, no TASK_CHILD_DIED allowed.
# A = wav2lip256_avatar1 (default), B = wav2lip256_zero01 (standby)
# Read-only wrt service code; drives the production switch script only.
$ErrorActionPreference = 'Continue'
$switch = ([IO.Path]::GetFullPath((Join-Path $PSScriptRoot '../../runtime/windows/Switch-Girlfriend-Avatar.ps1')))
$targets = @('wav2lip256_avatar1', 'wav2lip256_zero01')
$fail = 0
$summary = @()
for ($i = 1; $i -le 10; $i++) {
    $t = $targets[($i - 1) % 2]
    $t0 = Get-Date
    $out = & powershell.exe -NoLogo -NoProfile -NonInteractive -ExecutionPolicy Bypass -File $switch -Target $t 2>&1
    $dt = [int]((Get-Date) - $t0).TotalMilliseconds
    $text = ($out | Out-String)
    $ok = $text -match 'AVATAR_SWITCH_OK'
    $died = $text -match 'TASK_CHILD_DIED'
    $gen = ''
    if ($text -match 'LAUNCH_OK gen=(\d+)') { $gen = $Matches[1] }
    if (-not $ok -or $died) { $fail++ }
    $summary += ('{0:d2} target={1} gen={2} ms={3} SWITCH_OK={4} CHILD_DIED={5}' -f $i, $t, $gen, $dt, $ok, $died)
    Start-Sleep -Seconds 2
}
$summary | ForEach-Object { Write-Output $_ }
Write-Output ('SHADOW_E_RESULT switches=10 fail=' + $fail)
