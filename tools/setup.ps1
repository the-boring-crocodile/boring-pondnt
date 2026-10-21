<#
.SYNOPSIS
    Set up pondNT on this D365 dev box for Copilot in Visual Studio: look first, then change on "yes".

.DESCRIPTION
    Run from the unpacked release folder (Windows PowerShell 5.1 or 7), as Administrator
    (the -WhatIf preview runs without):

        .\setup.ps1              look, show the plan, ask before each change
        .\setup.ps1 -WhatIf      look and show the plan only; changes nothing
        .\setup.ps1 -Yes         apply the plan without asking (never touches Defender without -AddDefenderExclusion)
        .\setup.ps1 -Uninstall   remove pondNT's entries from .mcp.json; list what else to remove

    What it may change, each only after you agree:
      - %USERPROFILE%\.mcp.json: the entries of the servers below (HTTP with a key), keeps everything else; a backup
        is written next to it first.
      - the source index: builds it if none exists (10-35 minutes with the Defender exclusion,
        depending on the disk; many times longer without it).
      - the general services (Sourcecache, Knowledge, Canvas) on this machine: HTTP on 127.0.0.1 with
        an access key each, started at boot by scheduled tasks under \pondNT\,
        their entries in .mcp.json (the key is in the entry). -Services picks, -SkipServices skips.
      - the core and the D365 server the same way, except that their tasks run as you, at
        logon, elevated, without a window: Copilot's commands run in them, as you.
      - Defender: when it scans pondNT's python, setup ASKS (default no) whether to exclude this release's
        python.exe and pythonw.exe; -AddDefenderExclusion plans it. -Yes alone never does it. That is
        dangerous -- read INSTALL.md, step 2. -Uninstall offers to remove those exclusions again.
    It never installs d365fo.tools, never changes Windows services, PATH or the registry.
#>
[CmdletBinding()]
param(
    [switch]$WhatIf,
    [switch]$Yes,
    [switch]$Uninstall,
    [switch]$AddDefenderExclusion,
    [string]$Index,                     # an existing index file, or where to build one
    [string]$Name,                      # the index's name, e.g. d365scm-10.0.49 (default: this computer)
    [string]$Locales = 'en-US',         # label languages to index, e.g. 'en-US,de'
    [string]$Source,                    # PackagesLocalDirectory (default: found on the drives)
    [string]$McpJson = (Join-Path $env:USERPROFILE '.mcp.json'),
    [switch]$SkipIndex,
    [string]$Services = 'sourcecache,knowledge,canvas,core,d365',
    [switch]$SkipServices
)
$ErrorActionPreference = 'Stop'
# Elevated logon tasks, SYSTEM boot tasks and Defender settings need an administrator.
# The -WhatIf preview changes nothing, so it runs without one.
if (-not $WhatIf) {
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = New-Object Security.Principal.WindowsPrincipal $identity
    if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
        Write-Host 'Run this in PowerShell as Administrator.' -ForegroundColor Red
        exit 1
    }
}
$env:PYTHONUTF8 = '1'
$root = $PSScriptRoot
$python = Join-Path $root 'python\python.exe'
$pythonw = Join-Path $root 'python\pythonw.exe'   # the core's interpreter
$pythons = @($python, $pythonw)

