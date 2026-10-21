# Start/stop only the exact processes this helper owns. Optional Pond tunnel.
[CmdletBinding()]
param([string]$Release, [string[]]$Only, [switch]$Stop, [string]$CiRoot='C:\temp\ci',
      # Needed only for the tunnel: account@host of the tunnel endpoint and IP:PORT of the control plane there.
      [string]$TunnelHost, [string]$ControlPlane)
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'maintenance-state.ps1')

function Assert-PrivateDevboxCredentials([string]$Root) {
    # Provisioning owns these credentials. Validate before any process changes;
    # never silently rewrite operator ACLs or print credential contents.
    $current = [Security.Principal.WindowsIdentity]::GetCurrent().User.Value
    $allowed = @($current, 'S-1-5-18', 'S-1-5-32-544')
    $items = @((Get-Item -Force -LiteralPath $Root -ErrorAction Stop))
    $items += @(Get-ChildItem -Force -LiteralPath $Root -Recurse -ErrorAction Stop)
    foreach ($item in $items) {
        if ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) {
            throw 'Credential tree has a reparse point; provision a private ordinary directory.'
        }
        $acl = Get-Acl -LiteralPath $item.FullName
        $owner = ([Security.Principal.NTAccount]$acl.Owner).Translate([Security.Principal.SecurityIdentifier]).Value
        if ($owner -notin $allowed) { throw 'Credential tree has an unexpected owner.' }
        foreach ($rule in $acl.GetAccessRules($true, $true, [Security.Principal.SecurityIdentifier])) {
            if ($rule.AccessControlType -eq [Security.AccessControl.AccessControlType]::Allow -and
                $rule.IdentityReference.Value -notin $allowed) {
                throw 'Credential tree grants access outside the current administrator, SYSTEM and Administrators; provision private ACLs before starting.'
            }
        }
    }
}

function Quote-DevboxArgument([string]$Value) {
    if ($Value.Contains([char]0) -or $Value.Contains([char]10) -or $Value.Contains([char]13)) {
        throw 'Command arguments must not contain NUL or newlines.'
    }
    return '"' + (($Value -replace '(\\*)"', '$1$1\"') -replace '(\\+)$', '$1$1') + '"'
}

function Devbox-Command([string]$Executable, [string[]]$Arguments) {
    return (Quote-DevboxArgument $Executable) + ' ' + (($Arguments | ForEach-Object { Quote-DevboxArgument $_ }) -join ' ')
}

function Read-DevboxRecord([string]$Path) {
    $data = Read-MaintenanceRecord $Path 'pondnt-devbox'
    $seen = @{}
    foreach ($entry in @($data.entries)) {
        if (@($entry.PSObject.Properties.Name).Count -ne 6 -or
            $entry.name -notin @('devbox','devbox-d365','tunnel') -or
            $entry.state -notin @('starting','running','stopping','stopped') -or
            $entry.executable -isnot [string] -or -not $entry.executable -or
            $entry.commandline -isnot [string] -or -not $entry.commandline -or
            ($entry.pid -isnot [int] -and $entry.pid -isnot [long]) -or $entry.pid -lt 0 -or $entry.pid -gt [int]::MaxValue -or
            $entry.created -isnot [string] -or
            ($entry.state -ne 'starting' -and ($entry.pid -lt 1 -or $entry.created -notmatch '^[0-9]+$'))) {
            throw 'Invalid process ownership entry.'
        }
        if ($seen.ContainsKey($entry.name)) { throw 'Duplicate process ownership entry.' }
        $seen[$entry.name] = $true
    }
    return $data
}

function Get-DevboxProcess([int]$ProcessId) {
    $errors = @()
    $process = Get-Process -Id $ProcessId -ErrorAction SilentlyContinue -ErrorVariable errors
    foreach ($error in $errors) {
        if ($error.CategoryInfo.Category -ne 'ObjectNotFound') { throw $error }
    }
    return $process
}

function Assert-DevboxIdentity($Entry, $Process) {
    $null = $Process.Handle # retain a handle to this process, rather than later targeting a reused PID
    if ($Process.StartTime.ToUniversalTime().Ticks.ToString() -cne $Entry.created) {
        throw 'Recorded PID now belongs to another process; retained.'
    }
    $actual = Get-CimInstance Win32_Process -Filter ("ProcessId=" + $Entry.pid) -ErrorAction Stop
    if (-not $actual -or
        -not [string]::Equals($actual.ExecutablePath, $Entry.executable, [StringComparison]::OrdinalIgnoreCase) -or
        $actual.CommandLine -cne $Entry.commandline) {
        throw 'Recorded executable/command changed or could not be inspected; retained.'
    }
}

