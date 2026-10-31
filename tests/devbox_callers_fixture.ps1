param([string]$Helper, [string]$Fixture)
$ErrorActionPreference = 'Stop'
. $Helper
$root = Join-Path $Fixture 'ci'
$null = New-Item -ItemType Directory -Path $root
$access = Read-DevboxAccess $root
if ($access.Count -ne 1 -or $access[0].subject -ne 'pond' -or $access[0].role -ne 'writer') { throw 'without a file: pond writer only' }
Set-Content -LiteralPath (Join-Path $root 'devbox-callers.json') -Value '[{"subject": "tim", "role": "writer"}]'
$access = Read-DevboxAccess $root
if ($access.Count -ne 2 -or $access[1].subject -ne 'tim' -or $access[1].role -ne 'writer' -or $access[1].author -ne 'tim') { throw 'tim not added' }
$json = [ordered]@{ access = $access } | ConvertTo-Json -Depth 4
if (@(($json | ConvertFrom-Json).access).Count -ne 2) { throw 'access does not survive ConvertTo-Json' }
foreach ($bad in @('[{"subject": "pond", "role": "writer"}]', '[{"subject": "Someone", "role": "writer"}]',
                   '[{"subject": "tim", "role": "admin"}]', '[{"subject": "tim", "role": "writer", "x": 1}]',
                   '[{"subject": "tim", "role": "reader"}, {"subject": "tim", "role": "writer"}]')) {
    Set-Content -LiteralPath (Join-Path $root 'devbox-callers.json') -Value $bad
    $refused = $false
    try { $null = Read-DevboxAccess $root } catch { $refused = $true }
    if (-not $refused) { throw "accepted: $bad" }
}
'PASS: pond only without a file; tim added; bad entries refused'
