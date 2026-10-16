param([Parameter(Mandatory=$true)][string]$Python,
      [Parameter(Mandatory=$true)][string]$Runner,
      [Parameter(Mandatory=$true)][string]$Spec)
$ErrorActionPreference='Stop'
$env:PYTHONUNBUFFERED='1'
try {
    & $Python -u $Runner run --spec $Spec
    if ($LASTEXITCODE -ne 0) { throw "UI_MANAGER_EXIT_$LASTEXITCODE" }
} catch {
    Write-Host $_ -ForegroundColor Red
    Read-Host '启动失败，窗口保留。按回车关闭' | Out-Null
    exit 1
}
