$ErrorActionPreference = 'Stop'
$sourcePath = 'E:\github\AURA-Desktop-Agent\assets\expressions\neutral.png'
$sourceHash = (Get-FileHash -Algorithm SHA256 -LiteralPath $sourcePath).Hash.ToLowerInvariant()
[System.IO.File]::WriteAllText((Join-Path $PSScriptRoot 'source_sha256.txt'), $sourceHash, (New-Object System.Text.UTF8Encoding($false)))
$psApp = New-Object -ComObject Photoshop.Application
$psApp.Visible = $true
Write-Output ($psApp.DoJavaScriptFile((Join-Path $PSScriptRoot 'build_eye_rig.jsx')))
