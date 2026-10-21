param([Parameter(Mandatory)][string]$Helper, [Parameter(Mandatory)][string]$Fixture)
$ErrorActionPreference = 'Stop'
$env:COMPUTERNAME = 'synthetic-fixture'
. $Helper
function Require($Condition,[string]$Message) { if (-not $Condition) { throw $Message } }
function Refuses([scriptblock]$Action) {
    try { & $Action; throw 'Expected refusal did not occur.' }
    catch { if ($_.Exception.Message -eq 'Expected refusal did not occur.') { throw } }
}
function New-FakeProcess([int]$Number) {
    $p = [pscustomobject]@{Id=$Number; Handle=99; StartTime=[datetime]'2026-10-12T00:00:00Z'; HasExited=$false}
    $p | Add-Member ScriptMethod Kill { $script:kills++; $this.HasExited=$true }
    $p | Add-Member ScriptMethod WaitForExit { param($Wait); return $script:exitConfirmed }
    return $p
}
function Get-Process { [CmdletBinding()]param([int]$Id); return $script:fakeProcess }
function Get-CimInstance { [CmdletBinding()]param($ClassName,[string]$Filter); return $script:cim }
function Start-Process {
    [CmdletBinding()]param($FilePath,$ArgumentList,$WorkingDirectory,[switch]$PassThru,$WindowStyle,$RedirectStandardError)
    $script:starts++
    $script:fakeProcess = New-FakeProcess 42
    $script:cim = [pscustomobject]@{ExecutablePath=$FilePath; CommandLine=('"' + $FilePath + '" ' + $ArgumentList + $script:launchSuffix)}
    return $script:fakeProcess
}
$script:kills=0; $script:starts=0; $script:exitConfirmed=$true; $script:launchSuffix=''
$file = Join-Path $Fixture 'processes.json'
$record = Read-DevboxRecord $file
$entry = Start-DevboxEntry 'devbox' 'C:\fixture release\python\python.exe' @('-m','boring_pondnt.server','--state-dir','C:\fixture state') $Fixture $file $record
Require ($script:starts -eq 1 -and $entry.state -eq 'running') 'Start not recorded.'
$loaded = Read-DevboxRecord $file
Require ($loaded.entries[0].pid -eq 42) 'Receipt did not reload.'
$original = $script:fakeProcess.StartTime
$script:fakeProcess.StartTime = $original.AddSeconds(1)
Refuses { Stop-DevboxEntry $loaded.entries[0] $file $loaded }
Require ($script:kills -eq 0) 'Reused PID was killed.'
$script:fakeProcess.StartTime = $original
$script:cim.CommandLine += ' unexpected'
Refuses { Stop-DevboxEntry $loaded.entries[0] $file $loaded }
Require ($script:kills -eq 0) 'Changed command was killed.'
$script:cim.CommandLine = $entry.commandline
$script:cim.ExecutablePath = 'C:\unrelated\python.exe'
Refuses { Stop-DevboxEntry $loaded.entries[0] $file $loaded }
Require ($script:kills -eq 0) 'Changed executable was killed.'
$script:cim.ExecutablePath = $entry.executable
Stop-DevboxEntry $loaded.entries[0] $file $loaded
Require ($script:kills -eq 1 -and (Read-DevboxRecord $file).entries[0].state -eq 'stopped') 'Verified stop not recorded.'
'PASS: PID reuse, changed executable/command refuse; only recorded exact process stops.'

$file = Join-Path $Fixture 'missing.json'
$script:fakeProcess = $null
$record = [pscustomobject]@{schema=1;owner='pondnt-devbox';machine=$env:COMPUTERNAME;entries=@(
    [pscustomobject]@{name='devbox';state='running';pid=11;created='123';executable='C:\fixture.exe';commandline='"C:\fixture.exe"'}
)}
Stop-DevboxEntry $record.entries[0] $file $record
Require ($script:kills -eq 1 -and $record.entries[0].state -eq 'stopped') 'Absent process must not trigger broad cleanup.'
$record.entries[0].state='starting'
Refuses { Stop-DevboxEntry $record.entries[0] $file $record }
Refuses { Start-DevboxEntry 'devbox' 'C:\fixture.exe' @('x') $Fixture $file $record }
Require ($script:starts -eq 1 -and $script:kills -eq 1) 'Unknown start must not be retried or guessed.'
'PASS: absent recorded process settles; unknown start refuses replay and guessed cleanup.'

$file = Join-Path $Fixture 'failed-stop.json'
$record = Read-DevboxRecord $file
$entry = Start-DevboxEntry 'tunnel' 'C:\fixture\ssh.exe' @('-N','fixture@host') $Fixture $file $record
$script:exitConfirmed=$false
Refuses { Stop-DevboxEntry $entry $file $record }
Require ((Read-DevboxRecord $file).entries[0].state -eq 'stopping') 'Unconfirmed stop became terminal.'
$script:exitConfirmed=$true
$script:fakeProcess=$null
Stop-DevboxEntry $entry $file $record
Require ((Read-DevboxRecord $file).entries[0].state -eq 'stopped') 'Later observed exit did not settle.'
'PASS: stop deadline retains uncertainty; later observed absence settles without replay.'

foreach ($suffix in @(' ', "`t ")) {
    $script:launchSuffix = $suffix
    $file = Join-Path $Fixture ('separators-' + [guid]::NewGuid().ToString('N') + '.json')
    $record = Read-DevboxRecord $file
    $entry = Start-DevboxEntry 'devbox' 'C:\fixture\python.exe' @('-m','fixture','argument with trailing space ') $Fixture $file $record
    Require ($entry.commandline -ceq $script:cim.CommandLine) 'Observed launch string was normalized in the receipt.'
    Require ($entry.commandline.EndsWith($suffix)) 'Launch separators not retained.'
    $loaded = Read-DevboxRecord $file
    $beforeKills = $script:kills
    $script:cim.CommandLine += ' '
    Refuses { Stop-DevboxEntry $loaded.entries[0] $file $loaded }
    Require ($script:kills -eq $beforeKills) 'Later command change bypassed exact recorded identity.'
    $script:cim.CommandLine = $entry.commandline
    Stop-DevboxEntry $loaded.entries[0] $file $loaded
    Require ((Read-DevboxRecord $file).entries[0].state -eq 'stopped') 'Observed launch string did not permit exact stop.'
}
foreach ($suffix in @(' unexpected', [string][char]160)) {
    $script:launchSuffix = $suffix
    $file = Join-Path $Fixture ('invalid-launch-' + [guid]::NewGuid().ToString('N') + '.json')
    $record = Read-DevboxRecord $file
    $beforeKills = $script:kills
    Refuses { Start-DevboxEntry 'devbox' 'C:\fixture\python.exe' @('-m','fixture') $Fixture $file $record }
    Require ($script:kills -eq ($beforeKills + 1)) 'Rejected launch child was not stopped.'
    Require ((Read-DevboxRecord $file).entries[0].state -eq 'starting') 'Rejected launch lost its pending record.'
}
$script:launchSuffix = ''
'PASS: launch separators retained exactly; changed commands and non-ASCII suffixes refuse.'

$quoted = Quote-DevboxArgument 'C:\path with space\'
Require ($quoted -ceq '"C:\path with space\\"') 'Trailing-backslash quoting failed.'
Refuses { Quote-DevboxArgument ("bad" + [char]10 + "argument") }
'PASS: argument quoting and invalid-input refusal; synthetic APIs only.'
