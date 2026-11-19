<#
.SYNOPSIS
    Install the standard desktop services from C:\Program Files\pondNT\<version>.
.DESCRIPTION
    -WhatIf previews without changing files or tasks. Run apply/uninstall as Administrator.
    Services run as the person at logon through pythonw. Core and the four D365 servers
    run elevated; Sourcecache, Knowledge, Canvas and Ferry do not.
    Setup adds mode=keys once to exact legacy local configs and keeps other configs.
    -RotateKeys explicitly replaces selected bearers.
    -Index names an existing Sourcecache index. Build it separately with the sourcecache CMD.
    Setup warns about writable release files, never changes their permissions.
    Machine diagnostics are separate: boring-pondnt-core.cmd --self-check.
    The optional Pond tunnel is independent of setup.
#>
[CmdletBinding()]
param(
    [switch]$WhatIf,
    [switch]$Yes,
    [switch]$Uninstall,
    [switch]$AddDefenderExclusion,
    [string]$Index,                     # an existing Sourcecache index; setup never builds it
    [string]$McpJson = (Join-Path $env:USERPROFILE '.mcp.json'),
    [string]$Services,                 # omitted: read the owned tasks; fresh defaults come from Python
    [switch]$SkipServices,
    [switch]$RotateKeys                 # the only operation that replaces selected service bearers
)
$ErrorActionPreference = 'Stop'
# Task registration and optional Defender settings need an administrator.
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
function Ask($question) {
    if ($WhatIf) { return $false }
    if ($Yes) { return $true }
    return (Read-Host "$question [y/N]") -match '^(y|yes|j|ja)$'
}
# Read-only inspection. Potential grants are warnings, not effective-access claims.
$trustedSids = @('S-1-5-32-544', 'S-1-5-18', 'S-1-5-80-956008885-3418522649-1831038044-1853292631-2271478464')
$writeMask = [long](0x2 -bor 0x4 -bor 0x10 -bor 0x100 -bor 0x10000 -bor 0x40 -bor 0x40000 -bor 0x80000 -bor 0x10000000 -bor 0x40000000)
function Inspection-Path([string]$path) {
    if ($path.StartsWith('\\?\')) { return $path }
    return '\\?\' + $path
}
function Release-OpenWrites {
    try {
        $path = Inspection-Path $root
        $items = @(Get-Item -LiteralPath $path -Force -ErrorAction Stop) +
            @(Get-ChildItem -LiteralPath $path -Recurse -Force -ErrorAction Stop)
    } catch { "$root permissions unchecked: $_"; return }
    foreach ($item in $items) {
        if ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) {
            "$($item.FullName): reparse point unchecked"; continue
        }
        try {
            $acl = Get-Acl -LiteralPath (Inspection-Path $item.FullName) -ErrorAction Stop
            $owner = $acl.GetOwner([Security.Principal.SecurityIdentifier]).Value
            if ($owner -notin $trustedSids) { "$($item.FullName): owner $owner can change permissions" }
            foreach ($rule in $acl.GetAccessRules($true, $true, [Security.Principal.SecurityIdentifier])) {
                if ($rule.AccessControlType -ne 'Allow' -or $rule.IdentityReference.Value -in $trustedSids) { continue }
                if (([long]$rule.PropagationFlags -band 2) -ne 0) { continue }
                if (([long]$rule.FileSystemRights) -band $writeMask) {
                    "$($item.FullName): $($rule.IdentityReference.Value) $($rule.FileSystemRights)"
                }
            }
        } catch { "$($item.FullName): permissions unchecked: $_" }
    }
}
function Warn-ReleasePermissions {
    $findings = @(Release-OpenWrites)
    if ($findings) {
        Say "  WARNING: others may replace code executed by elevated tasks, or permissions could not be checked." Yellow
        $findings | ForEach-Object { Say "    $_" Yellow }
        Say "  Use C:\Program Files\pondNT\<version> with administrator-controlled permissions. Setup continues without changing ACLs or ownership." Yellow
    }
}
function Assert-ExistingIndex {
    if (-not $Index) { throw 'Sourcecache needs -Index pointing to an existing index. Build it separately with boring-sourcecache-d365.cmd build.' }
    try {
        $file = Get-Item -LiteralPath $Index -Force -ErrorAction Stop
        if ($file.PSIsContainer) { throw 'expected a file' }
        $handle = [IO.File]::Open($file.FullName, [IO.FileMode]::Open, [IO.FileAccess]::Read, [IO.FileShare]::ReadWrite)
        $handle.Dispose()
    } catch { throw "Cannot read Sourcecache index $Index; build or repair it separately: $_" }
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
    if ($LASTEXITCODE) { throw 'configuration publication failed; inspect before rerunning setup without -RotateKeys' }
    $script:McpReadSha = (Get-FileHash -Algorithm SHA256 -LiteralPath $McpJson).Hash.ToLowerInvariant()
    Say "  written: $McpJson" Green
}

if (-not (Test-Path $python)) { throw "run setup.ps1 from the unpacked release folder (no python\python.exe next to it)" }
Say "pondNT setup -- release folder $root" Cyan
if ($WhatIf) { Say "(-WhatIf: looking only, nothing will change)" Yellow }

$stateRoot = Join-Path $env:LOCALAPPDATA 'boring-pondnt'
function Services-Plan($config, [switch]$Apply, [switch]$Remove) {
    $a = @('-B', '-m', 'boring_pondnt.setup_services', '--root', $root, '--state', $stateRoot,
           "--only=$Services", "--installed=$($script:InstalledServices -join ',')")
    # Windows PowerShell 5.1 drops an empty argument on its way to a program: without an index
    # '--index ""' would arrive as a bare --index.
    if ($Index) { $a += @('--index', $Index) }
    if ($Remove) { $a += '--uninstall' }
    else {
        if (-not $Apply) { $a += '--plan' }
        if ($RotateKeys) { $a += '--rotate-keys' }
    }
    $OutputEncoding = New-Object Text.UTF8Encoding $false
    $servers = [ordered]@{}
    foreach ($k in $config['servers'].Keys) { $servers[$k] = $config['servers'][$k] }
    $json = if ($servers.Count) { $servers | ConvertTo-Json -Depth 20 } else { '{}' }
    $answer = $json | & $python @a
    if ($LASTEXITCODE) { throw 'service preparation failed; inspect the named files, then rerun setup (omit -RotateKeys to repair the retained bearer)' }
    return ($answer | Out-String | ConvertFrom-Json)
}
function Quote($s) { '"' + $s + '"' }
function Current-User {
    try { [Security.Principal.WindowsIdentity]::GetCurrent().Name }
    catch { "$env:USERDOMAIN\$env:USERNAME" }  # pwsh outside Windows (the synthetic test)
}
function Service-UserArguments($item, $arguments = $item.arguments) {
    '-X utf8 -m boring_pondnt.background --log ' + (Quote $item.log) + ' ' + $item.module + ' ' +
        ((@($arguments) | ForEach-Object { Quote $_ }) -join ' ')
}
function Sourcecache-TaskIndex($task) {
    if (@($task.Actions).Count -ne 1) { return $null }
    $found = [regex]::Match($task.Actions[0].Arguments, '"--db" "([^"]+)"')
    if ($found.Success) { return $found.Groups[1].Value }
    return $null
}
function Owned-TaskPython($item, $task) {
    if (@($task.Actions).Count -ne 1 -or @($task.Triggers).Count -ne 1) { return $null }
    if ($task.Triggers[0].CimClass.CimClassName -ne 'MSFT_TaskLogonTrigger') { return $null }
    if ($task.Triggers[0].UserId -notin @((Current-User), "$env:USERDOMAIN\$env:USERNAME", $env:USERNAME)) { return $null }
    $level = if ($item.highest) { 1 } else { 0 }
    if ($task.Principal.UserId -notin @((Current-User), "$env:USERDOMAIN\$env:USERNAME", $env:USERNAME) -or $task.Principal.RunLevel -ne $level) { return $null }
    if ("$($task.Principal.LogonType)" -notin @('Interactive', '3')) { return $null }
    $action = $task.Actions[0]
    if ($action.Execute -notmatch '^[A-Za-z]:\\.*\\python\\pythonw\.exe$') { return $null }
    if ($action.WorkingDirectory -ne $item.folder) { return $null }
    $arguments = @($item.arguments)
    if ($item.module -eq 'boring_sourcecache_d365.server') {
        $observed = Sourcecache-TaskIndex $task
        $position = [array]::IndexOf($arguments, '--db')
        if (-not $observed -or $position -lt 0 -or $position + 1 -ge $arguments.Count) { return $null }
        $arguments[$position + 1] = $observed
    }
    if ($action.Arguments -cne (Service-UserArguments $item $arguments)) {
        # The previous generated Canvas launcher read web.json for its second port.
        if ($item.module -ne 'boring_canvas.server') { return $null }
        $legacy = @($arguments[0..3]) + @('--host', '127.0.0.1', '--unsafe-url-auth')
        if ($action.Arguments -cne (Service-UserArguments $item $legacy)) { return $null }
    }
    return $action.Execute
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
function Service-Ports($item) {
    return @(([Uri]$item.url).Port)
}
function Service-Listeners($item) {
    foreach ($port in @(Service-Ports $item)) {
        $problems = @()
        $found = @(Get-NetTCPConnection -LocalAddress 127.0.0.1 -LocalPort $port -State Listen -ErrorAction SilentlyContinue -ErrorVariable problems)
        if (@($problems | Where-Object { $_.CategoryInfo.Category -ne 'ObjectNotFound' }).Count) {
            throw "Cannot inspect listener on port $port; no task replacement allowed."
        }
        $found
    }
}
function Assert-ServiceHandover($item) {
    $task = Checked-ServiceTask $item
    if (-not $task -and @(Service-Listeners $item).Count) {
        throw "$($item.name): a required port is occupied without our task; stop its owner explicitly."
    }
}
function Wait-ServiceStopped($item, [int]$seconds = 30) {
    $clock = [Diagnostics.Stopwatch]::StartNew()
    do {
        $task = Checked-ServiceTask $item
        $busy = $task -and "$($task.State)" -in @('Running','Queued','4','2')
        $listeners = @(Service-Listeners $item)
        if (-not $busy -and -not $listeners) { return }
        if ($clock.Elapsed.TotalSeconds -ge $seconds) { break }
        Start-Sleep -Milliseconds 250
    } while ($true)
    throw "$($item.name): task did not stop or ports remain occupied; task=$($task.State), occupied ports=$(@($listeners | ForEach-Object LocalPort) -join ','). Setup stopped."
}
function Stop-ServiceTask($item) {
    $task = Checked-ServiceTask $item
    if ($task) {
        Stop-ScheduledTask -TaskPath '\pondNT\' -TaskName $item.task -ErrorAction Stop
    }
    Wait-ServiceStopped $item
}
function Register-ServiceTask($item) {
    $action = New-ScheduledTaskAction -Execute $item.python -Argument (Service-UserArguments $item) -WorkingDirectory $item.folder
    $trigger = New-ScheduledTaskTrigger -AtLogOn -User (Current-User)
    $level = if ($item.highest) { 'Highest' } else { 'Limited' }
    $principal = New-ScheduledTaskPrincipal -UserId (Current-User) -LogonType Interactive -RunLevel $level
    $settings = New-ScheduledTaskSettingsSet -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 3 `
        -RestartInterval (New-TimeSpan -Minutes 1) -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
        -StartWhenAvailable -MultipleInstances IgnoreNew
    Stop-ServiceTask $item
    Register-ScheduledTask -TaskPath '\pondNT\' -TaskName $item.task -Action $action -Trigger $trigger `
        -Principal $principal -Settings $settings -Force | Out-Null
    Start-ScheduledTask -TaskPath '\pondNT\' -TaskName $item.task
}
# The handbook: served from the state folder; setup seeds it once and never overwrites it.
function Say-Handbook($kit) {
    if (-not $kit) { return }
    switch ($kit.state) {
        'new' { Say "    The handbook -> $($kit.folder) (copied once from this release; later releases never overwrite it)" }
        'kept' {
            if ($kit.seeded) { Say "    The handbook: copied to $($kit.folder)" Green }
            elseif ($kit.differs -or $kit.new_pages) {
                if ($kit.differs) {
                    Say "    The handbook: $($kit.folder) kept; differs from this release's handbook in: $(@($kit.differs) -join ', ')" Yellow
                    Say "      review and merge deliberately: compare $($kit.release) with it (setup never overwrites it)" Yellow
                } else { Say "    The handbook: $($kit.folder) kept" }
                if ($kit.new_pages) {
                    Say "      new in this release: $(@($kit.new_pages) -join ', ')" Yellow
                    Say "      Ferry uses the release's copy for missing service guides. Copy new pages into your handbook to adapt them." Yellow
                }
            } else { Say "    The handbook: $($kit.folder) kept; same as this release's handbook" }
        }
        'no-release-kit' { Say "    The handbook: this release has none at $($kit.release)" Yellow }
    }
}
function Wait-ServiceReady($item, [int]$seconds = 30) {
    $clock = [Diagnostics.Stopwatch]::StartNew()
    $ports = @(Service-Ports $item)
    do {
        $listeners = @(Service-Listeners $item)
        $missing = @($ports | Where-Object { $_ -notin @($listeners.LocalPort) })
        $task = Checked-ServiceTask $item
        $running = $task -and "$($task.State)" -in @('Running','4')
        if ($running -and -not $missing -and $listeners.Count -ge $ports.Count) { return $true }
        if ($clock.Elapsed.TotalSeconds -ge $seconds) { return $false }
        Start-Sleep -Milliseconds 250
    } while ($true)
}
function Service-StartFailure($item) {
    $detail = 'task result unavailable'
    try {
        $task = Checked-ServiceTask $item
        $info = Get-ScheduledTaskInfo -TaskPath '\pondNT\' -TaskName $item.task -ErrorAction Stop
        $detail = "task=$($task.State), LastTaskResult=$($info.LastTaskResult)"
    } catch { }
    return "$($item.name): task did not reach Running with every configured listener; $detail. See $($item.log). Setup stopped; rotation may already have published new client entries. Rerun without -RotateKeys after resolving this failure."
}
function Assert-McpUnchanged {
    $current = $null
    if (Test-Path -LiteralPath $McpJson -ErrorAction Stop) {
        $current = (Get-FileHash -Algorithm SHA256 -LiteralPath $McpJson).Hash.ToLowerInvariant()
    }
    if ($current -cne $script:McpReadSha) { throw 'Client configuration changed during setup; inspect before retrying.' }
}
function Read-InstalledServiceScope {
    $answer = & $python -B -m boring_pondnt.setup_services --root $root --state $stateRoot --describe
    if ($LASTEXITCODE) { throw 'Cannot read service task templates; no changes allowed.' }
    $templates = $answer | Out-String | ConvertFrom-Json
    $installed = @()
    $sourceIndex = ''
    foreach ($item in $templates.services) {
        $task = Read-ServiceTask $item
        if ($task -and (Owned-TaskPython $item $task)) {
            $installed += $item.short
            if ($item.short -eq 'sourcecache') { $sourceIndex = Sourcecache-TaskIndex $task }
        }
    }
    return [pscustomobject]@{ installed=$installed; index=$sourceIndex; defaults=$templates.default_services; configured=$templates.configured_services }
}
function Resolve-ServiceScope($scope, [string]$selection, [string]$index, [bool]$explicitServices, [bool]$explicitIndex) {
    $existing = @($scope.installed | Where-Object { $_ -ne 'gateway' })
    if ($explicitServices) {
        if (-not $selection.Trim()) { throw '-Services must name services; use -SkipServices to omit setup.' }
        $requested = @($selection -split ',' | ForEach-Object { $_.Trim() } | Where-Object { $_ })
    } elseif ($scope.installed.Count) {
        $requested = $existing
    } else {
        $requested = @($scope.defaults)
    }
    if (-not $explicitIndex) { $index = $scope.index }
    return [pscustomobject]@{
        services = (@($requested) + $existing | Select-Object -Unique) -join ','
        index = $index
    }
}
function Check-UnselectedServices($scope, [string]$selection, [bool]$explicitServices) {
    $requested = $selection -split ','
    $omitted = @($scope.configured | Where-Object { $_ -and $_ -notin $requested })
    if ($omitted.Count) {
        $names = $omitted -join ', '
        Say "  Services configured here but outside this run: $names" Yellow
        if (-not $explicitServices) {
            throw 'Pass -Services with every service you recorded in step 2 before proceeding.'
        }
    }
}
$script:InstalledServices = @()
if (-not $SkipServices -or $Uninstall) {
    $scope = Read-InstalledServiceScope
    $script:InstalledServices = @($scope.installed)
    $selection = Resolve-ServiceScope $scope $Services $Index ([bool]$PSBoundParameters.ContainsKey('Services')) ([bool]$PSBoundParameters.ContainsKey('Index'))
    $Services = $selection.services
    if (-not $Uninstall) { Check-UnselectedServices $scope $Services ($PSBoundParameters.ContainsKey('Services')) }
    $Index = $selection.index
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
            Say "  Task $name cannot be verified; retained (its task definition could not be verified)." Yellow
        }
    }
    if ($svc.removals -or $tasks) {
        Say "`nServices: entries $(@($svc.removals) -join ', '); scheduled tasks $(@($tasks | ForEach-Object { $_.item.task }) -join ', ')"
        if ($svc.handbook) { Say "  The handbook stays in $($svc.handbook) (yours; remove it by hand if you want it gone)." }
        if (Ask "Stop and remove them (their data folders stay)?") {
            Assert-McpUnchanged
            foreach ($t in $tasks) {
                Stop-ServiceTask $t.item
                Unregister-ScheduledTask -TaskPath '\pondNT\' -TaskName $t.item.task -Confirm:$false -ErrorAction Stop
            }
            if ($svc.removals) {
                Assert-McpUnchanged
                foreach ($k in $svc.removals) { $config['servers'].Remove($k) }
                Write-McpJson $config
            }
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
    Say '  - services\<name>\client.headers.json (may contain active bearer keys)'
    Say "  - client backups next to ${McpJson}: $(Split-Path -Leaf $McpJson).bak-* (may contain active bearer keys)"
    Say "  - the index file (and its folder) you built"
    foreach ($p in $excluded) { Say "  - Defender: Remove-MpPreference -ExclusionProcess '$p'" }
    Say "  - this folder: $root"
    Say "  - state and logs: $(Join-Path $env:LOCALAPPDATA 'boring-pondnt')"
    return
}

# ---------------------------------------------------------------- 1. installation inputs only
Say "`n1. Installation inputs" Cyan
if (-not (Test-Path -LiteralPath $pythonw -PathType Leaf)) { throw "Missing bundled interpreter: $pythonw" }
if (-not $SkipServices -and 'sourcecache' -in @($Services -split ',' | ForEach-Object { $_.Trim() })) {
    Assert-ExistingIndex
}
Say "  Runtime diagnostics are separate: .\boring-pondnt-core.cmd --self-check. Machine readiness is unverified."
# ---------------------------------------------------------------- 2. the plan
Say "`n2. The plan" Cyan
$plan = @()
$config = Read-McpJson
$svcPlan = $null
if (-not $SkipServices) {
    $svcPlan = Services-Plan $config
    $modules = @($svcPlan.services | ForEach-Object module | Sort-Object -Unique)
    if ($modules) {
        & $python -B -X utf8 -c "import importlib.util,sys; missing=[m for m in sys.argv[1:] if importlib.util.find_spec(m) is None]; print('Missing bundled modules: '+', '.join(missing)) if missing else None; sys.exit(bool(missing))" @modules
        if ($LASTEXITCODE) { throw 'Bundled modules are missing; unpack the complete release before setup.' }
    }
    foreach ($conflict in $svcPlan.conflicts) { Say "  conflict/manual action: $conflict" Yellow }
    foreach ($item in $svcPlan.services) { $null = Checked-ServiceTask $item }
    if ($svcPlan.removals) { $plan += ".mcp.json entries removed: $(@($svcPlan.removals) -join ', ') (reached through boring-gateway)" }
    if ($svcPlan.services) {
        $plan += "services on 127.0.0.1 with access keys: $(@($svcPlan.services | ForEach-Object { $_.name }) -join ', '); started by Task Scheduler \pondNT\ (as you at logon); data in $stateRoot\services"
        foreach ($s in $svcPlan.services) {
            $what = if ($RotateKeys) { 'ROTATE key' } elseif ($s.new_key) { 'new key' } else { 'key kept' }
            $runs = "runs as $(Current-User) at logon"
            $via = if ($s.behind) { "; Copilot reaches it through $($s.behind)" } else { '' }
            Say "    $($s.name) -> $($s.url) ($what, person $($s.subject); $runs$via)"
            if ($s.config_change) { Say "      $($s.config_change)" }
        }
        if ($svcPlan.gateway) {
            Say "    $($svcPlan.gateway.name) carries $(@($svcPlan.gateway.carries) -join ', '): Copilot sees its four tools instead of theirs"
        }
        Say-Handbook $svcPlan.handbook
        if ($svcPlan.services | Where-Object { $_.name -eq 'boring-d365' }) {
            Say "  This gives Copilot boring-d365: it can build, sync, restart services and write to AxDB." Yellow
        }
    }
}
if (-not $SkipServices) { Warn-ReleasePermissions }
if ($AddDefenderExclusion) { $plan += "Defender: exclude $python and $pythonw (INSTALL.md, step 2)" }
if ($plan) { $plan | ForEach-Object { Say "  - $_" } }
else { Say "  No automatic changes; resolve any reported conflicts." }
if ($WhatIf) { Say "`n-WhatIf: nothing changed."; return }

# ---------------------------------------------------------------- 3. do it, step by step
Say "`n3. Doing it" Cyan
if ($AddDefenderExclusion) {
    Say "  Excluding these interpreters means files they open are not scanned. Read INSTALL.md step 2." Yellow
    if (Ask "Exclude $python and $pythonw from Defender scanning?") {
        foreach ($p in $pythons) { Add-MpPreference -ExclusionProcess $p }
    }
}
$clientChanged = $false
$write = $false
$servicesChanged = $false
try {
    if ($svcPlan -and $svcPlan.services -and (Ask "Set up the services (configs, keys, logon tasks, .mcp.json entries)?")) {
        foreach ($item in @($svcPlan.services)) { if ($item) { Assert-ServiceHandover $item } }
        Assert-McpUnchanged
        if ($RotateKeys) {
            foreach ($item in $svcPlan.services) { Stop-ServiceTask $item }
        }
        $done = Services-Plan $config -Apply
        $wanted = @(@($svcPlan.services) | Where-Object { $_ } | ForEach-Object name | Sort-Object) -join ','
        $prepared = @(@($done.services) | Where-Object { $_ } | ForEach-Object name | Sort-Object) -join ','
        if ($wanted -cne $prepared) { throw 'Prepared service scope changed; inspect conflicts before rerunning setup without -RotateKeys.' }
        $servicesChanged = $true
        Say-Handbook $done.handbook
        foreach ($name in $done.removals) { $config['servers'].Remove($name); $write = $true }
        foreach ($s in $done.services) {
            if ($s.entry -and $s.entry_changes) { $config['servers'][$s.name] = $s.entry; $write = $true }
        }
        if ($RotateKeys -and $write) {
            Assert-McpUnchanged
            Write-McpJson $config
            $clientChanged = $true
            $write = $false
        }
        # Rotation publishes the replacement client before restarting selected tasks.
        # Ordinary installs retain readiness-before-publication.
        # The gateway last: it starts with the prepared backend credentials.
        foreach ($s in $done.services) {
            Register-ServiceTask $s
            if (-not (Wait-ServiceReady $s)) { throw (Service-StartFailure $s) }
            Say "  $($s.name): running on $($s.url)" Green
        }
        Say "  Private client.headers.json files own the bearers; .mcp.json and Ferry use those same keys."
    }
    if ($write) {
        Assert-McpUnchanged
        Write-McpJson $config
        $clientChanged = $true
    }
    if ($servicesChanged) {
        Assert-McpUnchanged
    }
    if ($clientChanged) { Say "  Restart Visual Studio (or reload MCP servers) to pick them up." Green }
    elseif ($servicesChanged) { Say "  .mcp.json unchanged; no backup or write needed." Green }
    Say "Done. Undo everything: .\setup.ps1 -Uninstall" Cyan
} catch {
    Say "  Setup did not complete. Resolve the named failure and rerun without -RotateKeys to reuse the retained bearer." Yellow
    throw
}