function Say($text, $color = 'Gray') { Write-Host $text -ForegroundColor $color }
function Ask-Risky($question) {
    # Opening a hole is never implied: -Yes does not answer this, Enter means no.
    if ($WhatIf -or $Yes) { return $false }
    return (Read-Host "$question [y/N]") -match '^(y|yes|j|ja)$'
}
function Ask($question) {
    if ($WhatIf) { return $false }
    if ($Yes) { return $true }
    return (Read-Host "$question [y/N]") -match '^(y|yes|j|ja)$'
}
function Read-McpJson {
    $script:McpReadSha = $null
    if (-not (Test-Path $McpJson)) { return [ordered]@{ servers = [ordered]@{} } }
    $script:McpReadSha = (Get-FileHash -Algorithm SHA256 -LiteralPath $McpJson).Hash.ToLowerInvariant()
    $text = Get-Content $McpJson -Raw -Encoding UTF8
    if (-not $text.Trim()) { return [ordered]@{ servers = [ordered]@{} } }
    try { $parsed = $text | ConvertFrom-Json } catch { throw "$McpJson is not valid JSON; fix or move it first: $_" }
    $result = [ordered]@{}
    foreach ($p in $parsed.PSObject.Properties) { $result[$p.Name] = $p.Value }
    $list = [ordered]@{}
    if ($result.Contains('servers') -and $result['servers']) {
        foreach ($p in $result['servers'].PSObject.Properties) { $list[$p.Name] = $p.Value }
    }
    $result['servers'] = $list
    return $result
}
function Write-McpJson($config) {
    if (Test-Path $McpJson) {
        $backup = "$McpJson.bak-$(Get-Date -Format yyyyMMdd-HHmmss)-$([guid]::NewGuid().ToString('N'))"
        Copy-Item $McpJson $backup
        Say "  backup: $backup"
    }
    $json = $config | ConvertTo-Json -Depth 20
    $a = @('-B', '-m', 'boring_pondnt.publish_config', '--path', $McpJson)
    if ($script:McpReadSha) { $a += @('--expected-sha', $script:McpReadSha) }
    $OutputEncoding = New-Object Text.UTF8Encoding $false
    $json | & $python @a
    if ($LASTEXITCODE) { throw 'configuration publication failed; old service keys retained; inspect before retrying' }
    $script:McpReadSha = (Get-FileHash -Algorithm SHA256 -LiteralPath $McpJson).Hash.ToLowerInvariant()
    Say "  written: $McpJson" Green
}

if (-not (Test-Path $python)) { throw "run setup.ps1 from the unpacked release folder (no python\python.exe next to it)" }
Say "pondNT setup -- release folder $root" Cyan
if ($WhatIf) { Say "(-WhatIf: looking only, nothing will change)" Yellow }

