$ErrorActionPreference='Stop'
& curl.exe --max-time 5 --silent --show-error --fail --noproxy '*' -X POST 'http://127.0.0.1:8781/replay'
if($LASTEXITCODE -ne 0){throw 'REPLAY_FAILED'}
