$root=Split-Path -Parent $PSScriptRoot
$cfg=Get-Content (Join-Path $root 'runtime\live\config.json') -Raw -Encoding UTF8 | ConvertFrom-Json
& curl.exe --max-time 5 --silent --show-error --fail --noproxy '*' "http://127.0.0.1:$($cfg.api_port)/status"
