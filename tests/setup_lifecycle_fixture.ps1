param([Parameter(Mandatory=$true)][string]$Setup, [string]$Python)
$ErrorActionPreference='Stop'
$errors=$null
$ast=[Management.Automation.Language.Parser]::ParseFile($Setup,[ref]$null,[ref]$errors)
if($errors.Count){throw ($errors | Out-String)}
foreach($name in @('Service-Ports','Service-Listeners','Assert-ServiceHandover','Wait-ServiceStopped','Stop-ServiceTask','Wait-ServiceReady','Assert-McpUnchanged')){
 $found=@($ast.FindAll({param($n) $n -is [Management.Automation.Language.FunctionDefinitionAst] -and $n.Name -eq $name},$false))
 if($found.Count -ne 1){throw "Missing function $name"}
 . ([scriptblock]::Create($found[0].Extent.Text))
}
$item=[pscustomobject]@{name='canvas';module='boring_canvas.server';task='pondNT canvas';url='http://127.0.0.1:18653/mcp';arguments=@('--config','C:\fixture\canvas\config.json')}
$script:task=[pscustomobject]@{State='Running'}
$script:listeners=@([pscustomobject]@{LocalPort=18653;OwningProcess=42})
$script:stops=0;$script:polls=0
function Checked-ServiceTask {param($Item) $script:task}
function Get-NetTCPConnection {
 [CmdletBinding()]param($LocalAddress,$LocalPort,$State)
 if($script:denied){Write-Error 'synthetic denial' -Category PermissionDenied;return}
 $script:listeners | Where-Object {$_.LocalPort -eq $LocalPort}
}
function Stop-ScheduledTask {param($TaskPath,$TaskName,$ErrorAction) $script:stops++;$script:task.State='Ready'}
function Start-Sleep {param($Milliseconds) $script:polls++;$script:listeners=@()}
function Stop-Process {throw 'No process kill is permitted'}
function Get-CimInstance {throw 'No process ownership reconstruction is permitted'}
if((@(Service-Ports $item) -join ',') -ne '18653'){throw 'Canvas must have one listener'}
if(-not (Wait-ServiceReady $item 0)){throw 'Running task with listeners not ready'}
$script:listeners=@()
if(Wait-ServiceReady $item 0){throw 'Missing MCP listener accepted'}
$script:listeners=@([pscustomobject]@{LocalPort=18653;OwningProcess=42})
$script:task=$null
$failed=$false;try{Assert-ServiceHandover $item}catch{$failed=$true}
if(-not $failed -or $script:stops){throw 'Listener without task was not retained'}
$script:task=[pscustomobject]@{State='Running'}
foreach($case in @('running','port-held','clear')){
 $script:task.State='Ready';$script:listeners=@()
 if($case -eq 'running'){$script:task.State='Running'}
 if($case -eq 'port-held'){$script:listeners=@([pscustomobject]@{LocalPort=18653;OwningProcess=99})}
 $failed=$false;try{Wait-ServiceStopped $item 0}catch{$failed=$true}
 if($failed -ne ($case -ne 'clear')){throw "Stop bound wrong: $case"}
}
$script:task.State='Running';$script:polls=0
Stop-ServiceTask $item
if($script:stops -ne 1){throw 'Scheduler did not own the stop'}
$script:listeners=@([pscustomobject]@{LocalPort=18653;OwningProcess=99})
$failed=$false;try{Wait-ServiceStopped $item 0}catch{$failed=$true}
if(-not $failed -or $script:stops -ne 1){throw 'Foreign listener was not left alone'}
$script:denied=$true
$failed=$false;try{$null=Service-Listeners $item}catch{$failed=$true}
if(-not $failed){throw 'Listener query failure hidden'}
'PASS: bounded Scheduler shutdown and listener checks; synthetic APIs only'