$stateRoot = Join-Path $env:LOCALAPPDATA 'boring-pondnt'
function Services-Plan($config, [switch]$Apply, [switch]$Remove, [switch]$Finalize) {
    $a = @('-B', '-m', 'boring_pondnt.setup_services', '--root', $root, '--state', $stateRoot, '--only', $Services)
    # Windows PowerShell 5.1 drops an empty argument on its way to a program: without an index
    # (-SkipIndex on a new machine) '--index ""' would arrive as a bare --index.
    if ($Index) { $a += @('--index', $Index) }
    # One mode only: the planner refuses --plan with --uninstall or --finalize.
    if ($Remove) { $a += '--uninstall' }
    elseif ($Finalize) { $a += '--finalize' }
    elseif (-not $Apply) { $a += '--plan' }
    $OutputEncoding = New-Object Text.UTF8Encoding $false
    $servers = [ordered]@{}
    foreach ($k in $config['servers'].Keys) { $servers[$k] = $config['servers'][$k] }
    $json = if ($servers.Count) { $servers | ConvertTo-Json -Depth 20 } else { '{}' }
    $answer = $json | & $python @a
    if ($LASTEXITCODE) { throw 'service preparation failed; working old keys retained; inspect and retry setup' }
    return ($answer | Out-String | ConvertFrom-Json)
}
function Quote($s) { '"' + $s + '"' }
function Service-ProcessCommand($item, [string]$executable) {
    (Quote $executable) + ' -m ' + $item.module + ' ' +
        (($item.arguments | ForEach-Object { Quote $_ }) -join ' ')
}
function Service-TaskCommand($item, [string]$executable) {
    '/c "set PYTHONUTF8=1&& ' + (Service-ProcessCommand $item $executable) +
        ' >> ' + (Quote $item.log) + ' 2>&1"'
}
# The core's task: the person, at logon, in their session, through pythonw (no window).
# The account as Windows names it. %USERDOMAIN% is the workgroup in an SSH session on a
# machine outside a domain, a name Task Scheduler cannot map.
function Current-User {
    try { [Security.Principal.WindowsIdentity]::GetCurrent().Name }
    catch { "$env:USERDOMAIN\$env:USERNAME" }  # pwsh outside Windows (the synthetic test)
}
function Service-UserArguments($item, $arguments = $item.arguments) {
    '-X utf8 -m boring_pondnt.background --log ' + (Quote $item.log) + ' ' + $item.module + ' ' +
        ((@($arguments) | ForEach-Object { Quote $_ }) -join ' ')
}
function Owned-UserTaskPython($item, $task) {
    if (@($task.Actions).Count -ne 1 -or @($task.Triggers).Count -ne 1) { return $null }
    if ($task.Triggers[0].CimClass.CimClassName -ne 'MSFT_TaskLogonTrigger') { return $null }
    if ($task.Principal.UserId -notin @((Current-User), "$env:USERDOMAIN\$env:USERNAME", $env:USERNAME) -or $task.Principal.RunLevel -ne 1) { return $null }
    if ("$($task.Principal.LogonType)" -notin @('Interactive', '3')) { return $null }
    $action = $task.Actions[0]
    if ($action.Execute -notmatch '^[A-Za-z]:\\.*\\python\\pythonw\.exe$') { return $null }
    if ($action.WorkingDirectory -ne $item.folder) { return $null }
    if ($action.Arguments -cne (Service-UserArguments $item)) { return $null }
    return $action.Execute
}
function Owned-TaskPython($item, $task) {
    if ($item.account -eq 'user') { return Owned-UserTaskPython $item $task }
    # Recognize the complete signature setup writes; a matching name is insufficient.
    if (@($task.Actions).Count -ne 1 -or @($task.Triggers).Count -ne 1) { return $null }
    if ($task.Triggers[0].CimClass.CimClassName -ne 'MSFT_TaskBootTrigger') { return $null }
    if ($task.Principal.UserId -notin @('SYSTEM', 'S-1-5-18') -or $task.Principal.RunLevel -ne 1) { return $null }
    $action = $task.Actions[0]
    if ($action.Execute -notin @('cmd.exe', $env:ComSpec)) { return $null }
    if ($action.WorkingDirectory -ne $item.folder) { return $null }
    $match = [regex]::Match($action.Arguments, '^/c "set PYTHONUTF8=1&& "([^"]+)" -m ')
    if (-not $match.Success) { return $null }
    $executable = $match.Groups[1].Value
    if ($executable -notmatch '^[A-Za-z]:\\.*\\python\\python\.exe$') { return $null }
    if ($action.Arguments -cne (Service-TaskCommand $item $executable)) { return $null }
    return $executable
}
function Read-ServiceTask($item) {
    $problems = @()
    $task = Get-ScheduledTask -TaskPath '\pondNT\' -TaskName $item.task -ErrorAction SilentlyContinue -ErrorVariable problems
    if (@($problems | Where-Object { $_.CategoryInfo.Category -ne 'ObjectNotFound' }).Count) {
        throw "Cannot inspect task $($item.task); no task changes allowed."
    }
    return $task
}
function Checked-ServiceTask($item) {
    $task = Read-ServiceTask $item
    if ($task -and -not (Owned-TaskPython $item $task)) {
        throw "Task $($item.task) has an unrecognized/customized definition; retained. Review it manually."
    }
    return $task
}
function Stop-ServiceProcesses($item, [string]$executable) {
    if ($item.account -eq 'user') {
        # Task Scheduler starts "<exe> <arguments>": the exact executable, ending in exactly our arguments.
        $tails = @(' ' + (Service-UserArguments $item))
        Get-CimInstance Win32_Process -Filter "Name='pythonw.exe'" |
            Where-Object { $line = "$($_.CommandLine)"; $_.ExecutablePath -eq $executable -and
                @($tails | Where-Object { $line.EndsWith($_, [StringComparison]::Ordinal) }).Count } |
            ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction Stop }
        return
    }
    # Exact executable and full command only; never a folder substring.
    $expected = Service-ProcessCommand $item $executable
    Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
        Where-Object { $_.ExecutablePath -eq $executable -and $_.CommandLine -ceq $expected } |
        ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction Stop }
}
function Port-Owner([int]$port) {
    $listen = Get-NetTCPConnection -LocalAddress 127.0.0.1 -LocalPort $port -State Listen -ErrorAction SilentlyContinue |
        Select-Object -First 1
    if (-not $listen) { return $null }
    return (Get-CimInstance Win32_Process -Filter "ProcessId=$($listen.OwningProcess)").ExecutablePath
}
function Register-ServiceTask($item) {
    # SYSTEM: a boot trigger under the user's account with S4U never fired on a
    # dev VM (probe); the same probe as SYSTEM did. Loopback services, key-gated, but with
    # machine authority -- a deliberate trade; revisit with a dedicated service account (TODO).
    if ($item.account -eq 'user') {
        # as the person, at logon, elevated like Visual Studio run as administrator; no window.
        $action = New-ScheduledTaskAction -Execute $item.python -Argument (Service-UserArguments $item) -WorkingDirectory $item.folder
        $trigger = New-ScheduledTaskTrigger -AtLogOn -User (Current-User)
        $principal = New-ScheduledTaskPrincipal -UserId (Current-User) -LogonType Interactive -RunLevel Highest
    } else {
        $line = Service-TaskCommand $item $item.python
        $action = New-ScheduledTaskAction -Execute 'cmd.exe' -Argument $line -WorkingDirectory $item.folder
        $trigger = New-ScheduledTaskTrigger -AtStartup
        $principal = New-ScheduledTaskPrincipal -UserId 'SYSTEM' -LogonType ServiceAccount -RunLevel Highest
    }
    $settings = New-ScheduledTaskSettingsSet -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 3 `
        -RestartInterval (New-TimeSpan -Minutes 1) -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
        -StartWhenAvailable -MultipleInstances IgnoreNew
    $existing = Checked-ServiceTask $item
    if ($existing) {
        $oldPython = Owned-TaskPython $item $existing
        Stop-ScheduledTask -TaskPath '\pondNT\' -TaskName $item.task -ErrorAction Stop
        Stop-ServiceProcesses $item $oldPython
    }
    Register-ScheduledTask -TaskPath '\pondNT\' -TaskName $item.task -Action $action -Trigger $trigger `
        -Principal $principal -Settings $settings -Force | Out-Null
    Start-ScheduledTask -TaskPath '\pondNT\' -TaskName $item.task
}
# The kit: served from the state folder; setup seeds it once and never overwrites it.
function Say-Kit($kit) {
    if (-not $kit) { return }
    switch ($kit.state) {
        'new' { Say "    The kit -> $($kit.folder) (copied once from this release; later releases never overwrite it)" }
        'kept' {
            if ($kit.seeded) { Say "    The kit: copied to $($kit.folder)" Green }
            elseif (@($kit.differs).Count) {
                Say "    The kit: $($kit.folder) kept; differs from this release's kit in: $(@($kit.differs) -join ', ')" Yellow
                Say "      review and merge deliberately: compare $($kit.release) with it (setup never overwrites it)" Yellow
            } else { Say "    The kit: $($kit.folder) kept; same as this release's kit" }
        }
        'no-release-kit' { Say "    The kit: this release has none at $($kit.release)" Yellow }
    }
}
function Wait-Port([int]$port, [string]$python, [int]$seconds = 30) {
    # Answering is not enough: the listener must be this release's python, not a leftover.
    $until = (Get-Date).AddSeconds($seconds)
    while ((Get-Date) -lt $until) {
        if ((Port-Owner $port) -eq $python) { return $true }
        Start-Sleep -Milliseconds 500
    }
    return $false
}

