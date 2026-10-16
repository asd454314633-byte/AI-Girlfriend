param([Parameter(Mandatory=$true)][string]$Spec)
$ErrorActionPreference='Stop'
$s=Get-Content -LiteralPath $Spec -Raw -Encoding UTF8 | ConvertFrom-Json -DateKind String
$Host.UI.RawUI.WindowTitle=[string]$s.title
$env:PYTHONUNBUFFERED='1'
Set-Location $s.workdir
Write-Host $s.title
Write-Host 'stdout/stderr and inference progress stream directly to this console.'
try{
    $proc=Start-Process -FilePath $s.executable -ArgumentList $s.argument_line -NoNewWindow -PassThru
    $temp=$s.pid_file+'.tmp'
    [IO.File]::WriteAllText($temp,(@{pid=$proc.Id;start_utc=$proc.StartTime.ToUniversalTime().ToString('o')}|ConvertTo-Json),[Text.UTF8Encoding]::new($false))
    Move-Item -LiteralPath $temp -Destination $s.pid_file -Force
    $proc.WaitForExit()
    if($proc.ExitCode -ne 0 -and -not (Test-Path -LiteralPath ($Spec+'.stop'))){throw "SERVICE_EXIT_$($proc.ExitCode)"}
}catch{Write-Host $_ -ForegroundColor Red;Read-Host '失败窗口保留，按回车关闭'|Out-Null;exit 1}
