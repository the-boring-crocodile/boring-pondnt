param([Parameter(Mandatory=$true)][string]$Setup, [Parameter(Mandatory=$true)][string]$Python)
$ErrorActionPreference='Stop'
$ast=[Management.Automation.Language.Parser]::ParseFile($Setup,[ref]$null,[ref]$null)
$names=@('Quote','Service-ProcessCommand','Service-Processes','Stop-CheckedProcess','Stop-ServiceProcesses',
 'Service-Ports','Service-Listeners','Assert-ServiceHandover','Wait-ServiceStopped','Stop-ServiceTask',
 'Wait-ServiceReady','Assert-McpUnchanged','PowerShell-Literal','Save-SetupAttempt','Start-SetupAttempt',
 'Set-SetupPhase','Fail-SetupAttempt','Complete-SetupAttempt')
foreach($name in $names){
 $found=@($ast.FindAll({param($node) $node -is [Management.Automation.Language.FunctionDefinitionAst] -and $node.Name -eq $name},$false))
 if($found.Count -ne 1){throw "Missing function $name"}
 . ([scriptblock]::Create($found[0].Extent.Text))
}
function Say {param($Text,$Color)}
$script:when=[DateTime]::UtcNow
$script:mode='valid'
function Get-Process {
 [CmdletBinding()]param($Id)
 if($script:mode -eq 'absent'){Write-Error 'gone' -Category ObjectNotFound;return}
 if($script:mode -eq 'denied'){Write-Error 'denied' -Category PermissionDenied;return}
 $script:fake
}
function Fake-Process {
 $p=[pscustomobject]@{Handle=1;Path='C:\fixture\python.exe';StartTime=$script:when}
 $p | Add-Member ScriptMethod Kill {$script:kills++}
 $p | Add-Member ScriptMethod WaitForExit {param($Milliseconds) $script:mode -ne 'timeout'}
 $p | Add-Member ScriptMethod Dispose {$script:disposed++}
 $p
}
$observed=[pscustomobject]@{ProcessId=42;ExecutablePath='C:\fixture\python.exe';CreationDate=$script:when}
foreach($case in @('valid','reused','path-changed','absent','denied','timeout')){
 $script:mode=$case;$script:fake=Fake-Process;$script:kills=0;$script:disposed=0
 if($case -eq 'reused'){$script:fake.StartTime=$script:when.AddSeconds(1)}
 if($case -eq 'path-changed'){$script:fake.Path='C:\unrelated\python.exe'}
 $failed=$false
 try {Stop-CheckedProcess $observed}catch{$failed=$true}
 if($failed -ne ($case -in @('reused','path-changed','denied','timeout'))){throw "Process result wrong: $case"}
 $wanted=if($case -in @('valid','timeout')){1}else{0}
 if($script:kills -ne $wanted){throw "Wrong process killed: $case"}
 if($case -notin @('absent','denied') -and $script:disposed -ne 1){throw 'Process handle leaked'}
}
$script:mode='valid'
$item=[pscustomobject]@{name='fixture-canvas';account='system';module='boring_canvas.server';task='fixture-canvas';url='http://127.0.0.1:18653/mcp';python='C:\fixture\python.exe';arguments=@('--port','18652');folder='C:\fixture';log='C:\fixture\service.log'}
$script:rows=@([pscustomobject]@{ProcessId=42;ExecutablePath=$item.python;CommandLine=(Service-ProcessCommand $item $item.python);CreationDate=$script:when})
$script:listeners=@([pscustomobject]@{LocalPort=18652;OwningProcess=42},[pscustomobject]@{LocalPort=18653;OwningProcess=42})
$script:task=[pscustomobject]@{State='Running'}
$script:logFree=$true;$script:polls=0
function Get-CimInstance {param($ClassName,$Filter,$ErrorAction) $script:rows}
function Get-NetTCPConnection {
 [CmdletBinding()]param($LocalAddress,$LocalPort,$State)
 if($script:listenerDenied){Write-Error 'denied' -Category PermissionDenied;return}
 $script:listeners | Where-Object {$_.LocalPort -eq $LocalPort}
}
function Checked-ServiceTask {param($Item) $script:task}
function Owned-TaskPython {param($Item,$Task) $item.python}
function Test-ServiceLogFree {param($Item) $script:logFree}
function Stop-ScheduledTask {param($TaskPath,$TaskName,$ErrorAction) $script:task.State='Ready'}
function Start-Sleep {param($Milliseconds) $script:polls++;$script:listeners=@();$script:logFree=$true}
if((@(Service-Ports $item) -join ',') -ne '18652,18653'){throw 'Canvas listener coverage missing'}
Assert-ServiceHandover $item
if(-not (Wait-ServiceReady $item 0)){throw 'Owned complete listeners refused'}
$script:listeners[0].OwningProcess=99
$failed=$false;try{Assert-ServiceHandover $item}catch{$failed=$true}
if(-not $failed -or (Wait-ServiceReady $item 0)){throw 'Foreign listener accepted'}
$script:listeners[0].OwningProcess=42
$script:rows[0].CommandLine+=' --unrelated'
if(Wait-ServiceReady $item 0){throw 'Same interpreter with unrelated command accepted'}
$script:rows[0].CommandLine=Service-ProcessCommand $item $item.python
$script:listeners=@($script:listeners[1])
if(Wait-ServiceReady $item 0){throw 'Missing Canvas web listener accepted'}
$script:listenerDenied=$true
$failed=$false;try{$null=Service-Listeners $item}catch{$failed=$true}
if(-not $failed){throw 'Listener inspection failure treated as absent'}
$script:listenerDenied=$false
$script:listeners=@()
foreach($case in @('task-running','port-held','log-held','clear')){
 $script:task.State='Ready';$script:listeners=@();$script:logFree=$true
 switch($case){
  'task-running' {$script:task.State='Running'}
  'port-held' {$script:listeners=@([pscustomobject]@{LocalPort=18653;OwningProcess=99})}
  'log-held' {$script:logFree=$false}
 }
 $failed=$false;try{Wait-ServiceStopped $item 0}catch{$failed=$true}
 if($failed -ne ($case -ne 'clear')){throw "Stop readiness wrong: $case"}
}
$script:task.State='Running';$script:listeners=@([pscustomobject]@{LocalPort=18653;OwningProcess=42});$script:logFree=$false
$script:fake=Fake-Process;$script:polls=0
Stop-ServiceTask $item
if($script:polls -ne 1){throw 'Replacement did not await listener/log release'}