function Stop-DevboxEntry($Entry, [string]$RecordPath, $Record) {
    if ($Entry.state -eq 'starting') { throw 'Earlier start has an unknown outcome; inspect it before restarting.' }
    if ($Entry.state -eq 'stopped') { return }
    $process = Get-DevboxProcess $Entry.pid
    if ($process) {
        Assert-DevboxIdentity $Entry $process
        $Entry.state = 'stopping'
        Write-MaintenanceRecord $RecordPath $Record
        $process.Kill()
        if (-not $process.WaitForExit(10000)) { throw 'Owned process exit remains unconfirmed.' }
    }
    $Entry.state = 'stopped'
    Write-MaintenanceRecord $RecordPath $Record
}

function Start-DevboxEntry([string]$Name, [string]$Executable, [string[]]$Arguments,
                           [string]$WorkingDirectory, [string]$RecordPath, $Record,
                           [string]$ErrorLog='') {
    if (@($Record.entries | Where-Object { $_.name -eq $Name -and $_.state -ne 'stopped' }).Count) {
        throw 'Existing process ownership is unresolved.'
    }
    $expected = Devbox-Command $Executable $Arguments
    $entry = [pscustomobject]@{name=$Name; state='starting'; pid=0; created=''; executable=$Executable; commandline=$expected}
    $Record.entries = @($Record.entries | Where-Object { $_.name -ne $Name }) + @($entry)
    Write-MaintenanceRecord $RecordPath $Record # reserve before launch; interrupted starts stay visible
    $process = $null
    try {
        $options = @{FilePath=$Executable; ArgumentList=(($Arguments | ForEach-Object { Quote-DevboxArgument $_ }) -join ' ');
                     WorkingDirectory=$WorkingDirectory; PassThru=$true; ErrorAction='Stop'}
        if ($ErrorLog) { $options.WindowStyle='Hidden'; $options.RedirectStandardError=$ErrorLog }
        $process = Start-Process @options
        $null = $process.Handle
        $actual = Get-CimInstance Win32_Process -Filter ("ProcessId=" + $process.Id) -ErrorAction Stop
        $bare = $Executable + ' ' + $options.ArgumentList
        # Native Start-Process can append separators after our final quoted argument.
        # Permit only ASCII space/tab here; record the observed string unchanged.
        # Subsequent identity checks still require the exact recorded command.
        $launchCommand = ([string]$actual.CommandLine).TrimEnd([char[]]" `t")
        if (-not $actual -or
            -not [string]::Equals($actual.ExecutablePath, $Executable, [StringComparison]::OrdinalIgnoreCase) -or
            ($launchCommand -cne $expected -and $launchCommand -cne $bare)) {
            throw 'Started process identity could not be verified.'
        }
        $entry.pid = [int]$process.Id
        $entry.created = $process.StartTime.ToUniversalTime().Ticks.ToString()
        $entry.commandline = $actual.CommandLine
        $entry.state = 'running'
        Write-MaintenanceRecord $RecordPath $Record
        return $entry
    } catch {
        # The direct handle from our own Start-Process is evidence of ownership.
        # If recording fails, stop that child; the on-disk pending entry still requires review.
        if ($process -and -not $process.HasExited) {
            $process.Kill()
            if (-not $process.WaitForExit(10000)) { throw 'Start failed and child exit remains unconfirmed; inspect record.' }
        }
        throw
    }
}

if ($MyInvocation.InvocationName -eq '.') { return } # fixture loads functions only

