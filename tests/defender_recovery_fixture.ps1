param([Parameter(Mandatory)][string]$Helper, [Parameter(Mandatory)][string]$Fixture)
$ErrorActionPreference = 'Stop'
$env:COMPUTERNAME = 'synthetic-fixture'
. $Helper

function Require($Condition, [string]$Message) { if (-not $Condition) { throw $Message } }
function Refuses([scriptblock]$Action) {
    try { & $Action; throw 'Expected refusal did not occur.' }
    catch { if ($_.Exception.Message -eq 'Expected refusal did not occur.') { throw } }
}
$script:actualWriter = ${function:Write-MaintenanceRecord}
function Write-MaintenanceRecord([string]$Path, $Record) {
    $script:writes++
    if ($script:writes -eq $script:failWrite) { throw 'synthetic record failure' }
    & $script:actualWriter $Path $Record
}
function Reset-Fake {
    $script:paths = @('pre-existing')
    $script:processes = @()
    $script:adds = 0; $script:removes = 0; $script:writes = 0
    $script:failWrite = -1; $script:failAdd = -1; $script:failRemove = -1
}
function Get-MpPreference {
    [CmdletBinding()]param()
    return [pscustomobject]@{ExclusionPath=$script:paths; ExclusionProcess=$script:processes}
}
function Add-MpPreference {
    [CmdletBinding()]param([string]$ExclusionPath, [string]$ExclusionProcess)
    $script:adds++
    if ($script:adds -eq $script:failAdd) { throw 'synthetic add failure' }
    if ($ExclusionPath) { $script:paths += $ExclusionPath } else { $script:processes += $ExclusionProcess }
}
function Remove-MpPreference {
    [CmdletBinding()]param([string]$ExclusionPath, [string]$ExclusionProcess)
    $script:removes++
    if ($script:removes -eq $script:failRemove) { throw 'synthetic removal failure' }
    if ($ExclusionPath) { $script:paths = @($script:paths | Where-Object { $_ -ne $ExclusionPath }) }
    else { $script:processes = @($script:processes | Where-Object { $_ -ne $ExclusionProcess }) }
}
function Record-For([string]$Name) { Join-Path $Fixture ($Name + '.json') }

Reset-Fake
$file = Record-For 'normal'
Invoke-DefenderChange 'apply' $file -Paths @('pre-existing','added') -Processes @('python.exe')
Invoke-DefenderChange 'apply' $file -Paths @('added')
Require ($script:adds -eq 2) 'Repeated apply must not add again.'
$script:paths += 'unrelated-later'
Invoke-DefenderChange 'undo' $file
Require ($script:removes -eq 2 -and $script:paths -contains 'pre-existing' -and
         $script:paths -contains 'unrelated-later' -and $script:paths -notcontains 'added') 'Undo touched unowned state.'
Require (@((Read-DefenderRecord $file).entries | Where-Object state -ne 'removed').Count -eq 0) 'Undo record not settled.'
'PASS: repeated apply/undo preserves pre-existing and later unrelated exclusions.'

Reset-Fake
$file = Record-For 'partial'
$script:failAdd = 2
Refuses { Invoke-DefenderChange 'apply' $file -Paths @('first','second') }
$data = Read-DefenderRecord $file
Require ($data.entries[0].state -eq 'present' -and $data.entries[1].state -eq 'adding') 'Partial add not recorded.'
Refuses { Invoke-DefenderChange 'undo' $file }
Require ($script:removes -eq 0) 'Uncertain ownership must refuse undo before effects.'
'PASS: partial add is durable; uncertain entry blocks silent undo.'

Reset-Fake
$file = Record-For 'post-effect-record-failure'
$script:failWrite = 3
Refuses { Invoke-DefenderChange 'apply' $file -Paths @('changed') }
Require ($script:paths -contains 'changed') 'Fixture did not reach the effect.'
Require ((Read-DefenderRecord $file).entries[0].state -eq 'adding') 'Lost post-effect uncertainty.'
Refuses { Invoke-DefenderChange 'apply' $file -Paths @('another') }
Require ($script:adds -eq 1) 'Uncertain previous apply admitted another effect.'
'PASS: post-effect record failure retains uncertainty and blocks further changes.'

Reset-Fake
$file = Join-Path $Fixture 'absent-parent/record.json'
Refuses { Invoke-DefenderChange 'apply' $file -Paths @('never-added') }
Require ($script:adds -eq 0) 'Record reservation failure reached Defender.'

Reset-Fake
$file = Record-For 'future'
[IO.File]::WriteAllText($file, '{"schema":2,"owner":"pondnt-defender","machine":"synthetic-fixture","entries":[]}')
$original = [IO.File]::ReadAllText($file)
Refuses { Invoke-DefenderChange 'apply' $file -Paths @('never-added') }
Require ($script:adds -eq 0 -and [IO.File]::ReadAllText($file) -ceq $original) 'Future record was changed.'

Reset-Fake
$file = Record-For 'foreign'
[IO.File]::WriteAllText($file, '{"applied":"old","level":"old","paths":["foreign-owned"],"processes":[]}')
$script:paths += 'foreign-owned'
Refuses { Invoke-DefenderChange 'undo' $file }
Require ($script:removes -eq 0) 'A foreign record was adopted.'
'PASS: invalid, foreign and unwritable records refuse before effects.'

Reset-Fake
$file = Record-For 'failed-undo'
Invoke-DefenderChange 'apply' $file -Paths @('remove-owned')
$script:failRemove = 1
Refuses { Invoke-DefenderChange 'undo' $file }
Require ((Read-DefenderRecord $file).entries[0].state -eq 'removing') 'Interrupted undo not retained.'
Require ($script:paths -contains 'remove-owned') 'Fixture removal unexpectedly completed.'
'PASS: failed undo retains a recoverable pending removal.'

Reset-Fake
$file = Record-For 'locked'
$held = Open-MaintenanceLock $file
try { Refuses { Invoke-DefenderChange 'apply' $file -Paths @('never-added') } }
finally { $held.Dispose() }
Require ($script:adds -eq 0) 'Concurrent record holder allowed effects.'
'PASS: record lock excludes concurrent changes; synthetic APIs only.'
