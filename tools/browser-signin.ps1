<#
.SYNOPSIS
Signs in the Edge profile that d365web (or d365whsapp in form mode) drives,
and writes the service's configuration file when there is none yet.

.DESCRIPTION
Run it as yourself on the box's desktop (double-click browser-signin.cmd), not
over SSH: the sign-in needs a person at a browser. It

  1. reads the service's configuration file if there is one and uses its
     address, company, profile and port; otherwise it suggests the AOS
     address from the box's web.config (Infrastructure.HostUrl), asks for
     the address and company, and writes the file;
  2. refuses while something listens on the profile's debugging port (the
     service's own browser: close it with web_close first);
  3. opens Edge with that profile on the D365 address and waits until you
     close it.

It never overwrites an existing configuration file, never starts or stops a
service and never touches app mode (d365whsapp with a client secret).

.EXAMPLE
browser-signin.cmd
browser-signin.cmd d365whsapp
powershell -ExecutionPolicy Bypass -File .\browser-signin.ps1 -Service d365web -Url https://aos.example -Company usmf
#>
param(
    [ValidateSet('', 'd365web', 'd365whsapp')][string]$Service = '',
    [string]$Url = '',
    [string]$Company = '',
    [string]$ConfigRoot = (Join-Path $env:LOCALAPPDATA 'boring-pondnt\services'),
    [string]$ProfilesRoot = (Join-Path $env:LOCALAPPDATA 'boring-pondnt\d365web\profiles'),
    [string]$WebConfig = '',
    [string]$Edge = '',
    [switch]$NoLaunch
)
$ErrorActionPreference = 'Stop'

function Say([string]$text) { Write-Host $text }
function Stop-With([string]$text) { Write-Host ''; Write-Host "stopped: $text"; exit 1 }

$defaults = @{
    d365web    = @{ profile = 'd365web'; port = 9333 }
    d365whsapp = @{ profile = 'd365whsapp-form'; port = 9334 }
}

if (-not $Service) {
    $answer = (Read-Host 'Which service: d365web or d365whsapp? [d365web]').Trim()
    $Service = if ($answer) { $answer } else { 'd365web' }
    if ($Service -notin 'd365web', 'd365whsapp') { Stop-With "unknown service '$Service': d365web or d365whsapp" }
}

$elevated = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole(
    [Security.Principal.WindowsBuiltInRole]::Administrator)
if ($elevated) {
    Say 'note: this window is elevated. The sign-in belongs to your own Edge profile; a normal window is enough.'
}

$configFile = Join-Path (Join-Path $ConfigRoot $Service) "$Service.json"
$settings = $null
if (Test-Path -LiteralPath $configFile) {
    try { $existing = Get-Content -LiteralPath $configFile -Raw | ConvertFrom-Json }
    catch { Stop-With "$configFile is not valid JSON: $($_.Exception.Message)" }
    if ($Service -eq 'd365whsapp') {
        if ($existing.mode -ne 'form') {
            Stop-With "$configFile is in app mode: it signs in with a client secret, not a browser (D365WHSAPP.md, Setup)."
        }
        $settings = $existing.form
    } else {
        $settings = $existing
    }
    if (-not $settings -or -not $settings.url) { Stop-With "$configFile names no url" }
    Say "Using $configFile (left as it is)."
    $url = [string]$settings.url
    $company = [string]$settings.company
    $profileName = if ($settings.profile) { [string]$settings.profile } else { $defaults[$Service].profile }
    $port = if ($null -ne $settings.debug_port) { [int]$settings.debug_port } else { $defaults[$Service].port }
    $profileDir = if ($settings.profiles_dir) { [string]$settings.profiles_dir } else { Join-Path $ProfilesRoot $profileName }
} else {
    # Suggest the AOS address the box itself names, when it can be read.
    $suggestion = ''
    if (-not $WebConfig) {
        $WebConfig = Get-PSDrive -PSProvider FileSystem -ErrorAction SilentlyContinue |
            ForEach-Object { Join-Path $_.Root 'AosService\webroot\web.config' } |
            Where-Object { Test-Path -LiteralPath $_ } | Select-Object -First 1
    }
    if ($WebConfig -and (Test-Path -LiteralPath $WebConfig)) {
        try {
            $hostUrl = ([xml](Get-Content -LiteralPath $WebConfig -Raw)).configuration.appSettings.add |
                Where-Object { $_.key -eq 'Infrastructure.HostUrl' } | Select-Object -First 1
            if ($hostUrl) { $suggestion = ([string]$hostUrl.value).TrimEnd('/') }
        } catch { $suggestion = '' }
    }
    $url = $Url
    if (-not $url) {
        $prompt = if ($suggestion) { "AOS address [$suggestion]" } else { 'AOS address (https://...)' }
        $url = (Read-Host $prompt).Trim()
        if (-not $url) { $url = $suggestion }
    }
    $company = $Company
    if (-not $company) {
        $company = (Read-Host 'Company [usmf]').Trim()
        if (-not $company) { $company = 'usmf' }
    }
    $profileName = $defaults[$Service].profile
    $port = $defaults[$Service].port
    $profileDir = Join-Path $ProfilesRoot $profileName
}

