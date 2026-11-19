param([Parameter(Mandatory=$true)][string]$Setup, [switch]$NoNative)
$ErrorActionPreference='Stop'
$errors=$null
$ast=[Management.Automation.Language.Parser]::ParseFile($Setup,[ref]$null,[ref]$errors)
if($errors.Count){throw ($errors | Out-String)}
foreach($name in @('Quote','Current-User','Service-UserArguments','Sourcecache-TaskIndex','Owned-TaskPython','Read-InstalledServiceScope','Resolve-ServiceScope','Check-UnselectedServices','Read-ServiceTask','Checked-ServiceTask','Services-Plan','Say-Handbook')){
 $found=@($ast.FindAll({param($n) $n -is [Management.Automation.Language.FunctionDefinitionAst] -and $n.Name -eq $name},$false))
 if($found.Count -ne 1){throw "Missing function $name"}
 . ([scriptblock]::Create($found[0].Extent.Text))
}
function Say($text, $color) { $script:lastMessage = $text }
function Current-User {'FIXTURE\person'}
$item=[pscustomobject]@{highest=$true;module='boring_pondnt.server';folder='C:\fixture\50%';task='pondNT fixture';arguments=@('--config','C:\fixture\50%\config.json');log='C:\fixture\50%\service.log'}
function Task {
 [pscustomobject]@{Actions=@([pscustomobject]@{Execute='C:\old release\python\pythonw.exe';Arguments=(Service-UserArguments $item);WorkingDirectory=$item.folder});
  Triggers=@([pscustomobject]@{CimClass=[pscustomobject]@{CimClassName='MSFT_TaskLogonTrigger'};UserId=(Current-User)});
  Principal=[pscustomobject]@{UserId=(Current-User);RunLevel=1;LogonType='Interactive'}}
}
if((Owned-TaskPython $item (Task)) -ne 'C:\old release\python\pythonw.exe'){throw 'Expected direct task not recognized'}
foreach($case in @('principal','run-level','logon-type','trigger','trigger-user','execute','working-dir','arguments','actions')){
 $t=Task
 switch($case){
  'principal' {$t.Principal.UserId='OTHER\person'}
  'run-level' {$t.Principal.RunLevel=0}
  'logon-type' {$t.Principal.LogonType='ServiceAccount'}
  'trigger' {$t.Triggers[0].CimClass.CimClassName='MSFT_TaskBootTrigger'}
  'trigger-user' {$t.Triggers[0].UserId='OTHER\person'}
  'execute' {$t.Actions[0].Execute='cmd.exe'}
  'working-dir' {$t.Actions[0].WorkingDirectory+='-other'}
  'arguments' {$t.Actions[0].Arguments+=' --other'}
  'actions' {$t.Actions+=@($t.Actions[0])}
 }
 if(Owned-TaskPython $item $t){throw "Customized task recognized: $case"}
}
$script:task=Task
function Get-ScheduledTask {
 [CmdletBinding()]param($TaskPath,$TaskName)
 if($script:denied){Write-Error 'synthetic denial' -Category PermissionDenied;return}
 if ($null -ne $script:scopeTasks) { return $script:scopeTasks[$TaskName] }
 $script:task
}
$null=Checked-ServiceTask $item
$script:task.Actions[0].Arguments+=' --other'
$failed=$false;try{$null=Checked-ServiceTask $item}catch{$failed=$true}
if(-not $failed){throw 'Customized task collision accepted'}
$script:task=$null
if(Checked-ServiceTask $item){throw 'Absent task treated as present'}
$script:denied=$true
$failed=$false;try{$null=Read-ServiceTask $item}catch{$failed=$true}
if(-not $failed){throw 'Unreadable Scheduler treated as absent'}

$script:planned=@()
function Fake-Python {
 $script:planned+=,@($args);$global:LASTEXITCODE=0
 if ('--describe' -in $args) { return $script:templateJson }
 '{"services":[],"conflicts":[]}'
}
$python='Fake-Python';$root='C:\fixture\release';$stateRoot='C:\fixture\state';$Index='';$Services='core';$RotateKeys=$true
$config=@{servers=[ordered]@{}}
$null=Services-Plan $config
$null=Services-Plan $config -Remove
$null=Services-Plan $config -Apply
$modes=$script:planned | ForEach-Object {($_ | Where-Object {$_ -in '--plan','--uninstall'}) -join ','}
if(($modes -join '|') -cne '--plan|--uninstall|'){throw 'Planner modes wrong'}
$rotated=$script:planned | ForEach-Object {[string]([array]::IndexOf(@($_),'--rotate-keys') -ge 0)}
if(($rotated -join '|') -cne 'True|False|True'){throw 'Rotation modes wrong'}