# ---------------------------------------------------------------- uninstall
if ($Uninstall) {
    $config = Read-McpJson
    $svc = Services-Plan $config -Remove  # retain evidence before entries are removed
    foreach ($conflict in $svc.conflicts) { Say "  $conflict" Yellow }
    $tasks = @()
    foreach ($item in $svc.services) {
        $task = Read-ServiceTask $item
        if (-not $task) { continue }
        $oldPython = Owned-TaskPython $item $task
        if ($oldPython) { $tasks += [pscustomobject]@{ item = $item; python = $oldPython } }
        else { Say "  $($item.task): customized/unrecognized task retained." Yellow }
    }
    foreach ($name in $svc.tasks) {
        if ($name -notin @($svc.services | ForEach-Object { $_.task })) {
            Say "  Task $name cannot be verified; retained (Sourcecache needs -Index)." Yellow
        }
    }
    if ($svc.removals -or $tasks) {
        Say "`nServices: entries $(@($svc.removals) -join ', '); scheduled tasks $(@($tasks | ForEach-Object { $_.item.task }) -join ', ')"
        if ($svc.kit) { Say "  The kit stays in $($svc.kit) (yours; remove it by hand if you want it gone)." }
        if (Ask "Stop and remove them (their data folders stay)?") {
            foreach ($t in $tasks) {
                $null = Checked-ServiceTask $t.item
                Stop-ScheduledTask -TaskPath '\pondNT\' -TaskName $t.item.task -ErrorAction Stop
                Stop-ServiceProcesses $t.item $t.python
                Unregister-ScheduledTask -TaskPath '\pondNT\' -TaskName $t.item.task -Confirm:$false
            }
            if ($svc.removals) { foreach ($k in $svc.removals) { $config['servers'].Remove($k) }; Write-McpJson $config }
        }
    }
    # Ask everything first, then list what stays.
    $excluded = @()
    try { $excluded = @((Get-MpPreference -ErrorAction Stop).ExclusionProcess | Where-Object { $_ -in $pythons }) } catch { }
    if ($excluded -and (Ask "`nRemove the Defender exclusions for this release's python ($($excluded -join ', '))?")) {
        foreach ($p in $excluded) { Remove-MpPreference -ExclusionProcess $p }
        Say "  Defender exclusions removed." Green
        $excluded = @()
    }
    Say "`nAlso remove by hand, if you made them:"
    Say "  - service data (notebook, canvases, keys, logs): $(@($svc.folders) -join '; ')"
    Say "  - the index file (and its folder) you built"
    foreach ($p in $excluded) { Say "  - Defender: Remove-MpPreference -ExclusionProcess '$p'" }
    Say "  - this folder: $root"
    Say "  - state and logs: $(Join-Path $env:LOCALAPPDATA 'boring-pondnt')"
    return
}

