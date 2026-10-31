<#
.SYNOPSIS
    Defender exclusions for a D365 F&O development box: report, apply, undo.

.DESCRIPTION
    Real-time scanning of every file Visual Studio, the X++ compiler, DB sync
    and SQL Server touch makes a dev box slow. This script excludes those
    processes and the D365 folders -- following the list of d365fo.tools'
    Add-D365WindowsDefenderRules, but narrower and checked:

    - detects the real paths (Visual Studio via vswhere, the AOSService drive,
      the SQL Server service binary) and only adds what exists;
    - three levels:
        default       Microsoft only: the PackagesLocalDirectory packages whose
                      models are all published by Microsoft Corporation, its
                      bin and the AOS webroot, Microsoft's tool and cache
                      folders, and only processes with a valid Microsoft
                      signature. ISV and customer packages stay scanned.
        -NoProcesses  as default, without process exclusions: nothing a tool
                      reads elsewhere goes unscanned.
        -D365-All     everything detected: the whole AOSService folder and all
                      tool processes (fastest; for disposable boxes).
    - leaves out machine-wide exclusions (C:\Windows\Microsoft.NET,
      C:\Windows\assembly) and extension exclusions (.xml, .md, ...);
    - reports by default; changes only with -Apply, recording what it added
      in a JSON file, so -Undo removes exactly that;
    - checks first whether Defender is tamper-protected or centrally managed
      (then exclusions may be refused or reverted -- ask IT).

    Run elevated. A security trade-off: routine on disposable boxes; on boxes
    that stay, the owner decides.

.EXAMPLE
    powershell -NoProfile -ExecutionPolicy Bypass -File devbox-defender.ps1
    powershell -NoProfile -ExecutionPolicy Bypass -File devbox-defender.ps1 -Apply
    powershell -NoProfile -ExecutionPolicy Bypass -File devbox-defender.ps1 -D365-All -Apply
    powershell -NoProfile -ExecutionPolicy Bypass -File devbox-defender.ps1 -Undo
#>
[CmdletBinding()]  # unknown parameters (the old -All) are refused, not ignored
param(
    [switch]$Apply,
    [switch]$Undo,
    [switch]${D365-All},
    [switch]$NoProcesses,
    # The receipt that -Undo reads. Default: outside the release folder, so it survives an update.
    [string]$Record = ''
)

$ErrorActionPreference = 'Stop'
# $PSScriptRoot can be empty while parameter defaults are evaluated (Windows PowerShell 5.1,
# seen on the Azure box 2026-10-26: 'Join-Path ... empty string'); resolve paths here instead.
$here = if ($PSScriptRoot) { $PSScriptRoot } else { Split-Path -Parent $MyInvocation.MyCommand.Path }
if (-not $Record) { $Record = Join-Path $env:ProgramData 'boring-pondnt\devbox-defender-record.json' }
$recordDir = Split-Path -Parent $Record; if (-not (Test-Path $recordDir)) { New-Item -ItemType Directory -Force $recordDir | Out-Null }

function Test-Admin {
    ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole(
        [Security.Principal.WindowsBuiltInRole]::Administrator)
}

. (Join-Path $here 'maintenance-state.ps1')

function Read-DefenderRecord([string]$Path) {
    $data = Read-MaintenanceRecord $Path 'pondnt-defender'
    $seen = @{}
    foreach ($entry in @($data.entries)) {
        if (@($entry.PSObject.Properties.Name).Count -ne 3 -or
            $entry.kind -notin @('path','process') -or $entry.value -isnot [string] -or -not $entry.value.Trim() -or
            $entry.state -notin @('adding','present','removing','removed')) { throw 'Invalid exclusion entry.' }
        $key = $entry.kind + ':' + $entry.value
        if ($seen.ContainsKey($key)) { throw 'Duplicate exclusion entry.' }
        $seen[$key] = $true
    }
    return $data
}

function Test-DefenderExclusion([string]$Kind, [string]$Value) {
    $pref = Get-MpPreference -ErrorAction Stop
    if ($Kind -eq 'path') { return @($pref.ExclusionPath) -contains $Value }
    return @($pref.ExclusionProcess) -contains $Value
}