# Observe Sourcecache's index only after validating the entire actual task.
$script:denied=$false
$coreItem=$item
$coreItem | Add-Member -NotePropertyName short -NotePropertyValue core
$script:task=Task
$script:task.Actions[0].Arguments+=' --custom'
$coreTask=$script:task
$item=[pscustomobject]@{short='sourcecache';highest=$false;module='boring_sourcecache_d365.server';
 folder='C:\fixture\sourcecache';task='pondNT sourcecache';log='C:\fixture\sourcecache\service.log';
 arguments=@('serve','--config','C:\fixture\sourcecache\config.json','--db','G:\old index\source.db','--state-dir','C:\fixture\sourcecache')}
$sourceTask=Task
$sourceTask.Principal.RunLevel=0
$item.arguments[4]='' # discovery template, not a second saved index
$script:scopeTasks=@{'pondNT sourcecache'=$sourceTask;'pondNT fixture'=$coreTask}
$script:templateJson=@{default_services=@('core','knowledge');services=@($item,$coreItem)} | ConvertTo-Json -Depth 10
$scope=Read-InstalledServiceScope
if(($scope.installed -join ',') -cne 'sourcecache'){throw 'Unowned task included in installed scope'}
if($scope.index -cne 'G:\old index\source.db'){throw 'Task index not recovered'}
if(-not (Owned-TaskPython $item $sourceTask)){throw 'Observed Sourcecache index not owned'}
$sourceTask.Actions[0].Arguments+=' --custom'
$scope=Read-InstalledServiceScope
if($scope.installed.Count -or $scope.index){throw 'Custom Sourcecache task was guessed'}
if(-not ($script:planned[0] -contains '--installed=')){throw 'Observed task scope not passed to planner'}
'PASS: exact task ownership, observed Sourcecache index and planner arguments; synthetic APIs only'

# The previous internal 0.3.0 Canvas launcher read its second port from web.json.
$item=[pscustomobject]@{highest=$false;module='boring_canvas.server';folder='C:\fixture\canvas';
 task='pondNT boring-canvas';log='C:\fixture\canvas\service.log';
 arguments=@('--config','C:\fixture\canvas\config.json','--state-dir','C:\fixture\canvas\state','--unsafe-url-auth')}
$canvasTask=Task
$canvasTask.Principal.RunLevel=0
$legacyArgs=@($item.arguments[0..3])+@('--host','127.0.0.1','--unsafe-url-auth')
$canvasTask.Actions[0].Arguments=Service-UserArguments $item $legacyArgs
if(-not (Owned-TaskPython $item $canvasTask)){throw 'Previous generated Canvas task not recognized'}
$canvasTask.Actions[0].Arguments+=' "--port" "18510"'
if(Owned-TaskPython $item $canvasTask){throw 'Customized legacy Canvas task recognized'}


# Published 0.2.10 created Sourcecache and gateway as SYSTEM/cmd, but core
# remained a desktop task. Step 2 exports the complete scope and removes the
# SYSTEM registrations. The old gateway task must not determine the new route.
$item = $coreItem
$coreTask = Task
$legacyTaskXml = [xml]'<Task><Principals><Principal><UserId>S-1-5-18</UserId></Principal></Principals><Actions><Exec><Command>cmd.exe</Command><Arguments>/c "set PYTHONUTF8=1&amp;&amp; "C:\old release\python\python.exe" -m boring_sourcecache_d365.server serve "--db" "G:\old index\source.db""</Arguments></Exec></Actions></Task>'
$recordedIndex = [regex]::Match($legacyTaskXml.Task.Actions.Exec.Arguments, '"--db" "([^"]+)"').Groups[1].Value
$recorded = 'sourcecache,core,d365data'
$script:scopeTasks = @{'pondNT fixture' = $coreTask}
$scope = Read-InstalledServiceScope
if (($scope.installed -join ',') -cne 'core' -or $scope.index) { throw 'Removed SYSTEM task was inferred' }
$scope.configured = @('core','sourcecache','d365data')
$default = Resolve-ServiceScope $scope '' '' $false $false
try {
    Check-UnselectedServices $scope $default.services $false
    throw 'Missing -Services went undetected'
} catch {
    if ($_ -notlike '*Pass -Services*step 2*') { throw }
}
Check-UnselectedServices $scope 'core' $true
if ($script:lastMessage -notlike '*sourcecache, d365data*') { throw 'Preview did not name omitted services' }
Check-UnselectedServices $scope $recorded $true
$legacy = Resolve-ServiceScope $scope $recorded $recordedIndex $true $true
if ($legacy.services -cne $recorded -or $legacy.index -cne 'G:\old index\source.db') {
    throw 'Legacy preview/apply lost the recorded index or an opt-in'
}
$Services = $legacy.services
$Index = $legacy.index
$script:InstalledServices = @($scope.installed)
$null = Services-Plan $config
$null = Services-Plan $config -Apply
foreach ($argsPassed in @($script:planned[-2], $script:planned[-1])) {
    if ('--only=sourcecache,core,d365data' -notin $argsPassed -or
        'G:\old index\source.db' -notin $argsPassed -or '--gateway' -in $argsPassed) {
        throw 'Legacy preview and apply did not both pass the recorded scope to the planner'
    }
}
