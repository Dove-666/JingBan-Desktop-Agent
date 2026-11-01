param([string[]]$States = @('neutral','happy','surprised','sleepy','angry','thinking','talk_open'))
$ErrorActionPreference = 'Stop'
$scriptDir = $PSScriptRoot
$requestFile = Join-Path $scriptDir 'batch_states.txt'
[System.IO.File]::WriteAllText($requestFile, ($States -join "`n"), (New-Object System.Text.UTF8Encoding($false)))
$successFile = Join-Path $scriptDir 'photoshop_success.txt'
$errorFile = Join-Path $scriptDir 'photoshop_error.txt'
if (Test-Path -LiteralPath $successFile) { Remove-Item -LiteralPath $successFile }
if (Test-Path -LiteralPath $errorFile) { Remove-Item -LiteralPath $errorFile }
$psApp = New-Object -ComObject Photoshop.Application
$psApp.Visible = $true
Write-Output ('Photoshop ' + $psApp.Version)
$result = $psApp.DoJavaScriptFile((Join-Path $scriptDir 'photoshop_expressions.jsx'))
Write-Output $result
if (Test-Path -LiteralPath $errorFile) { throw (Get-Content -LiteralPath $errorFile -Raw) }
if (-not (Test-Path -LiteralPath $successFile)) { throw 'Photoshop did not write completion evidence.' }
Get-Content -LiteralPath $successFile
