$ErrorActionPreference = 'Stop'
$photoshop = New-Object -ComObject Photoshop.Application
$photoshop.Visible = $true
$photoshop.DoJavaScriptFile((Join-Path $PSScriptRoot 'build_expression_interactions.jsx'))
