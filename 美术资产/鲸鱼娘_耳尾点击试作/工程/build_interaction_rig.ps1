$ErrorActionPreference = 'Stop'
$source = Join-Path ((Get-Item -LiteralPath $PSScriptRoot).Parent.Parent.Parent.FullName) 'assets/expressions/neutral.png'
$hash = (Get-FileHash -LiteralPath $source -Algorithm SHA256).Hash.ToLowerInvariant()
[System.IO.File]::WriteAllText((Join-Path $PSScriptRoot 'source_sha256.txt'), $hash, (New-Object System.Text.UTF8Encoding($false)))
$photoshop = New-Object -ComObject Photoshop.Application
$photoshop.Visible = $true
$photoshop.DoJavaScriptFile((Join-Path $PSScriptRoot 'build_interaction_rig.jsx'))
