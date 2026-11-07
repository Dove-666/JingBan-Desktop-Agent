$ErrorActionPreference = 'Stop'
$compilerCache = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..\..\artifacts\windows_release_20261107\tooling'))
New-Item -ItemType Directory -Path $compilerCache -Force | Out-Null
$source = 'https://github.com/jrsoftware/issrc/releases/download/is-6_7_4/innosetup-6.7.4.exe'
$download = Join-Path $compilerCache 'innosetup-6.7.4.exe'
Invoke-WebRequest -Uri $source -OutFile $download
$hash = (Get-FileHash -LiteralPath $download -Algorithm SHA256).Hash.ToLowerInvariant()
$signature = Get-AuthenticodeSignature -LiteralPath $download
if ($signature.Status -ne 'Valid' -or $signature.SignerCertificate.Subject -notmatch 'Pyrsys B.V.') {
    throw 'Compiler publisher signature did not validate.'
}
@{version='6.7.4';sha256=$hash;signature='Valid';source=$source} |
    ConvertTo-Json | Set-Content -LiteralPath (Join-Path $compilerCache 'inno_provenance.json') -Encoding UTF8
Write-Output 'Official Inno Setup compiler download and signature verified. No installer executed.'
