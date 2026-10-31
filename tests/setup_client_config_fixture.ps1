param([Parameter(Mandatory=$true)][string]$Setup)
$ErrorActionPreference='Stop'
$text=Get-Content -LiteralPath $Setup -Raw
$start=$text.IndexOf('$write = $false')
if($start -lt 0 -or $start -ne $text.LastIndexOf('$write = $false')){throw 'Expected one installer apply block'}
$apply=[scriptblock]::Create($text.Substring($start))
function Ask {param($Question) $true}
function Say {param($Text,$Color) $script:messages+=@($Text)}
function Say-Handbook {param($Handbook)}
function Checked-ServiceTask {param($Item)}
function Assert-ServiceHandover {param($Item)}
function Assert-McpUnchanged {if($script:fault -eq 'client-change'){throw 'synthetic client change'}}
function Start-SetupAttempt {param($Selected,$Operation) $script:SetupAttempt=@{started_services=@();retired_services=@()};$script:began++}
function Set-SetupPhase {param($Phase,$Service) $script:phase=$Phase}
function Save-SetupAttempt {}
function Fail-SetupAttempt {$script:failures++}
function Complete-SetupAttempt {$script:completed++}
function Register-ServiceTask {param($Item) if($script:fault -eq 'task-start'){throw 'synthetic task start failure'}}
function Wait-ServiceReady {param($Item) ($script:fault -ne 'not-ready')}
function Service-StartFailure {param($Item) 'synthetic startup failure'}
function Read-McpJson {$config}
function Write-McpJson {param($Value) if($script:fault -eq 'publish'){throw 'synthetic publish failure'};$script:writes++}
function Services-Plan {
 param($Value,[switch]$Apply,[switch]$Finalize)
 if($Finalize){$script:finalizes++; return @{}}
 $script:plan
}
foreach($case in @('unchanged','backend-only','renew-key','new-entry','remove-direct','task-start','not-ready','publish','client-change')){
 $script:fault=$case
 $config=[ordered]@{servers=[ordered]@{'boring-pondnt'=[ordered]@{type='http';url='http://127.0.0.1:18513/mcp';headers=@{Authorization='synthetic-old'};owner='preserve'}}}
 $entry=[pscustomobject]@{type='http';url='http://127.0.0.1:18513/mcp';headers=@{Authorization='synthetic-new'}}
 $item=[pscustomobject]@{name='boring-pondnt';entry=$entry;entry_changes=$false;url='http://127.0.0.1:18513/mcp';python='fixture-python';log='fixture.log'}
 $removals=@()
 switch($case){
  'backend-only' {$item.name='boring-sourcecache';$item.entry=$null}
  'renew-key' {$item.entry_changes=$true}
  'new-entry' {$config.servers.Remove('boring-pondnt');$item.entry_changes=$true}
  'remove-direct' {$removals=@('boring-pondnt');$item.entry=$null}
  'publish' {$item.entry_changes=$true}
 }
 $script:plan=[pscustomobject]@{services=@($item);retire=@();removals=$removals;handbook=$null}
 $svcPlan=$script:plan
 $script:writes=0;$script:finalizes=0;$script:completed=0;$script:failures=0;$script:began=0;$script:messages=@()
 $failed=$false
 try { & $apply } catch {$failed=$true}
 if($case -in @('task-start','not-ready','publish','client-change')){
  if(-not $failed -or $script:writes -or $script:finalizes -or $script:completed){throw "Failure was published or finalized: $case"}
  if($script:failures -ne 1 -or ($script:messages -match '^Done\.')){throw "Failure reported success: $case"}
  if($case -ne 'client-change' -and $script:began -ne 1){throw "Missing recovery attempt: $case"}
  continue
 }
 if($failed){throw "Successful case failed: $case"}
 $want=if($case -in @('renew-key','new-entry','remove-direct')){1}else{0}
 if($script:writes -ne $want){throw "Wrong publication count: $case"}
 if($script:finalizes -ne 1 -or $script:completed -ne 1){throw "Backend cleanup/receipt skipped: $case"}
 if($case -in @('unchanged','backend-only')){
  if($config.servers['boring-pondnt'].owner -cne 'preserve' -or $config.servers['boring-pondnt'].headers.Authorization -cne 'synthetic-old'){throw "Unchanged client entry replaced: $case"}
 }
 if($case -eq 'remove-direct' -and $config.servers.Contains('boring-pondnt')){throw 'Direct entry retained during routing change'}
 if($case -in @('renew-key','new-entry') -and $config.servers['boring-pondnt'].headers.Authorization -cne 'synthetic-new'){throw 'Required entry update lost'}
}
'PASS: client publication and fail-closed recovery decisions; synthetic APIs only'
