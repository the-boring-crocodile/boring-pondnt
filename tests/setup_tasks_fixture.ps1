param([Parameter(Mandatory=$true)][string]$Setup, [switch]$NoNative)
$ErrorActionPreference = 'Stop'
$tokens = $null
$errors = $null
$ast = [Management.Automation.Language.Parser]::ParseFile($Setup, [ref]$tokens, [ref]$errors)
if ($errors.Count) { throw ($errors | Out-String) }
# Import only named function definitions. Never execute setup's top-level statements.
$names = @('Quote','Service-ProcessCommand','Service-TaskCommand','Owned-TaskPython',
           'Read-ServiceTask','Checked-ServiceTask','Service-Processes','Stop-ServiceProcesses',
           'Current-User','Service-UserArguments','Owned-UserTaskPython')
foreach ($name in $names) {
    $found = @($ast.FindAll({ param($node)
        $node -is [Management.Automation.Language.FunctionDefinitionAst] -and $node.Name -eq $name
    }, $false))
    if ($found.Count -ne 1) { throw "Expected exactly one function $name" }
    . ([scriptblock]::Create($found[0].Extent.Text))
}
$item = [pscustomobject]@{
    module = 'boring_knowledge.server'
    folder = 'C:\fixture\services\knowledge'
    task = 'pondNT boring-knowledge'
    arguments = @('http','--config','C:\fixture\services\knowledge\config.json',
                  '--db','C:\fixture\services\knowledge\notebook.db','--role','global',
                  '--state-dir','C:\fixture\services\knowledge')
    log = 'C:\fixture\services\knowledge\service.log'
}
$python = 'C:\old release\python\python.exe'
function Fixture-Task {
    [pscustomobject]@{
        Actions = @([pscustomobject]@{
            Execute='cmd.exe'
            Arguments=(Service-TaskCommand $item $python)
            WorkingDirectory=$item.folder
        })
        Triggers = @([pscustomobject]@{CimClass=[pscustomobject]@{CimClassName='MSFT_TaskBootTrigger'}})
        Principal = [pscustomobject]@{UserId='SYSTEM';RunLevel=1}
    }
}
$expected = '/c "set PYTHONUTF8=1&& "C:\old release\python\python.exe" -m boring_knowledge.server "http" "--config" "C:\fixture\services\knowledge\config.json" "--db" "C:\fixture\services\knowledge\notebook.db" "--role" "global" "--state-dir" "C:\fixture\services\knowledge" >> "C:\fixture\services\knowledge\service.log" 2>&1"'
if ((Service-TaskCommand $item $python) -cne $expected) { throw 'Legacy task command changed' }
if ((Owned-TaskPython $item (Fixture-Task)) -cne $python) { throw 'Legacy task not recognized' }

foreach ($case in @('extra-command','working-folder','principal','actions','trigger','executable','extra-argument')) {
    $t = Fixture-Task
    switch ($case) {
        'extra-command' { $t.Actions[0].Arguments += ' & echo unrelated' }
        'working-folder' { $t.Actions[0].WorkingDirectory += '-other' }
        'principal' { $t.Principal.UserId = 'someone-else' }
        'actions' { $t.Actions += $t.Actions[0] }
        'trigger' { $t.Triggers[0].CimClass.CimClassName = 'MSFT_TaskLogonTrigger' }
        'executable' { $t.Actions[0].Execute = 'C:\other\cmd.exe' }
        'extra-argument' { $t.Actions[0].Arguments = $t.Actions[0].Arguments.Replace('"http"', '"http" "--custom"') }
    }
    if (Owned-TaskPython $item $t) { throw "Customized task accepted: $case" }
}
if (-not $NoNative) {  # the ScheduledTasks module exists on Windows only
    # Construct native CIM definitions in memory; do not register a scheduled task.
    $action = New-ScheduledTaskAction -Execute 'cmd.exe' -Argument $expected -WorkingDirectory $item.folder
    $trigger = New-ScheduledTaskTrigger -AtStartup
    $principal = New-ScheduledTaskPrincipal -UserId 'SYSTEM' -LogonType ServiceAccount -RunLevel Highest
    $native = New-ScheduledTask -Action $action -Trigger $trigger -Principal $principal
    if ((Owned-TaskPython $item $native) -cne $python) { throw 'Native CIM ownership shape differs' }
}

