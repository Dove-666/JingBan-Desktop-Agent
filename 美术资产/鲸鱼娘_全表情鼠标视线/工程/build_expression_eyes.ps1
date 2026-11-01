param([string[]]$States=@('surprised','sleepy','angry','thinking','talk_open'))
$ErrorActionPreference='Stop'
$utf8=New-Object System.Text.UTF8Encoding($false)
[System.IO.File]::WriteAllText((Join-Path $PSScriptRoot 'batch_states.txt'),($States -join "`n"),$utf8)
foreach($stateName in $States){$hash=(Get-FileHash -LiteralPath ('E:\github\AURA-Desktop-Agent\assets\expressions\'+$stateName+'.png') -Algorithm SHA256).Hash.ToLowerInvariant();[System.IO.File]::WriteAllText((Join-Path $PSScriptRoot ($stateName+'_source_sha256.txt')),$hash,$utf8)}
$psApp=New-Object -ComObject Photoshop.Application
$psApp.Visible=$true
Write-Output ($psApp.DoJavaScriptFile((Join-Path $PSScriptRoot 'build_expression_eyes.jsx')))
