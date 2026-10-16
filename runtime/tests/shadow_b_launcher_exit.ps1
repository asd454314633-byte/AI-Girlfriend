# Shadow validation B: launcher exits, listener must survive, ownership must stay explainable.
# Fault injection on the CURRENT generation only (gen 58): kill the launcher trampoline.
$ErrorActionPreference = 'Continue'
$statePath = ([IO.Path]::GetFullPath((Join-Path $PSScriptRoot '../../runtime/windows/avatar_runtime_state.json')))
$st = Get-Content -LiteralPath $statePath -Raw -Encoding UTF8 | ConvertFrom-Json
$ownerPid    = [int]$st.owner_pid
$launcherPid = [int]$st.launcher_pid
$listenerPid = [int]$st.listener_pid
Write-Output ("BASELINE gen={0} owner={1} launcher={2} listener={3} state={4}" -f $st.generation, $ownerPid, $launcherPid, $listenerPid, $st.state)

function Test-Pid([int]$Id) { return [bool](Get-Process -Id $Id -ErrorAction SilentlyContinue) }
function Test-Port([int]$P) {
    try { $r = Invoke-WebRequest -Uri ('http://127.0.0.1:' + $P + '/') -TimeoutSec 8 -UseBasicParsing; return $r.StatusCode } catch { return 'ERR' }
}

if (-not (Test-Pid $launcherPid)) { Write-Output 'B_ABORT launcher already dead'; exit 1 }
Write-Output ("PRE owner_alive={0} launcher_alive={1} listener_alive={2} http8010={3}" -f (Test-Pid $ownerPid), (Test-Pid $launcherPid), (Test-Pid $listenerPid), (Test-Port 8010))

# Inject: launcher dies (simulated crash of the venv trampoline)
Stop-Process -Id $launcherPid -Force -ErrorAction Continue
Start-Sleep -Seconds 4

Write-Output ("POST+4s owner_alive={0} launcher_alive={1} listener_alive={2} http8010={3}" -f (Test-Pid $ownerPid), (Test-Pid $launcherPid), (Test-Pid $listenerPid), (Test-Port 8010))
Start-Sleep -Seconds 8
Write-Output ("POST+12s owner_alive={0} launcher_alive={1} listener_alive={2} http8010={3}" -f (Test-Pid $ownerPid), (Test-Pid $launcherPid), (Test-Pid $listenerPid), (Test-Port 8010))

$st2 = Get-Content -LiteralPath $statePath -Raw -Encoding UTF8 | ConvertFrom-Json
Write-Output ("STATE_AFTER gen={0} state={1} owner={2} launcher={3} listener={4} exit_reason={5}" -f $st2.generation, $st2.state, $st2.owner_pid, $st2.launcher_pid, $st2.listener_pid, $st2.exit_reason)

# Ownership explainability: every recorded pid must be attributable
$verdict = 'PASS'
if (-not (Test-Pid $listenerPid)) { $verdict = 'FAIL listener died' }
if (-not (Test-Pid $ownerPid))    { $verdict = 'FAIL owner exited early' }
if ((Test-Port 8010) -ne 200)     { $verdict = 'FAIL port lost' }
Write-Output ('SHADOW_B_RESULT ' + $verdict)