$script:task = Fixture-Task
function Get-ScheduledTask {
    [CmdletBinding()]param($TaskPath,$TaskName)
    if ($script:queryFailure) { Write-Error 'synthetic access denied' -Category PermissionDenied; return }
    $script:task
}
$null = Checked-ServiceTask $item
$script:task.Actions[0].Arguments += ' changed'
$refused=$false
try { $null = Checked-ServiceTask $item } catch { $refused=$true }
if (-not $refused) { throw 'Collision was not refused' }
$script:task=$null
if (Checked-ServiceTask $item) { throw 'Absent task treated as existing' }
$script:queryFailure=$true
$refused=$false
try { $null = Checked-ServiceTask $item } catch { $refused=$true }
if (-not $refused) { throw 'Inspection failure treated as absent task' }
$script:queryFailure=$false

# All process APIs are doubles; no process enumeration or termination occurs.
$command = Service-ProcessCommand $item $python
$script:processes = @(
    [pscustomobject]@{ProcessId=11;ExecutablePath=$python;CommandLine=$command},
    [pscustomobject]@{ProcessId=12;ExecutablePath=$python;CommandLine=($command+' --unrelated')},
    [pscustomobject]@{ProcessId=13;ExecutablePath='C:\other\python.exe';CommandLine=$command},
    [pscustomobject]@{ProcessId=14;ExecutablePath=$python;CommandLine=('python inspect.py "'+$item.folder+'\file"')},
    [pscustomobject]@{ProcessId=15;ExecutablePath=$python;CommandLine=($command.Replace(('"'+$python+'" '),('"'+$python+'"  '))+' ')}
)
$script:stopped=@()
function Get-CimInstance { param($ClassName,$Filter) $script:processes }
function Stop-CheckedProcess { param($Observed) $script:stopped += $Observed.ProcessId }
Stop-ServiceProcesses $item $python
if (($script:stopped -join ',') -cne '11,15') { throw 'Owned cmd separator variant missed or unrelated process selected' }

# the core's task, as the person at logon through pythonw, serving the state handbook.
$core = [pscustomobject]@{
    account = 'user'
    module = 'boring_pondnt.server'
    folder = 'C:\fixture\services\core'
    task = 'pondNT boring-pondnt'
    arguments = @('--config','C:\fixture\services\core\config.json','--handbook','C:\fixture\handbook')
    log = 'C:\fixture\services\core\service.log'
}
$pythonw = 'C:\old release\python\pythonw.exe'
$coreArgs = '-X utf8 -m boring_pondnt.background --log "C:\fixture\services\core\service.log" boring_pondnt.server "--config" "C:\fixture\services\core\config.json"'
$kitArgs = $coreArgs + ' "--handbook" "C:\fixture\handbook"'
if ((Service-UserArguments $core) -cne $kitArgs) { throw 'Core task arguments changed' }
function Core-Task {
    [pscustomobject]@{
        Actions = @([pscustomobject]@{Execute=$pythonw;Arguments=$kitArgs;WorkingDirectory=$core.folder})
        Triggers = @([pscustomobject]@{CimClass=[pscustomobject]@{CimClassName='MSFT_TaskLogonTrigger'}})
        Principal = [pscustomobject]@{UserId=(Current-User);RunLevel=1;LogonType='Interactive'}
    }
}
if ((Owned-TaskPython $core (Core-Task)) -cne $pythonw) { throw 'Core task not recognized' }
# A core task from before the handbook (--kit) is not this release's: retained for review.
$t = Core-Task; $t.Actions[0].Arguments = $coreArgs + ' "--kit" "C:\fixture\kit"'
if (Owned-TaskPython $core $t) { throw 'Pre-handbook core task accepted' }

