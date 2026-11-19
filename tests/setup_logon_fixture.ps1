param([Parameter(Mandatory=$true)][string]$Setup)
$ErrorActionPreference = 'Stop'
$errors = $null
$ast = [Management.Automation.Language.Parser]::ParseFile($Setup, [ref]$null, [ref]$errors)
if ($errors.Count) { throw ($errors | Out-String) }
foreach ($name in @('Quote','Service-UserArguments','Owned-TaskPython','Register-ServiceTask')) {
    $found = @($ast.FindAll({param($n) $n -is [Management.Automation.Language.FunctionDefinitionAst] -and $n.Name -eq $name}, $false))
    if ($found.Count -ne 1) { throw "Missing function: $name" }
    . ([scriptblock]::Create($found[0].Extent.Text))
}
function Current-User { 'FIXTURE\person' }
function New-ScheduledTaskAction { param($Execute,$Argument,$WorkingDirectory)
    [pscustomobject]@{Execute=$Execute;Arguments=$Argument;WorkingDirectory=$WorkingDirectory}
}
function New-ScheduledTaskTrigger { param([switch]$AtLogOn,$User)
    if (-not $AtLogOn -or $User -ne (Current-User)) { throw 'Wrong logon trigger' }
    [pscustomobject]@{CimClass=[pscustomobject]@{CimClassName='MSFT_TaskLogonTrigger'};UserId=$User}
}
function New-ScheduledTaskPrincipal { param($UserId,$LogonType,$RunLevel)
    $number = if ($RunLevel -eq 'Highest') { 1 } elseif ($RunLevel -eq 'Limited') { 0 } else { throw 'Unknown level' }
    [pscustomobject]@{UserId=$UserId;LogonType=$LogonType;RunLevel=$number}
}
function New-ScheduledTaskSettingsSet { [pscustomobject]@{} }
function Stop-ServiceTask { param($Item) $script:events += 'stop' }
function Register-ScheduledTask { param($TaskPath,$TaskName,$Action,$Trigger,$Principal,$Settings,[switch]$Force)
    $script:events += 'register'
    $script:task = [pscustomobject]@{Actions=@($Action);Triggers=@($Trigger);Principal=$Principal}
}
function Start-ScheduledTask { param($TaskPath,$TaskName) $script:events += 'start' }
foreach ($elevated in @($false,$true)) {
    $item = [pscustomobject]@{account='user';highest=$elevated;module='boring_knowledge.server';
        python='C:\Program Files\pondNT\candidate\python\pythonw.exe';
        folder='C:\fixture\50%\knowledge';task='pondNT fixture';arguments=@('http','--config','C:\fixture\50%\config.json');
        log='C:\fixture\50%\service.log'}
    $script:events = @()
    Register-ServiceTask $item
    if (($script:events -join ',') -ne 'stop,register,start') { throw 'Unexpected task order' }
    if ((Owned-TaskPython $item $script:task) -cne $item.python) { throw 'Task signature mismatch' }
    if ($script:task.Principal.RunLevel -ne [int]$elevated) { throw 'Elevation mismatch' }
    if (-not $script:task.Actions[0].Arguments.StartsWith('-X utf8 -m boring_pondnt.background ')) { throw 'Not the direct Python bootstrap' }
    $script:task.Principal.RunLevel = 1 - [int]$elevated
    if (Owned-TaskPython $item $script:task) { throw 'Wrong privilege task accepted' }
}
# Canvas: install, edit settings, rerun and select the same owned task for uninstall.
$item = [pscustomobject]@{highest=$false;module='boring_canvas.server';
    python='C:\Program Files\pondNT\candidate\python\pythonw.exe';
    folder='C:\fixture\50%\canvas';task='pondNT canvas';url='http://127.0.0.1:18511/mcp';
    arguments=@('--config','C:\fixture\50%\canvas\config.json','--state-dir','C:\fixture\50%\canvas\state','--unsafe-url-auth');
    log='C:\fixture\50%\canvas\service.log'}
Register-ServiceTask $item
$stableArguments = $script:task.Actions[0].Arguments
$item.url = 'http://127.0.0.1:18611/drawing/mcp'
if (-not (Owned-TaskPython $item $script:task)) { throw 'Canvas port edit lost task ownership' }
Register-ServiceTask $item
if ($script:task.Actions[0].Arguments -cne $stableArguments) { throw 'Canvas action changed with port' }
$script:task.Actions[0].Arguments = $stableArguments + ' --other'
if (Owned-TaskPython $item $script:task) { throw 'Customized Canvas action accepted' }
$script:task.Actions[0].Arguments = $stableArguments
function Unregister-ScheduledTask { param($TaskPath,$TaskName,[switch]$Confirm) $script:task=$null }
if (Owned-TaskPython $item $script:task) {
    Stop-ServiceTask $item
    Unregister-ScheduledTask -TaskPath '\pondNT\' -TaskName $item.task -Confirm:$false
}
if ($script:task) { throw 'Canvas task not removable after port edit' }
'PASS: person/logon elevation and Canvas edit/rerun/uninstall; synthetic APIs only'
