param([Parameter(Mandatory=$true)][string]$Setup)
$ErrorActionPreference='Stop'
$errors=$null
$ast=[Management.Automation.Language.Parser]::ParseFile($Setup,[ref]$null,[ref]$errors)
if($errors.Count){throw ($errors | Out-String)}
$fn=@($ast.FindAll({param($n) $n -is [Management.Automation.Language.FunctionDefinitionAst] -and $n.Name -eq 'Assert-ExistingIndex'},$false))
. ([scriptblock]::Create($fn[0].Extent.Text))
$work=Join-Path ([IO.Path]::GetTempPath()) ('pondnt-index-'+[guid]::NewGuid().ToString('N'))
$null=New-Item -ItemType Directory -Path $work
try {
 $Index=Join-Path $work 'source.db'
 [IO.File]::WriteAllText($Index,'synthetic index readability only')
 $before=(Get-FileHash -LiteralPath $Index).Hash
 $mtime=[IO.File]::GetLastWriteTimeUtc($Index)
 Assert-ExistingIndex
 if((Get-FileHash -LiteralPath $Index).Hash -ne $before -or [IO.File]::GetLastWriteTimeUtc($Index) -ne $mtime){throw 'Index input check changed file'}
 foreach($path in @('',(Join-Path $work 'absent.db'),$work)){
  $Index=$path;$failed=$false
  try{Assert-ExistingIndex}catch{$failed=$true}
  if(-not $failed){throw 'Missing/unreadable index accepted'}
 }
} finally {Remove-Item -LiteralPath $work -Recurse -Force}
'PASS: existing index input checked without building or changing it; synthetic APIs only'