$url = $url.Trim()
if ($url -match '^https://[^/\s?#]+') { $url = $Matches[0] }  # the AOS only: any path is dropped
else { Stop-With "the AOS address must start with https:// (got '$url')" }
$company = $company.Trim().ToLowerInvariant()

if (-not (Test-Path -LiteralPath $configFile)) {
    $block = [ordered]@{ url = $url; company = $company; profile = $profileName; debug_port = $port; headless = $true }
    if ($Service -eq 'd365web') {
        $block.idle_minutes = 30
        $block.step_timeout = 900
        $content = $block
    } else {
        $content = [ordered]@{ mode = 'form'; form = $block }
    }
    New-Item -ItemType Directory -Force -Path (Split-Path -Parent $configFile) | Out-Null
    [IO.File]::WriteAllText($configFile, ($content | ConvertTo-Json -Depth 4), (New-Object Text.UTF8Encoding($false)))
    Say "Wrote $configFile."
}

# The service's browser holds the profile and its port: two Edges cannot share a profile.
$listening = Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue
if ($listening) {
    Stop-With ("something listens on port $port, most likely the service's own browser. " +
               "Close it first (web_close), then run this again.")
}

if (-not $Edge) {
    $Edge = @("${env:ProgramFiles(x86)}\Microsoft\Edge\Application\msedge.exe",
              "$env:ProgramFiles\Microsoft\Edge\Application\msedge.exe") |
        Where-Object { $_ -and (Test-Path -LiteralPath $_) } | Select-Object -First 1
}
if (-not $Edge) { Stop-With 'Microsoft Edge (msedge.exe) was not found under Program Files.' }

$target = if ($company) { "$url/?cmp=$company" } else { "$url/" }
$arguments = @("--user-data-dir=`"$profileDir`"", "--remote-debugging-port=$port",
               '--remote-debugging-address=127.0.0.1', '--no-first-run', '--no-default-browser-check', "`"$target`"")

Say ''
Say "Service:  $Service"
Say "Profile:  $profileDir"
Say "Address:  $target"
Say "Port:     $port (127.0.0.1)"
if ($NoLaunch) {
    Say "Edge:     `"$Edge`" $($arguments -join ' ')"
    Say 'Not started (-NoLaunch).'
    exit 0
}
Say ''
Say 'Edge opens now. Sign in with the account the tests should use, wait for the'
Say 'D365 dashboard, then close Edge. This window waits for it.'
$edgeProcess = Start-Process -FilePath $Edge -ArgumentList $arguments -PassThru
$edgeProcess.WaitForExit()
Say ''
Say 'Done: the profile is signed in.'
Say ("Next, if $Service is not set up yet: add it to your complete -Services list in pondNT setup " +
     '(INSTALL.md, "Update, and back"). When it is set up already, nothing else is needed.')
