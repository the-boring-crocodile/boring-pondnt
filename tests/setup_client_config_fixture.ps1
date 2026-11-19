param([Parameter(Mandatory=$true)][string]$Setup)
$ErrorActionPreference='Stop'
$text=Get-Content -LiteralPath $Setup -Raw
$start=$text.IndexOf('$clientChanged = $false')
if($start -lt 0 -or $start -ne $text.LastIndexOf('$clientChanged = $false')){throw 'Expected one installer apply block'}
$apply=[scriptblock]::Create($text.Substring($start))
function Ask {param($Question) $true}
function Say {param($Text,$Color) $script:messages+=@($Text)}
function Say-Handbook {param($Handbook)}
function Assert-ServiceHandover {param($Item)}
function Assert-McpUnchanged {if($script:fault -eq 'client-change'){throw 'synthetic client change'}}
function Stop-ServiceTask {param($Item) $script:events+=@('stop')}
function Register-ServiceTask {
 param($Item)
 $script:events+=@('register')
 if($script:fault -in @('task-start','rotate-start')){throw 'synthetic task start failure'}
}
function Wait-ServiceReady {param($Item) ($script:fault -ne 'not-ready')}
function Service-StartFailure {param($Item) 'synthetic startup failure'}
function Write-McpJson {
 param($Value)
 $script:events+=@('publish')
 if($script:fault -in @('publish','rotate-publish')){throw 'synthetic publish failure'}
 $script:writes++
}
function Services-Plan {
 param($Value,[switch]$Apply)
 $script:events+=@('prepare')
 $script:plan
}
foreach($case in @('unchanged','backend-only','rotate-key','new-entry','remove-direct','task-start','not-ready','publish','client-change','rotate-publish','rotate-start')){
 $script:fault=$case
 $RotateKeys=($case -in @('rotate-key','rotate-publish','rotate-start'))
 $config=[ordered]@{servers=[ordered]@{'boring-pondnt'=[ordered]@{type='http';url='http://127.0.0.1:18513/mcp';headers=@{Authorization='synthetic-old'};owner='preserve'}}}
 $entry=[pscustomobject]@{type='http';url='http://127.0.0.1:18513/mcp';headers=@{Authorization='synthetic-new'}}
 $item=[pscustomobject]@{name='boring-pondnt';entry=$entry;entry_changes=$false;url='http://127.0.0.1:18513/mcp';python='fixture-python';log='fixture.log'}
 $removals=@()
 switch($case){
  'backend-only' {$item.name='boring-sourcecache';$item.entry=$null}
  'rotate-key' {$item.entry_changes=$true}
  'new-entry' {$config.servers.Remove('boring-pondnt');$item.entry_changes=$true}
  'remove-direct' {$removals=@('boring-pondnt');$item.entry=$null}
  'publish' {$item.entry_changes=$true}
  'rotate-publish' {$item.entry_changes=$true}
  'rotate-start' {$item.entry_changes=$true}
 }
 $script:plan=[pscustomobject]@{services=@($item);removals=$removals;handbook=$null}
 $svcPlan=$script:plan
 $script:writes=0;$script:events=@();$script:messages=@()
 $failed=$false
 try { & $apply } catch {$failed=$true}
 if($case -in @('rotate-publish','rotate-start')){
  if(-not $failed -or ($script:messages -match '^Done[.]')){throw 'Rotation failure reported success'}
  $expected=if($case -eq 'rotate-publish'){'stop,prepare,publish'}else{'stop,prepare,publish,register'}
  $writes=if($case -eq 'rotate-publish'){0}else{1}
  if(($script:events -join ',') -cne $expected -or $script:writes -ne $writes){throw 'Rotation failure order/effects wrong'}
  continue
 }
 if($case -in @('task-start','not-ready','publish','client-change')){
  if(-not $failed -or $script:writes){throw "Failure published client: $case"}
  if($script:messages -match '^Done[.]'){throw "Failure reported success: $case"}
  continue
 }
 if($failed){throw "Successful case failed: $case"}
 $want=if($case -in @('rotate-key','new-entry','remove-direct')){1}else{0}
 if($script:writes -ne $want){throw "Wrong publication count: $case"}
 if($RotateKeys -and ($script:events -join ',') -cne 'stop,prepare,publish,register'){throw 'Rotation must stop, prepare, publish, then start'}
 if($case -in @('unchanged','backend-only')){
  if($config.servers['boring-pondnt'].owner -cne 'preserve' -or $config.servers['boring-pondnt'].headers.Authorization -cne 'synthetic-old'){throw "Unchanged client entry replaced: $case"}
 }
 if($case -eq 'remove-direct' -and $config.servers.Contains('boring-pondnt')){throw 'Direct entry retained behind gateway'}
 if($case -in @('rotate-key','new-entry') -and $config.servers['boring-pondnt'].headers.Authorization -cne 'synthetic-new'){throw 'Required entry update lost'}
}
'PASS: client publication and explicit rotation; synthetic APIs only'