# ---------------------------------------------------------------- 1. look
Say "`n1. This machine" Cyan
$env:PYTHONUTF8 = '1'
if ($WhatIf) {
    $check = [pscustomobject]@{ ok = $true; checks = @() }
    Say "  Preview: live shell, job, write and database probes skipped; readiness is unverified." Yellow
} else {
    $check = & $python -m boring_pondnt.server --self-check 2>$null | Out-String | ConvertFrom-Json
}
foreach ($c in $check.checks) {
    $mark = if ($c.ok) { 'ok  ' } elseif ($c.required) { 'FAIL' } else { 'note' }
    $color = if ($c.ok) { 'Gray' } elseif ($c.required) { 'Red' } else { 'Yellow' }
    Say ("  {0} {1,-16} {2}" -f $mark, $c.name, $c.detail) $color
}
if (-not $check.ok) { Say "  A required check failed: the core server will not work properly until it is fixed." Red }
$hasD365Tools = [bool]($check.checks | Where-Object { $_.name -eq 'd365fo_tools' -and $_.ok })
$hasXref = [bool]($check.checks | Where-Object { $_.name -eq 'xref_database' -and $_.ok })
$defender = $check.checks | Where-Object { $_.name -eq 'defender' }

if (-not $Source) {
    $Source = Get-PSDrive -PSProvider FileSystem | ForEach-Object { Join-Path $_.Root 'AosService\PackagesLocalDirectory' } |
        Where-Object { Test-Path $_ } | Select-Object -First 1
}
Say ("  {0} {1,-16} {2}" -f $(if ($Source) { 'ok  ' } else { 'note' }), 'packages', $(if ($Source) { $Source } else { 'no PackagesLocalDirectory found (-Source)' }))

