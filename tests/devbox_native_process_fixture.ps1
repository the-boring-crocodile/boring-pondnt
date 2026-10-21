param([Parameter(Mandatory)][string]$Helper, [Parameter(Mandatory)][string]$Fixture)
$ErrorActionPreference = 'Stop'
. $Helper
$file = Join-Path $Fixture 'native-process.json'
$record = Read-DevboxRecord $file
$executable = (Get-Process -Id $PID).Path
$entry = $null
try {
    # A sleeping child of this test, never a service or another user's process.
    $entry = Start-DevboxEntry 'devbox' $executable @('-NoProfile','-NonInteractive','-Command','Start-Sleep -Seconds 120') $Fixture $file $record
    $loaded = Read-DevboxRecord $file
    Stop-DevboxEntry $loaded.entries[0] $file $loaded
    if ((Read-DevboxRecord $file).entries[0].state -ne 'stopped') { throw 'Native owned child did not settle.' }
    'PASS: native exact CIM/process identity, held handle and confirmed exit; test child only.'
} finally {
    if ($entry) {
        $child = Get-DevboxProcess $entry.pid
        if ($child) {
            Assert-DevboxIdentity $entry $child
            $child.Kill()
            if (-not $child.WaitForExit(10000)) { throw 'Native fixture cleanup unconfirmed.' }
        }
    }
}