function Invoke-DefenderChange([string]$Mode, [string]$Path, [string[]]$Paths=@(),
                               [string[]]$Processes=@()) {
    $lock = Open-MaintenanceLock $Path
    try {
        $data = Read-DefenderRecord $Path
        # Pending entries are durable evidence of an interrupted/uncertain call.
        # Never turn "present now" into a claim that this helper added it.
        $pending = @($data.entries | Where-Object { $_.state -in @('adding','removing') })
        if ($pending.Count) { throw 'Uncertain previous change: review the record and current exclusions before apply/undo.' }
        Write-MaintenanceRecord $Path $data  # establish writable recovery state before any effect
        if ($Mode -eq 'apply') {
            foreach ($kind in @('path','process')) {
                $values = if ($kind -eq 'path') { $Paths } else { $Processes }
                foreach ($value in @($values | Select-Object -Unique)) {
                    if (Test-DefenderExclusion $kind $value) { continue } # pre-existing: never claim it
                    $entry = @($data.entries | Where-Object { $_.kind -eq $kind -and $_.value -eq $value })
                    if ($entry.Count) { $entry = $entry[0]; $entry.state = 'adding' }
                    else {
                        $entry = [pscustomobject]@{kind=$kind; value=$value; state='adding'}
                        $data.entries = @($data.entries) + @($entry)
                    }
                    Write-MaintenanceRecord $Path $data
                    if ($kind -eq 'path') { Add-MpPreference -ExclusionPath $value -ErrorAction Stop }
                    else { Add-MpPreference -ExclusionProcess $value -ErrorAction Stop }
                    if (-not (Test-DefenderExclusion $kind $value)) { throw 'Defender did not confirm the requested exclusion.' }
                    $entry.state = 'present'
                    Write-MaintenanceRecord $Path $data
                }
            }
        } elseif ($Mode -eq 'undo') {
            foreach ($entry in @($data.entries | Where-Object { $_.state -eq 'present' })) {
                if (-not (Test-DefenderExclusion $entry.kind $entry.value)) {
                    $entry.state = 'removed'; Write-MaintenanceRecord $Path $data; continue
                }
                $entry.state = 'removing'
                Write-MaintenanceRecord $Path $data
                if ($entry.kind -eq 'path') { Remove-MpPreference -ExclusionPath $entry.value -ErrorAction Stop }
                else { Remove-MpPreference -ExclusionProcess $entry.value -ErrorAction Stop }
                if (Test-DefenderExclusion $entry.kind $entry.value) { throw 'Defender still reports the exclusion after removal.' }
                $entry.state = 'removed'
                Write-MaintenanceRecord $Path $data
            }
        } else { throw 'Mode must be apply or undo.' }
    } finally { $lock.Dispose() }
}

if ($MyInvocation.InvocationName -eq '.') { return } # fixtures load owners, never machine planning

if (($Apply -or $Undo) -and -not (Test-Admin)) { throw 'Run elevated (as administrator) to change exclusions.' }
if ($Apply -and $Undo) { throw 'Either -Apply or -Undo, not both.' }
if (${D365-All} -and $NoProcesses) { throw 'Either -D365-All or -NoProcesses, not both.' }
$level = if (${D365-All}) { 'd365-all' } elseif ($NoProcesses) { 'microsoft-only, no processes' } else { 'microsoft-only' }

# --- undo: remove exactly what an earlier -Apply recorded ---------------------
if ($Undo) {
    if (-not (Test-Path $Record)) { throw "No record at $Record -- nothing this script added." }
    Invoke-DefenderChange 'undo' $Record
    'Recorded exclusions removed; unrelated exclusions preserved.'
    return
}

# --- state of Defender --------------------------------------------------------
try { $status = Get-MpComputerStatus; $pref = Get-MpPreference }
catch { "Microsoft Defender is not available on this machine ($($_.Exception.Message.Trim())): nothing to exclude."; return }
"Defender: real-time protection $($status.RealTimeProtectionEnabled), mode $($status.AMRunningMode), " +
"tamper protection $($status.IsTamperProtected)"
if ($status.IsTamperProtected) {
    'WARNING: tamper protection is on; on a managed device exclusions may be refused or reverted. Ask IT.'
}

# --- what exists on this box ---------------------------------------------------
$paths = New-Object System.Collections.Generic.List[string]
$processes = New-Object System.Collections.Generic.List[string]
function Add-IfExists($list, [string]$path) { if ($path -and (Test-Path -LiteralPath $path)) { $list.Add($path) } }

function Test-MicrosoftPackage([string]$package) {
    # All of the package's model descriptors name Microsoft Corporation as publisher.
    $descriptors = @(Get-ChildItem (Join-Path $package 'Descriptor') -Filter *.xml -ErrorAction SilentlyContinue)
    if ($descriptors.Count -eq 0) { return $false }
    foreach ($d in $descriptors) {
        $publisher = ([xml](Get-Content -Raw -LiteralPath $d.FullName)).AxModelInfo.Publisher
        if ($publisher -ne 'Microsoft Corporation') { return $false }
    }
    return $true
}

function Test-MicrosoftSigned([string]$file) {
    $sig = Get-AuthenticodeSignature -LiteralPath $file
    return $sig.Status -eq 'Valid' -and $sig.SignerCertificate.Subject -match 'O=Microsoft Corporation'
}