$existing = $null
if ($Index) { if (Test-Path $Index) { $existing = $Index } }
else {
    $existing = Get-PSDrive -PSProvider FileSystem | ForEach-Object { Join-Path $_.Root 'pondnt' } | Where-Object { Test-Path $_ } |
        ForEach-Object { Get-ChildItem $_ -Filter *.db -File -ErrorAction SilentlyContinue } |
        Sort-Object LastWriteTime -Descending | Select-Object -First 1 -ExpandProperty FullName
}
# Where a new index goes: the volume the source is on, if it has room; otherwise the
# non-system drive with the most free space. Never Azure's temporary disk (wiped on deallocation).
$minFree = 10GB
$temporary = @(Get-CimInstance Win32_LogicalDisk -ErrorAction SilentlyContinue |
    Where-Object { $_.VolumeName -eq 'Temporary Storage' } | ForEach-Object { $_.DeviceID + '\' })
$sourceDrive = if ($Source) { Get-PSDrive -PSProvider FileSystem | Where-Object { $Source.StartsWith($_.Root, 'OrdinalIgnoreCase') } |
    Select-Object -First 1 } else { $null }
$data = if ($sourceDrive -and $sourceDrive.Free -ge $minFree -and $temporary -notcontains $sourceDrive.Root) { $sourceDrive }
    else { Get-PSDrive -PSProvider FileSystem | Where-Object { $_.Free -ge $minFree -and $_.Root -ne "$env:SystemDrive\" -and
                                                              $temporary -notcontains $_.Root } |
           Sort-Object Free -Descending | Select-Object -First 1 }
if (-not $Index) { $Index = if ($existing) { $existing } elseif ($data) { Join-Path $data.Root 'pondnt\d365.db' } else { 'C:\pondnt\d365.db' } }
if (-not $Name) { $Name = $env:COMPUTERNAME.ToLower() }
Say ("  {0} {1,-16} {2}" -f $(if ($existing) { 'ok  ' } else { 'note' }), 'source index', $(if ($existing) { "$existing (existing)" } else { "none yet; would build $Index" }))

# ---------------------------------------------------------------- 2. the plan
Say "`n2. The plan" Cyan
$plan = @()
if (-not $existing -and -not $SkipIndex -and $Source) {
    $plan += "build the source index: $Index (name $Name, label languages $Locales; 10-35 min with the Defender exclusion, depending on the disk, many times longer without it; about 6 GB for two label languages)"
}
$config = Read-McpJson
$svcPlan = $null
if (-not $SkipServices) {
    if (-not $existing -and $plan -match 'build the source index') { $indexForServices = $Index } else { $indexForServices = $existing }
    $Index = "$indexForServices"
    $svcPlan = Services-Plan $config
    foreach ($conflict in $svcPlan.conflicts) { Say "  conflict/manual action: $conflict" Yellow }
    foreach ($item in $svcPlan.services) { $null = Checked-ServiceTask $item }
    if ($svcPlan.services) {
        $plan += "services on 127.0.0.1 with access keys: $(@($svcPlan.services | ForEach-Object { $_.name }) -join ', '); started by Task Scheduler \pondNT\ (services: SYSTEM at boot; core: you at logon); data in $stateRoot\services"
        foreach ($s in $svcPlan.services) {
            $what = if ($s.new_key) { 'new key' } else { 'key kept' }
            $runs = if ($s.account -eq 'user') { "runs as $(Current-User) at logon" } else { 'runs as SYSTEM at boot' }
            Say "    $($s.name) -> $($s.url) ($what, person $($s.subject); $runs)"
        }
        Say-Kit $svcPlan.kit
        if ($svcPlan.services | Where-Object { $_.name -eq 'boring-d365' }) {
            Say "  This gives Copilot boring-d365: it can build, sync, restart services and write to AxDB." Yellow
        }
    }
}
$scanned = $defender -and $defender.detail -match 'scans'
if ($WhatIf) {
    # The preview skips the probes, Defender's included: say what applying will check.
    $plan += "Defender: checked when applying; if it scans $python and $pythonw, setup asks (default no) whether to exclude them"
}
if ($scanned -and $AddDefenderExclusion) {
    $plan += "Defender: exclude $python and $pythonw (DANGEROUS -- INSTALL.md, step 2)"
} elseif ($scanned -and -not $Yes) {
    $plan += "Defender (asked; default no): exclude $python and $pythonw -- much faster index and search, but files they open are not scanned (INSTALL.md, step 2)"
}
if ($plan) { $plan | ForEach-Object { Say "  - $_" } } else { Say "  no automatic changes; resolve any manual actions above" Green }
if (-not $WhatIf -and -not $hasD365Tools) { Say "  (not done by setup) d365fo.tools is missing: PowerShell-dependent d365 tools will refuse until an admin runs Install-Module d365fo.tools" Yellow }
if (-not $WhatIf -and -not $hasXref) { Say "  (note) the cross-reference database did not answer: the combined server xref tools will say so when asked" Yellow }
if ($scanned -and $Yes -and -not $AddDefenderExclusion) {
    Say "  (not done by setup) Defender scans every file pondNT reads: slower. -Yes never excludes; read INSTALL.md step 2, then rerun with -AddDefenderExclusion" Yellow
}
if (-not $plan -or $WhatIf) { if ($WhatIf -and $plan) { Say "`n-WhatIf: nothing changed." Yellow }; return }

# ---------------------------------------------------------------- 3. do it, step by step
Say "`n3. Doing it" Cyan
if ($plan -match 'Defender') {
    Say "  Defender: excluding pondNT's python means files it opens are not scanned at that moment, and whoever"
    Say "  can replace $root\python gets an unscanned program. Fine on a disposable dev box; ask security elsewhere." Yellow
    $agreed = if ($AddDefenderExclusion) { Ask "Exclude $python and $pythonw from Defender scanning?" }
              else { Ask-Risky "Exclude $python and $pythonw from Defender scanning?" }
    if ($agreed) {
        foreach ($p in $pythons) { Add-MpPreference -ExclusionProcess $p }
        Say "  Defender exclusions added. Undo: .\setup.ps1 -Uninstall, or Remove-MpPreference -ExclusionProcess <path>" Green
    } else { Say "  Defender left as it is." }
}
if ($plan -match 'build the source index') {
    if (Ask "Build the source index now (10-35 min with the Defender exclusion, depending on the disk, many times longer without it; this window stays busy)?") {
        & (Join-Path $root 'boring-sourcecache-d365.cmd') build --source $Source --db $Index --name $Name --locales $Locales
        if ($LASTEXITCODE) { Say '  the index build failed; no service configuration was changed' Red }
    }
}
$write = $false
$servicesChanged = $false
if ($svcPlan -and $svcPlan.services -and (Ask "Set up the services (configs, keys, boot tasks, .mcp.json entries)?")) {
    foreach ($item in $svcPlan.services) { $null = Checked-ServiceTask $item }
    $done = Services-Plan $config -Apply
    $servicesChanged = $true
    Say-Kit $done.kit
    foreach ($s in $done.services) {
        Register-ServiceTask $s
        $port = ([Uri]$s.url).Port
        if (Wait-Port $port $s.python) { Say "  $($s.name): running on $($s.url)" Green }
        else { Say "  $($s.name): not served by this release on port $port yet; see $($s.log)" Yellow }
        $config['servers'][$s.name] = $s.entry
        $write = $true
    }
    Say "  The access keys are in .mcp.json (headers). The services listen on 127.0.0.1 only, but a key is a"
    Say "  bearer secret: whoever holds it and can reach the port acts with its role. Keep .mcp.json private."
}
if ($write) {
    Write-McpJson $config
    if ($servicesChanged) { $null = Services-Plan (Read-McpJson) -Apply -Finalize }
    Say "  Restart Visual Studio (or reload MCP servers) to pick them up." Green
}
Say "`nDone. Undo everything: .\setup.ps1 -Uninstall" Cyan
