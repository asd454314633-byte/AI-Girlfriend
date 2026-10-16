# Optional OS speech synthesis fixture, never a real person's identity recording.
param([string]$Text='This is a synthetic voice reference for local testing.')
$ErrorActionPreference='Stop'
$root=Split-Path -Parent $PSScriptRoot
$dir=Join-Path $root 'models\native\qwen3'
New-Item -ItemType Directory -Force -Path $dir | Out-Null
$wav=Join-Path $dir 'ref.wav';$txt=Join-Path $dir 'ref.txt'
if((Test-Path -LiteralPath $wav) -or (Test-Path -LiteralPath $txt)){throw 'REFERENCE_EXISTS_NO_OVERWRITE'}
Add-Type -AssemblyName System.Speech
$synth=[System.Speech.Synthesis.SpeechSynthesizer]::new()
try{$synth.SetOutputToWaveFile($wav);$synth.Speak($Text)}finally{$synth.Dispose()}
[IO.File]::WriteAllText($txt,$Text,[Text.UTF8Encoding]::new($false))
Write-Host 'Generated an OS-synthetic fixture, not the private default voice. Use your own authorized reference for real deployment.'
