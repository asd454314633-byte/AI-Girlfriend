$errs = $null
$null = [System.Management.Automation.Language.Parser]::ParseFile(([IO.Path]::GetFullPath((Join-Path $PSScriptRoot '../../runtime/windows/Avatar-Supervisor.ps1'))), [ref]$null, [ref]$errs)
Write-Output ("pwsh7 parse errors: " + $errs.Count)
$errs | ForEach-Object { Write-Output ("  line " + $_.Extent.StartLineNumber + ": " + $_.Message) }