$ci = [IO.Path]::GetFullPath($CiRoot)
$recordPath = Join-Path $ci 'devbox-processes.json'
if (-not $Stop) { Assert-PrivateDevboxCredentials $ci }
$lock = Open-MaintenanceLock $recordPath
try {
    $record = Read-DevboxRecord $recordPath
    $names = @('devbox','devbox-d365')
    if ($Only) {
        $names = @($Only | ForEach-Object { $_ -split ',' } | ForEach-Object { $_.Trim() } | Where-Object { $_ })
        if (@($names | Where-Object { $_ -notin @('devbox','devbox-d365') }).Count) { throw 'Unknown -Only service.' }
    } else { $names += 'tunnel' }
    $selected = @($record.entries | Where-Object { $_.name -in $names })
    # Establish every selected ownership proof before stopping any process.
    foreach ($entry in $selected) {
        if ($entry.state -eq 'starting') { throw 'Unresolved earlier start; review process record.' }
        if ($entry.state -ne 'stopped') {
            $process = Get-DevboxProcess $entry.pid
            if ($process) { Assert-DevboxIdentity $entry $process }
        }
    }
    if (-not $Stop) {
        if (-not $Release) { throw '-Release is required when starting.' }
        $python = [IO.Path]::GetFullPath((Join-Path $Release 'python\python.exe'))
        if (-not (Test-Path -LiteralPath $python -PathType Leaf)) { throw 'Release has no python.exe.' }
        $sshExecutable = (Get-Command ssh.exe -CommandType Application -ErrorAction Stop).Source
    }
    foreach ($entry in $selected) { Stop-DevboxEntry $entry $recordPath $record }
    if ($Stop) { 'Recorded selected processes stopped; unrecorded processes retained.'; return }
    $env:PYTHONUTF8 = '1'
    if ('tunnel' -in $names) {
        if (-not $TunnelHost -or -not $ControlPlane) { throw 'The tunnel needs -TunnelHost account@host and -ControlPlane IP:PORT.' }
        if ($TunnelHost -notmatch '^[a-z_][a-z0-9_-]*@[A-Za-z0-9.-]+$') { throw '-TunnelHost must be account@host.' }
        if ($ControlPlane -notmatch '^(\d{1,3}(\.\d{1,3}){3}|\[[0-9A-Fa-f:]+\]):\d{1,5}$') { throw '-ControlPlane must be IP:PORT.' }
        foreach ($file in @('ssh\id_ed25519','ssh\known_hosts')) {
            if (-not (Test-Path -LiteralPath (Join-Path $ci $file) -PathType Leaf)) { throw 'Missing operator-provisioned SSH file.' }
        }
        $ssh = @('-F','none','-i',(Join-Path $ci 'ssh\id_ed25519'),'-o','IdentitiesOnly=yes',
                 '-o',('UserKnownHostsFile=' + (Join-Path $ci 'ssh\known_hosts')),
                 '-N','-o','ExitOnForwardFailure=yes','-o','ServerAliveInterval=30',
                 '-R','127.0.0.1:18520:127.0.0.1:18520','-R','127.0.0.1:18522:127.0.0.1:18522',
                 '-L',('127.0.0.1:18503:' + $ControlPlane),$TunnelHost)
        $null = Start-DevboxEntry 'tunnel' $sshExecutable $ssh $ci $recordPath $record
        'Tunnel process started and recorded; network readiness still requires acceptance.'
    }
    foreach ($name in @($names | Where-Object { $_ -ne 'tunnel' })) {
        $id = Join-Path $ci ($name + '-id'); $secret = Join-Path $ci ($name + '-secret')
        if (-not ((Test-Path -LiteralPath $id -PathType Leaf) -and (Test-Path -LiteralPath $secret -PathType Leaf))) {
            "$name has no provisioned credentials; not started."; continue
        }
        $clientId = (Get-Content -Raw -LiteralPath $id).Trim()
        if (-not $clientId) { throw 'Empty client identity file.' }
        $state = Join-Path $ci ('state-' + $name)
        $null = New-Item -ItemType Directory -Force -Path $state
        $port = if ($name -eq 'devbox') {18520} else {18522}
        $module = if ($name -eq 'devbox') {'boring_pondnt.server'} else {'boring_pondnt.d365_server'}
        $arguments = @('-m',$module)
        if ($name -eq 'devbox-d365') { $arguments += 'serve' }
        # A family service of the Pond: the Crocodile's introspection and registration through the
        # tunnel, the Pond "pond" its only caller (boring-service-kit ServiceConfig).
        $service = [ordered]@{
            url = "http://127.0.0.1:$port/mcp"; bind = '127.0.0.1'; port = $port
            oauth = [ordered]@{ issuer = 'http://127.0.0.1:18503'; introspection_url = 'http://127.0.0.1:18503/introspect'
                                client_id = $clientId; secret_file = $secret }
            pond = [ordered]@{ url = 'http://127.0.0.1:18503'; client_id = $clientId; secret_file = $secret }
            access = @([ordered]@{ subject = 'pond'; role = 'writer'; author = 'pond' })
        }
        $config = Join-Path $state 'config.json'
        [IO.File]::WriteAllText($config, ($service | ConvertTo-Json -Depth 4), (New-Object Text.UTF8Encoding $false))
        $arguments += @('--config',$config,'--state-dir',$state)
        $log = Join-Path $state ('server-' + [guid]::NewGuid().ToString('N') + '.log')
        $null = Start-DevboxEntry $name $python $arguments $ci $recordPath $record $log
        "$name process started and recorded; inspect its private log for readiness."
    }
} finally { $lock.Dispose() }