$work=Join-Path ([IO.Path]::GetTempPath()) ('setup-recovery-'+[guid]::NewGuid().ToString('N'))
$null=New-Item -ItemType Directory -Path $work
try {
 $python=$Python;$root=Join-Path $work "release's space";$stateRoot=Join-Path $work 'state'
 $Index=Join-Path $work "index's.db";$McpJson=Join-Path $work 'mcp.json'
 [IO.File]::WriteAllText($McpJson,'{"servers":{},"owner":"retain"}')
 $script:McpReadSha=(Get-FileHash -LiteralPath $McpJson).Hash.ToLowerInvariant()
 Assert-McpUnchanged
 $before=[IO.File]::ReadAllBytes($McpJson)
 [IO.File]::WriteAllText($McpJson,'{"owner":"concurrent change"}')
 $failed=$false;try{Assert-McpUnchanged}catch{$failed=$true}
 if(-not $failed){throw 'Concurrent client change ignored'}
 [IO.File]::WriteAllBytes($McpJson,$before)
 [IO.File]::SetLastWriteTimeUtc($McpJson,[DateTime]'2020-01-01Z')
 $mtime=[IO.File]::GetLastWriteTimeUtc($McpJson)
 $selected=[pscustomobject]@{services=@(
  [pscustomobject]@{module='boring_knowledge.server';folder=(Join-Path $stateRoot 'services\knowledge');behind=$null;headers=@{Authorization='synthetic-credential'}},
  [pscustomobject]@{module='boring_pondnt.d365web_server';folder=(Join-Path $stateRoot 'services\d365web');behind='boring-gateway'}
 )}
 Start-SetupAttempt $selected
 $first=$script:SetupAttemptPath
 $record=Get-Content -LiteralPath $first -Raw | ConvertFrom-Json
 if($record.services -cne 'knowledge,d365web' -or $record.gateway -cne 'd365web'){throw 'Retry scope lost'}
 if((Get-Content -LiteralPath $first -Raw) -match 'Authorization|Bearer|synthetic-credential'){throw 'Credential persisted in receipt'}
 $errors=$null;$tokens=$null
 $null=[Management.Automation.Language.Parser]::ParseInput($record.retry_preview,[ref]$tokens,[ref]$errors)
 if($errors.Count){throw 'Retry command quoting invalid'}
 Set-SetupPhase 'start-service' 'boring-d365web'
 Fail-SetupAttempt
 $record=Get-Content -LiteralPath $first -Raw | ConvertFrom-Json
 if($record.status -ne 'failed' -or $record.phase -ne 'start-service' -or $record.current_service -ne 'boring-d365web'){throw 'Failure phase lost'}
 Start-SetupAttempt $selected
 Complete-SetupAttempt
 if($first -eq $script:SetupAttemptPath -or -not(Test-Path -LiteralPath $first)){throw 'Previous recovery evidence overwritten'}
 $record=Get-Content -LiteralPath $script:SetupAttemptPath -Raw | ConvertFrom-Json
 if($record.status -ne 'completed'){throw 'Completion not persisted'}
 Start-SetupAttempt $selected 'uninstall'
 $record=Get-Content -LiteralPath $script:SetupAttemptPath -Raw | ConvertFrom-Json
 if($record.retry_preview -notmatch ' -Uninstall .* -WhatIf$'){throw 'Uninstall retry lost'}
 if((Get-FileHash -LiteralPath $McpJson).Hash.ToLowerInvariant() -cne $script:McpReadSha -or [IO.File]::GetLastWriteTimeUtc($McpJson) -ne $mtime){throw 'Recovery record touched client config'}
} finally {Remove-Item -LiteralPath $work -Recurse -Force}
'PASS: process identity, bounded handover, listener refusal and durable recovery; synthetic APIs only'