# D365: the AOSService folder (PackagesLocalDirectory, webroot) on whichever drive holds it
$aos = @('K:', 'J:', 'C:', 'F:') | ForEach-Object { "$_\AOSService" } | Where-Object { Test-Path $_ } | Select-Object -First 1
$skippedPackages = @()
$unsigned = @()
if ($aos) {
    $pld = Join-Path $aos 'PackagesLocalDirectory'
    if (${D365-All}) {
        Add-IfExists $paths $aos
    } else {
        Add-IfExists $paths (Join-Path $pld 'bin')
        Add-IfExists $paths (Join-Path $aos 'webroot')
        foreach ($package in Get-ChildItem $pld -Directory -ErrorAction SilentlyContinue) {
            if ($package.Name -eq 'bin') { continue }
            if (Test-MicrosoftPackage $package.FullName) { $paths.Add($package.FullName) }
            else { $skippedPackages += $package.Name }
        }
    }
    $bin = Join-Path $aos 'PackagesLocalDirectory\bin'
    foreach ($exe in 'xppc.exe', 'xppbp.exe', 'xppcAgent.exe', 'LabelC.exe', 'SyncEngine.exe', 'xpppfagen.exe') {
        Add-IfExists $processes (Join-Path $bin $exe)
    }
    foreach ($exe in 'Batch.exe', 'xppc.exe', 'LabelC.exe') { Add-IfExists $processes (Join-Path $aos "webroot\bin\$exe") }
}

# Visual Studio: every installed instance, its folder and its build tools
$vswhere = "${env:ProgramFiles(x86)}\Microsoft Visual Studio\Installer\vswhere.exe"
if (Test-Path $vswhere) {
    foreach ($vs in (& $vswhere -all -prerelease -property installationPath)) {
        Add-IfExists $paths $vs
        Add-IfExists $processes (Join-Path $vs 'Common7\IDE\devenv.exe')
        Add-IfExists $processes (Join-Path $vs 'MSBuild\Current\Bin\MSBuild.exe')
        Add-IfExists $processes (Join-Path $vs 'MSBuild\Current\Bin\amd64\MSBuild.exe')
        Add-IfExists $processes (Join-Path $vs 'Common7\IDE\Extensions\TestPlatform\testhost.exe')
        Add-IfExists $processes (Join-Path $vs 'Common7\IDE\CommonExtensions\Microsoft\TestWindow\vstest.console.exe')
    }
}
Add-IfExists $paths "$env:LOCALAPPDATA\Microsoft\VisualStudio"
Add-IfExists $paths "$env:APPDATA\Microsoft\VisualStudio"
Add-IfExists $paths "$env:ProgramData\Microsoft\VisualStudio\Packages"
if (${D365-All}) { Add-IfExists $paths "$env:USERPROFILE\.nuget\packages" }  # third-party packages: only with -D365-All

# SQL Server: the binary the service actually runs
foreach ($svc in Get-CimInstance Win32_Service -Filter "Name LIKE 'MSSQL%'" -ErrorAction SilentlyContinue) {
    if ($svc.PathName -match '^"?([^"]+sqlservr\.exe)') { Add-IfExists $processes $Matches[1] }
}

# IIS, IIS Express, Git
Add-IfExists $processes "$env:WINDIR\System32\inetsrv\w3wp.exe"
Add-IfExists $processes "$env:ProgramFiles\IIS Express\iisexpress.exe"
Add-IfExists $processes "$env:ProgramFiles\Git\cmd\git.exe"

if ($NoProcesses) { $processes.Clear() }
elseif (-not ${D365-All}) {
    $unsigned = @($processes | Where-Object { -not (Test-MicrosoftSigned $_) })
    foreach ($u in $unsigned) { [void]$processes.Remove($u) }
}
$newPaths = @($paths | Where-Object { @($pref.ExclusionPath) -notcontains $_ } | Select-Object -Unique)
$newProcesses = @($processes | Where-Object { @($pref.ExclusionProcess) -notcontains $_ } | Select-Object -Unique)
"Level: $level"
if ($skippedPackages.Count) { "Packages left scanned (not all models by Microsoft): $($skippedPackages -join ', ')" }
if ($unsigned -and $unsigned.Count) { "Processes left scanned (no valid Microsoft signature): $($unsigned -join ', ')" }

"Already excluded: $(@($pref.ExclusionPath).Count) paths, $(@($pref.ExclusionProcess).Count) processes."
"Would add $($newPaths.Count) paths:";     $newPaths | ForEach-Object { "  path    $_" }
"Would add $($newProcesses.Count) processes:"; $newProcesses | ForEach-Object { "  process $_" }
'Not added on purpose: C:\Windows\Microsoft.NET, C:\Windows\assembly, extension exclusions (.xml, .md, ...).'

if (-not $Apply) { 'Report only. Run with -Apply to add these, -Undo to remove what -Apply added.'; return }

Invoke-DefenderChange 'apply' $Record -Paths $newPaths -Processes $newProcesses
"Requested exclusions verified. Recovery record: $Record"