foreach ($case in @('other-user','boot','system-logon','console-python','extra-argument','no-kit','not-elevated')) {
    $t = Core-Task
    switch ($case) {
        'other-user' { $t.Principal.UserId = 'someone-else' }
        'boot' { $t.Triggers[0].CimClass.CimClassName = 'MSFT_TaskBootTrigger' }
        'system-logon' { $t.Principal.LogonType = 'ServiceAccount' }
        'console-python' { $t.Actions[0].Execute = 'C:\old release\python\python.exe' }
        'extra-argument' { $t.Actions[0].Arguments += ' --kit D:\other' }
        'no-kit' { $t.Actions[0].Arguments = $coreArgs }
        'not-elevated' { $t.Principal.RunLevel = 0 }
    }
    if (Owned-TaskPython $core $t) { throw "Customized core task accepted: $case" }
}
if (Owned-TaskPython $item (Core-Task)) { throw 'A logon task was taken for a SYSTEM service' }
$script:processes = @(
    [pscustomobject]@{ProcessId=21;ExecutablePath=$pythonw;CommandLine=('"' + $pythonw + '" ' + $coreArgs)},
    [pscustomobject]@{ProcessId=22;ExecutablePath=$pythonw;CommandLine=('"' + $pythonw + '" ' + $coreArgs + ' --unrelated')},
    [pscustomobject]@{ProcessId=23;ExecutablePath='C:\other\pythonw.exe';CommandLine=('"x" ' + $coreArgs)},
    [pscustomobject]@{ProcessId=24;ExecutablePath=$pythonw;CommandLine=('"' + $pythonw + '" ' + $kitArgs)}
)
$script:stopped=@()
Stop-ServiceProcesses $core $pythonw
if ($script:stopped.Count -ne 1 -or $script:stopped[0] -ne 24) { throw 'Unrelated core process selected' }
# Services-Plan passes exactly one mode to the planner, which refuses two. The planner is a double here.
$found = @($ast.FindAll({ param($node)
    $node -is [Management.Automation.Language.FunctionDefinitionAst] -and $node.Name -eq 'Services-Plan'
}, $false))
. ([scriptblock]::Create($found[0].Extent.Text))
$script:planned = @()
function Fake-Python { $script:planned += ,@($args); '{"services":[],"conflicts":[]}' }
$python = 'Fake-Python'; $root = 'C:\fixture\release'; $stateRoot = 'C:\fixture\state'; $Index = ''; $Services = 'core'
$cfg = @{ servers = [ordered]@{} }
$null = Services-Plan $cfg
$null = Services-Plan $cfg -Remove
$null = Services-Plan $cfg -Apply -Finalize
$null = Services-Plan $cfg -Apply
$modes = $script:planned | ForEach-Object { ($_ | Where-Object { $_ -in '--plan','--uninstall','--finalize' }) -join ',' }
if (($modes -join '|') -cne '--plan|--uninstall|--finalize|') { throw "Planner modes wrong: $($modes -join '|')" }
# -Gateway goes to plan and apply only; uninstall and finalize read the gateway's ferry.json.
$Gateway = 'all'; $script:planned = @()
$null = Services-Plan $cfg; $null = Services-Plan $cfg -Remove; $null = Services-Plan $cfg -Apply -Finalize; $null = Services-Plan $cfg -Apply
$gated = $script:planned | ForEach-Object { [string]([array]::IndexOf(@($_), '--gateway') -ge 0) }
if (($gated -join '|') -cne 'True|False|False|True') { throw "Gateway passed wrong: $($gated -join '|')" }
Write-Output 'PASS: legacy ownership, customized task refusal, exact process selection; synthetic APIs only.'
