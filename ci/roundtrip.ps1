<#
.SYNOPSIS
    One round of pondNT testing on a Windows box, without copy and paste.

.DESCRIPTION
    Everything stays in C:\temp\ci (key, known_hosts, downloads, results).
    1. Fetches the Crocodile's outbox list (SHA256SUMS) and every file that
       is new or changed, checking each hash.
    2. Runs guest-run.ps1 on the release zip (unless -SkipCi).
    3. Runs any probe_*.py in C:\temp\ci with the release's own Python.
    4. Uploads results.json and the probe results to the Crocodile's inbox.
    5. Prints one summary line.

.EXAMPLE
    powershell -NoProfile -ExecutionPolicy Bypass -File C:\temp\ci\roundtrip.ps1 -Remote user@crocodile.example -RemoteRoot /srv/pondnt-ci
#>
param(
    # account@host of the Crocodile that serves the outbox and inbox.
    [Parameter(Mandatory)][ValidatePattern('^[a-z_][a-z0-9_-]*@[A-Za-z0-9.-]+$')][string]$Remote,
    # Directory on the remote that holds outbox/ and inbox/.
    [Parameter(Mandatory)][ValidatePattern('^/[^\s]+$')][string]$RemoteRoot,
    [switch]$SkipCi)

$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
$base = 'C:\temp\ci'
$remote = $Remote
$outbox = $RemoteRoot.TrimEnd('/') + '/outbox'
$inbox = $RemoteRoot.TrimEnd('/') + '/inbox'
$o = @('-F', 'none', '-i', "$base\ssh\id_ed25519", '-o', 'IdentitiesOnly=yes', '-o', "UserKnownHostsFile=$base\ssh\known_hosts", '-q')
$env:TEMP = "$base\tmp"; $env:TMP = $env:TEMP
New-Item -ItemType Directory -Force $env:TEMP | Out-Null

function Fetch([string]$name) {
    & scp @o "${remote}:$outbox/$name" "$base\$name"
    if ($LASTEXITCODE -ne 0) { throw "scp of $name failed ($LASTEXITCODE)" }
}

# 1. what is in the outbox, and fetch what changed
Fetch 'SHA256SUMS'
$fetched = @()
foreach ($line in Get-Content "$base\SHA256SUMS") {
    if ($line -notmatch '^([0-9a-f]{64})\s+\*?(.+)$') { continue }
    $hash, $name = $Matches[1].ToUpper(), $Matches[2].Trim()
    $local = Join-Path $base $name
    if (-not (Test-Path $local) -or (Get-FileHash $local).Hash -ne $hash) {
        Fetch $name
        if ((Get-FileHash $local).Hash -ne $hash) { throw "hash mismatch after download: $name" }
        $fetched += $name
    }
}

# 2. the CI run
$zip = Get-ChildItem "$base\boring-pondnt-*-win-x64-tests.zip" | Sort-Object LastWriteTime | Select-Object -Last 1
$ciExit = $null
if (-not $SkipCi -and $zip) {
    & powershell -NoProfile -ExecutionPolicy Bypass -File "$base\guest-run.ps1" -Zip $zip.FullName -Work "$base\run"
    $ciExit = $LASTEXITCODE
}

# 3. probes, with the release's Python
$python = Get-ChildItem "$base\run\release\*\python\python.exe" -ErrorAction SilentlyContinue | Select-Object -First 1
$probes = @()
foreach ($probe in Get-ChildItem "$base\probe_*.py" -ErrorAction SilentlyContinue) {
    if (-not $python) { break }
    $out = Join-Path "$base\run" ($probe.BaseName + '.json')
    & $python.FullName $probe.FullName $out | Out-Null
    if (Test-Path $out) { $probes += $out }
}

# 4. results to the inbox
$uploads = @()
if (Test-Path "$base\run\results.json") { $uploads += "$base\run\results.json" }
$uploads += $probes
foreach ($file in $uploads) {
    & scp @o $file "${remote}:$inbox/"
    if ($LASTEXITCODE -ne 0) { throw "upload of $file failed ($LASTEXITCODE)" }
}

# 5. one line
$summary = ''
if (Test-Path "$base\run\results.json") {
    $r = Get-Content -Raw -Encoding UTF8 "$base\run\results.json" | ConvertFrom-Json
    $summary = "ok=$($r.ok) tests: $($r.tests.summary) services: $($r.services_tests.summary)"
}
Write-Output "roundtrip done: fetched [$($fetched -join ', ')], ci exit $ciExit, uploaded $($uploads.Count) file(s). $summary"
