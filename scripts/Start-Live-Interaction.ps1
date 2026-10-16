param([switch]$Enable)
$ErrorActionPreference='Stop'
& (Join-Path $PSScriptRoot 'Manage-Project-Service.ps1') -Service live -Action start
if(-not $?){throw 'LIVE_MANAGED_START_FAILED'}
if($Enable){
    $handler=[Net.Http.HttpClientHandler]::new();$handler.UseProxy=$false
    $client=[Net.Http.HttpClient]::new($handler)
    try{
        $content=[Net.Http.StringContent]::new('{"enabled":true}',[Text.Encoding]::UTF8,'application/json')
        $client.PostAsync('http://127.0.0.1:8781/control',$content).GetAwaiter().GetResult().EnsureSuccessStatusCode() | Out-Null
    }finally{$client.Dispose();$handler.Dispose()}
}