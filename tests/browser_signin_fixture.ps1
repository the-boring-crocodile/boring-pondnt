param([string]$Helper, [string]$Fixture)
$ErrorActionPreference = 'Stop'
# browser-signin.ps1 in child PowerShells: Read-Host and the port check are replaced,
# Edge is a placeholder file and -NoLaunch keeps it from starting. Nothing listens, nothing opens.
function Check($condition, $message) { if (-not $condition) { throw "FAIL: $message" } }

$webConfig = Join-Path $Fixture 'web.config'
Set-Content -LiteralPath $webConfig -Value ('<?xml version="1.0"?><configuration><appSettings>' +
    '<add key="Infrastructure.HostUrl" value="https://aos.example/" /></appSettings></configuration>')
$edge = Join-Path $Fixture 'msedge.exe'
New-Item -ItemType File -Path $edge | Out-Null
$profiles = Join-Path $Fixture 'profiles'

function Run([string]$configRoot, [string]$readHost, [string]$listening, [string]$arguments) {
    $prelude = "function Read-Host { param(`$Prompt) $readHost }; " +
               "function Get-NetTCPConnection { $listening }; "
    $call = "& '$Helper' -ConfigRoot '$configRoot' -ProfilesRoot '$profiles' -WebConfig '$webConfig' " +
            "-Edge '$edge' -NoLaunch $arguments; exit `$LASTEXITCODE"
    $output = & powershell.exe -NoProfile -NonInteractive -ExecutionPolicy Bypass -Command ($prelude + $call) 2>&1 | Out-String
    return @{ code = $LASTEXITCODE; text = $output }
}
$noPrompt = "throw 'unexpected prompt: ' + `$Prompt"
$nothing = 'return $null'

# 1. No configuration yet: the address is suggested from web.config, Enter takes it and usmf.
$root1 = Join-Path $Fixture 'one'
$r = Run $root1 "''" $nothing '-Service d365web'
Check ($r.code -eq 0) "new d365web: $($r.text)"
$file = Join-Path $root1 'd365web\d365web.json'
$written = Get-Content -LiteralPath $file -Raw | ConvertFrom-Json
Check ($written.url -eq 'https://aos.example') "suggested url: $($written.url)"
Check ($written.company -eq 'usmf' -and $written.profile -eq 'd365web' -and $written.debug_port -eq 9333) 'd365web defaults'
Check ($written.headless -eq $true -and $written.step_timeout -eq 900) 'd365web keys'
Check ($r.text -match [regex]::Escape("$profiles\d365web")) 'profile folder shown'

# 2. An existing file is used and left untouched, even with other values given.
$before = (Get-FileHash -LiteralPath $file).Hash
$r = Run $root1 $noPrompt $nothing '-Service d365web -Url https://other.example -Company demf'
Check ($r.code -eq 0 -and $r.text -match 'left as it is') "existing d365web: $($r.text)"
Check ((Get-FileHash -LiteralPath $file).Hash -eq $before) 'existing file unchanged'
Check ($r.text -match 'aos\.example/\?cmp=usmf') 'existing values used'

# 3. d365whsapp: form mode, its own profile and port; the path is dropped, the company lower-cased.
$root3 = Join-Path $Fixture 'three'
$r = Run $root3 $noPrompt $nothing '-Service d365whsapp -Url https://aos.example/some/path -Company DEMF'
Check ($r.code -eq 0) "new d365whsapp: $($r.text)"
$whs = Get-Content -LiteralPath (Join-Path $root3 'd365whsapp\d365whsapp.json') -Raw | ConvertFrom-Json
Check ($whs.mode -eq 'form') 'form mode'
Check ($whs.form.url -eq 'https://aos.example' -and $whs.form.company -eq 'demf') "form block: $($whs.form | ConvertTo-Json -Compress)"
Check ($whs.form.profile -eq 'd365whsapp-form' -and $whs.form.debug_port -eq 9334) 'whsapp defaults'

# 4. App mode signs in with a secret: refused, file untouched.
$root4 = Join-Path $Fixture 'four'
New-Item -ItemType Directory -Force -Path (Join-Path $root4 'd365whsapp') | Out-Null
$appFile = Join-Path $root4 'd365whsapp\d365whsapp.json'
Set-Content -LiteralPath $appFile -Value '{"aos_url": "https://aos.example", "tenant": "t", "client_id": "c"}'
$r = Run $root4 $noPrompt $nothing '-Service d365whsapp'
Check ($r.code -eq 1 -and $r.text -match 'app mode') "app mode refused: $($r.text)"

# 5. Something on the port (the service's browser): refused before Edge.
$r = Run $root1 $noPrompt "return [pscustomobject]@{ LocalPort = 9333 }" '-Service d365web'
Check ($r.code -eq 1 -and $r.text -match 'listens on port 9333') "port busy refused: $($r.text)"

# 6. Not https: refused, nothing written.
$root6 = Join-Path $Fixture 'six'
$r = Run $root6 $noPrompt $nothing '-Service d365web -Url http://aos.example -Company usmf'
Check ($r.code -eq 1 -and $r.text -match 'https://') "http refused: $($r.text)"
Check (-not (Test-Path -LiteralPath (Join-Path $root6 'd365web\d365web.json'))) 'nothing written for http'

Write-Output 'PASS: browser-signin cases with placeholder Edge only.'
