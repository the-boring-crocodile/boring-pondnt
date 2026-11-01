<#
.SYNOPSIS
    Set up pondNT on this D365 dev box for Copilot in Visual Studio: look first, then change on "yes".

.DESCRIPTION
    Run from the unpacked release folder (Windows PowerShell 5.1 or 7), as Administrator
    (the -WhatIf preview runs without):

        .\setup.ps1              look, show the plan, ask before each change
        .\setup.ps1 -WhatIf      look and show the plan only; changes nothing
        .\setup.ps1 -Yes         apply the plan without asking (never touches Defender without -AddDefenderExclusion)
        .\setup.ps1 -Uninstall   stop and remove pondNT's scheduled tasks and its .mcp.json entries (asks first;
                                 data folders stay); offer to remove the Defender exclusions; list what else to remove
        .\setup.ps1 -Gateway all put the services (not the core, not Knowledge) behind one entry, boring-gateway

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
      - optional, -Gateway: boring-gateway (a boring-mcp-ferry, SYSTEM at boot, 127.0.0.1 with a key) in
        place of the chosen services' own entries: Copilot sees its four tools (service_list,
        service_describe, service_read, service_call) instead of every service's. 'all' = every chosen
        service but the core and Knowledge; or a list, e.g. 'd365,canvas'; 'none' takes them back out;
        'keep' (default) keeps what is behind it now. The services keep their tasks, ports and keys.
      - Defender: when it scans pondNT's python, setup ASKS (default no) whether to exclude this release's
        python.exe and pythonw.exe; -AddDefenderExclusion plans it. -Yes alone never does it. That is
        dangerous -- read INSTALL.md, step 2. -Uninstall offers to remove those exclusions again.
      - the release folder's permissions: its python runs as SYSTEM and elevated, so only administrators
        and SYSTEM may change or add files there (Users read and run). An unpacked zip inherits its
        parent's permissions, and C:\ lets every user add files to new folders; setup takes this folder
        off inheritance once and checks every file below it (nothing else on the machine).
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
    [switch]$SkipServices,
    [string]$Gateway = 'keep'           # keep | none | all | services behind boring-gateway, e.g. 'd365,canvas'
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
# The release folder runs as SYSTEM (services) and elevated (core, D365): whoever may add a
# file under it (a sitecustomize.py, a .pth) runs code there. Trusted to change it:
# Administrators, SYSTEM, TrustedInstaller. Anything else that may write, append, delete or
# change permissions is reported; inherited entries below the root are judged at the root.
$trustedSids = @('S-1-5-32-544', 'S-1-5-18', 'S-1-5-80-956008885-3418522649-1831038044-1853292631-2271478464')
$writeMask = [long](0x2 -bor 0x4 -bor 0x10 -bor 0x100 -bor 0x10000 -bor 0x40 -bor 0x40000 -bor 0x80000 -bor 0x10000000 -bor 0x40000000)
function Release-OpenWrites {
    $items = @(Get-Item -LiteralPath $root -Force) + @(Get-ChildItem -LiteralPath $root -Recurse -Force -ErrorAction Stop)
    foreach ($i in $items) {
        $rules = (Get-Acl -LiteralPath $i.FullName).GetAccessRules($true, $true, [Security.Principal.SecurityIdentifier])
        foreach ($r in $rules) {
            if ($r.AccessControlType -ne 'Allow') { continue }
            if ($i.FullName -ne $root -and $r.IsInherited) { continue }
            if ($trustedSids -contains $r.IdentityReference.Value) { continue }
            if (([long]$r.FileSystemRights) -band $writeMask) {
                "$($i.FullName): $($r.IdentityReference.Value) $($r.FileSystemRights)"
            }
        }
    }
}
function Lock-ReleaseFolder {
    # Off inheritance; Administrators and SYSTEM full, Users read and run; the files below inherit it.
    $out = & icacls.exe $root /inheritance:r /grant:r '*S-1-5-32-544:(OI)(CI)F' '*S-1-5-18:(OI)(CI)F' '*S-1-5-32-545:(OI)(CI)RX' /Q 2>&1
    if ($LASTEXITCODE) { throw "icacls could not set the release folder's permissions: $out" }
    $left = @(Release-OpenWrites)
    if ($left) {
        $left | Select-Object -First 10 | ForEach-Object { Say "    still open: $_" Red }
        throw "entries setup did not set still let others change the release folder; remove them (icacls PATH /remove:g SID) and run setup again"
    }
    Say "  release folder: only administrators and SYSTEM may change it now" Green
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
    # Uninstall and finalize read what is behind the gateway from its ferry.json.
    if ($Gateway -and -not $Remove -and -not $Finalize) { $a += @('--gateway', $Gateway) }
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
function Service-Processes($item, [string]$executable) {
    if ($item.account -eq 'user') {
        $tails = @(' ' + (Service-UserArguments $item))
        return @(Get-CimInstance Win32_Process -Filter "Name='pythonw.exe'" -ErrorAction Stop |
            Where-Object { $line = "$($_.CommandLine)"; $_.ExecutablePath -eq $executable -and
                @($tails | Where-Object { $line.EndsWith($_, [StringComparison]::Ordinal) }).Count })
    }
    $command = Service-ProcessCommand $item $executable
    $image = Quote $executable
    # cmd.exe adds separator whitespace after the image and at the end. Keep every argument exact.
    $pattern = '^' + [regex]::Escape($image) + '[ \t]+' +
        [regex]::Escape($command.Substring($image.Length + 1)) + '[ \t]*$'
    return @(Get-CimInstance Win32_Process -Filter "Name='python.exe'" -ErrorAction Stop |
        Where-Object { $_.ExecutablePath -eq $executable -and $_.CommandLine -cmatch $pattern })
}
function Stop-CheckedProcess($observed) {
    $problems = @()
    $process = Get-Process -Id $observed.ProcessId -ErrorAction SilentlyContinue -ErrorVariable problems
    if (@($problems | Where-Object { $_.CategoryInfo.Category -ne 'ObjectNotFound' }).Count) {
        throw "Cannot inspect selected service process $($observed.ProcessId); not stopped."
    }
    if (-not $process) { return }
    try {
        $null = $process.Handle  # retain this process, not a later reuse of its PID
        # CIM creation timestamps have microsecond precision; Process exposes 100 ns ticks.
        if (-not $observed.CreationDate -or $process.Path -ine $observed.ExecutablePath -or
            [Math]::Abs(($process.StartTime.ToUniversalTime() - $observed.CreationDate.ToUniversalTime()).Ticks) -gt 9) {
            throw "Selected service process identity changed; not stopped."
        }
        $process.Kill()
        if (-not $process.WaitForExit(10000)) { throw "Selected service process did not exit within 10 seconds." }
    } finally { $process.Dispose() }
}
function Stop-ServiceProcesses($item, [string]$executable) {
    foreach ($observed in @(Service-Processes $item $executable)) { Stop-CheckedProcess $observed }
}
function Service-Ports($item) {
    $ports = @(([Uri]$item.url).Port)
    if ($item.module -eq 'boring_canvas.server') {
        $at = [array]::IndexOf(@($item.arguments), '--port')
        if ($at -lt 0) { throw 'Canvas web port is absent from the service plan.' }
        $ports += [int]$item.arguments[$at + 1]
    }
    return @($ports | Sort-Object -Unique)
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
function Test-ServiceLogFree($item) {
    if (-not (Test-Path -LiteralPath $item.log -ErrorAction Stop)) { return $true }
    try {
        $handle = [IO.File]::Open($item.log, [IO.FileMode]::Open, [IO.FileAccess]::Read, [IO.FileShare]::None)
        $handle.Dispose()
        return $true
    } catch [IO.IOException] { return $false }
}
function Assert-ServiceHandover($item) {
    $task = Checked-ServiceTask $item
    $listeners = @(Service-Listeners $item)
    if (-not $listeners) { return }
    $ownedIds = @()
    if ($task) {
        $ownedIds = @(Service-Processes $item (Owned-TaskPython $item $task) | ForEach-Object ProcessId)
    }
    if (@($listeners | Where-Object { $_.OwningProcess -notin $ownedIds }).Count) {
        throw "$($item.name): a required port has an unrelated listener; retained. Stop it explicitly before setup."
    }
}
function Wait-ServiceStopped($item, [int]$seconds = 30) {
    $clock = [Diagnostics.Stopwatch]::StartNew()
    do {
        $task = Checked-ServiceTask $item
        $busy = $task -and "$($task.State)" -in @('Running','Queued','4','2')
        $listeners = @(Service-Listeners $item)
        $logFree = Test-ServiceLogFree $item
        if (-not $busy -and -not $listeners -and $logFree) { return }
        if ($clock.Elapsed.TotalSeconds -ge $seconds) { break }
        Start-Sleep -Milliseconds 250
    } while ($true)
    throw "$($item.name): old task/process resources did not settle; task=$($task.State), occupied ports=$(@($listeners | ForEach-Object LocalPort) -join ','), log free=$logFree. Setup stopped."
}
function Stop-ServiceTask($item) {
    $task = Checked-ServiceTask $item
    if ($task) {
        $oldPython = Owned-TaskPython $item $task
        Stop-ScheduledTask -TaskPath '\pondNT\' -TaskName $item.task -ErrorAction Stop
        Stop-ServiceProcesses $item $oldPython
    }
    Wait-ServiceStopped $item
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
            elseif (@($kit.differs).Count) {
                Say "    The handbook: $($kit.folder) kept; differs from this release's handbook in: $(@($kit.differs) -join ', ')" Yellow
                Say "      review and merge deliberately: compare $($kit.release) with it (setup never overwrites it)" Yellow
            } else { Say "    The handbook: $($kit.folder) kept; same as this release's handbook" }
        }
        'no-release-kit' { Say "    The handbook: this release has none at $($kit.release)" Yellow }
    }
}
function Wait-ServiceReady($item, [int]$seconds = 30) {
    $clock = [Diagnostics.Stopwatch]::StartNew()
    $ports = @(Service-Ports $item)
    do {
        $ownedIds = @(Service-Processes $item $item.python | ForEach-Object ProcessId)
        $listeners = @(Service-Listeners $item)
        $missing = @($ports | Where-Object { $_ -notin @($listeners.LocalPort) })
        $foreign = @($listeners | Where-Object { $_.OwningProcess -notin $ownedIds })
        $task = Checked-ServiceTask $item
        $running = $task -and "$($task.State)" -in @('Running','4')
        if ($running -and -not $missing -and -not $foreign -and $listeners.Count -ge $ports.Count) { return $true }
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
    return "$($item.name): required listeners were not served by the exact planned process; $detail. See $($item.log). Client publication and key retirement stopped."
}
function Assert-McpUnchanged {
    $current = $null
    if (Test-Path -LiteralPath $McpJson -ErrorAction Stop) {
        $current = (Get-FileHash -Algorithm SHA256 -LiteralPath $McpJson).Hash.ToLowerInvariant()
    }
    if ($current -cne $script:McpReadSha) { throw 'Client configuration changed during setup; inspect before retrying.' }
}
function PowerShell-Literal([string]$value) { "'" + $value.Replace("'", "''") + "'" }
function Save-SetupAttempt {
    $json = $script:SetupAttempt | ConvertTo-Json -Depth 10
    $a = @('-B', '-m', 'boring_pondnt.publish_config', '--path', $script:SetupAttemptPath)
    if ($script:SetupAttemptSha) { $a += @('--expected-sha', $script:SetupAttemptSha) }
    $OutputEncoding = New-Object Text.UTF8Encoding $false
    $json | & $python @a
    if ($LASTEXITCODE) { throw 'Cannot persist setup recovery record; setup stopped.' }
    $script:SetupAttemptSha = (Get-FileHash -Algorithm SHA256 -LiteralPath $script:SetupAttemptPath).Hash.ToLowerInvariant()
}
function Start-SetupAttempt($selected, [string]$operation = 'apply') {
    $folder = Join-Path $stateRoot 'setup-attempts'
    $null = New-Item -ItemType Directory -Path $folder -Force
    $script:SetupAttemptPath = Join-Path $folder ("$([guid]::NewGuid().ToString('N')).json")
    $script:SetupAttemptSha = $null
    $only = @($selected.services | Where-Object { $_.module -ne 'boring_mcp_ferry.server' } |
        ForEach-Object { Split-Path -Leaf $_.folder } | Select-Object -Unique) -join ','
    $gated = @($selected.services | Where-Object { $_.behind } |
        ForEach-Object { Split-Path -Leaf $_.folder }) -join ','
    if (-not $gated) { $gated = 'none' }
    $retry = '& ' + (PowerShell-Literal (Join-Path $root 'setup.ps1'))
    if ($operation -eq 'uninstall') { $retry += ' -Uninstall' }
    else { $retry += ' -SkipIndex -Services ' + (PowerShell-Literal $only) + ' -Gateway ' + (PowerShell-Literal $gated) }
    if ($Index) { $retry += ' -Index ' + (PowerShell-Literal $Index) }
    $retry += ' -McpJson ' + (PowerShell-Literal $McpJson)
    $script:SetupAttempt = [ordered]@{
        schema = 1; operation = $operation; status = 'in_progress'; phase = 'preparation'
        release = $root; state = $stateRoot; client = $McpJson; client_sha_before = $script:McpReadSha
        services = $only; gateway = $gated; index = $Index
        started_utc = [DateTime]::UtcNow.ToString('o'); current_service = $null
        started_services = @(); retired_services = @()
        retry_preview = $retry + ' -WhatIf'; retry_apply = $retry
    }
    Save-SetupAttempt
    Say "  Recovery record: $script:SetupAttemptPath"
}
function Set-SetupPhase([string]$phase, [string]$service = '') {
    $script:SetupAttempt['phase'] = $phase
    $script:SetupAttempt['current_service'] = $service
    Save-SetupAttempt
}
function Fail-SetupAttempt {
    if ($script:SetupAttempt) {
        $script:SetupAttempt['status'] = 'failed'
        try { Save-SetupAttempt } catch { Say '  Recovery record could not be updated; inspect its last saved phase.' Yellow }
        Say "  Setup did not complete. Recovery record: $script:SetupAttemptPath" Yellow
        Say "  Preview the recorded retry: $($script:SetupAttempt.retry_preview)" Yellow
    }
}
function Complete-SetupAttempt {
    $script:SetupAttempt['status'] = 'completed'
    $script:SetupAttempt['phase'] = 'completed'
    $script:SetupAttempt['completed_utc'] = [DateTime]::UtcNow.ToString('o')
    Save-SetupAttempt
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
        if ($svc.handbook) { Say "  The handbook stays in $($svc.handbook) (yours; remove it by hand if you want it gone)." }
        if (Ask "Stop and remove them (their data folders stay)?") {
            $script:SetupAttempt = $null
            try {
                Assert-McpUnchanged
                Start-SetupAttempt $svc 'uninstall'
                foreach ($t in $tasks) {
                    Set-SetupPhase 'remove-task' $t.item.name
                    Stop-ServiceTask $t.item
                    Unregister-ScheduledTask -TaskPath '\pondNT\' -TaskName $t.item.task -Confirm:$false -ErrorAction Stop
                    $script:SetupAttempt['retired_services'] += $t.item.name
                    Save-SetupAttempt
                }
                if ($svc.removals) {
                    Set-SetupPhase 'publish-client'
                    Assert-McpUnchanged
                    foreach ($k in $svc.removals) { $config['servers'].Remove($k) }
                    Write-McpJson $config
                }
                Complete-SetupAttempt
            } catch {
                Fail-SetupAttempt
                throw
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
    foreach ($r in $svcPlan.retire) { $null = Checked-ServiceTask $r }
    if ($svcPlan.removals) { $plan += ".mcp.json entries removed: $(@($svcPlan.removals) -join ', ') (reached through boring-gateway, or the gateway retired)" }
    if ($svcPlan.retire) { $plan += "retire: $(@($svcPlan.retire | ForEach-Object { $_.task }) -join ', ') (-Gateway none: nothing behind it)" }
    if ($svcPlan.services) {
        $plan += "services on 127.0.0.1 with access keys: $(@($svcPlan.services | ForEach-Object { $_.name }) -join ', '); started by Task Scheduler \pondNT\ (services: SYSTEM at boot; core: you at logon); data in $stateRoot\services"
        foreach ($s in $svcPlan.services) {
            $what = if ($s.new_key) { 'new key' } else { 'key kept' }
            $runs = if ($s.account -eq 'user') { "runs as $(Current-User) at logon" } else { 'runs as SYSTEM at boot' }
            $via = if ($s.behind) { "; Copilot reaches it through $($s.behind)" } else { '' }
            Say "    $($s.name) -> $($s.url) ($what, person $($s.subject); $runs$via)"
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
$openWrites = @()
if (-not $SkipServices) {
    $openWrites = @(Release-OpenWrites)
    if ($openWrites) {
        $plan += "release folder permissions: only administrators and SYSTEM may change it (its python runs as SYSTEM and elevated; now $($openWrites.Count) entries let others add or change files)"
        $openWrites | Select-Object -First 5 | ForEach-Object { Say "    open: $_" Yellow }
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
        if ($LASTEXITCODE) { throw 'Source index build failed; no service configuration was changed.' }
    }
}
if ($openWrites) {
    if (Ask "Allow only administrators and SYSTEM to change this release folder (its python runs as SYSTEM and elevated)?") {
        Lock-ReleaseFolder
    } elseif ($svcPlan -and $svcPlan.services) {
        Say "  Services not set up: they would run as SYSTEM and elevated from a folder others may add files to." Yellow
        $svcPlan = $null
    }
}
$write = $false
$servicesChanged = $false
$script:SetupAttempt = $null
try {
    if ($svcPlan -and ($svcPlan.services -or $svcPlan.retire) -and (Ask "Set up the services (configs, keys, boot tasks, .mcp.json entries)?")) {
        foreach ($item in @($svcPlan.services) + @($svcPlan.retire)) { if ($item) { Assert-ServiceHandover $item } }
        Assert-McpUnchanged
        Start-SetupAttempt $svcPlan
        $done = Services-Plan $config -Apply
        $wanted = @(@($svcPlan.services) + @($svcPlan.retire) | Where-Object { $_ } | ForEach-Object name | Sort-Object) -join ','
        $prepared = @(@($done.services) + @($done.retire) | Where-Object { $_ } | ForEach-Object name | Sort-Object) -join ','
        if ($wanted -cne $prepared) { throw 'Prepared service scope changed; inspect conflicts and recovery record before retrying.' }
        $servicesChanged = $true
        Say-Handbook $done.handbook
        foreach ($r in $done.retire) {
            Set-SetupPhase 'retire-task' $r.name
            $task = Checked-ServiceTask $r
            if ($task) {
                Stop-ServiceTask $r
                Unregister-ScheduledTask -TaskPath '\pondNT\' -TaskName $r.task -Confirm:$false -ErrorAction Stop
                $script:SetupAttempt['retired_services'] += $r.name
                Save-SetupAttempt
                Say "  $($r.name): retired" Green
            }
        }
        foreach ($name in $done.removals) { $config['servers'].Remove($name); $write = $true }
        # The gateway last: it starts with the prepared backend credentials.
        foreach ($s in $done.services) {
            Set-SetupPhase 'start-service' $s.name
            Register-ServiceTask $s
            if (-not (Wait-ServiceReady $s)) { throw (Service-StartFailure $s) }
            $script:SetupAttempt['started_services'] += $s.name
            Save-SetupAttempt
            Say "  $($s.name): running on $($s.url)" Green
            if ($s.entry -and $s.entry_changes) { $config['servers'][$s.name] = $s.entry; $write = $true }
        }
        Say "  Access keys stay private in .mcp.json or Ferry's headers files; each bearer acts with its service role."
    }
    if ($write) {
        Set-SetupPhase 'publish-client'
        Assert-McpUnchanged
        Write-McpJson $config
    }
    if ($servicesChanged) {
        Assert-McpUnchanged
        Set-SetupPhase 'finalize-keys-and-routing'
        $null = Services-Plan $config -Finalize
        Complete-SetupAttempt
    }
    if ($write) { Say "  Restart Visual Studio (or reload MCP servers) to pick them up." Green }
    elseif ($servicesChanged) { Say "  .mcp.json unchanged; no backup or write needed." Green }
    Say "Done. Undo everything: .\setup.ps1 -Uninstall" Cyan
} catch {
    Fail-SetupAttempt
    throw
}
