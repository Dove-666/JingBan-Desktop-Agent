param(
    [string]$DotnetPath = 'dotnet',
    [ValidateSet('x64', 'arm64')][string]$Architecture = 'x64',
    [string[]]$NuGetSource = @()
)
$ErrorActionPreference = 'Stop'
$packageRoot = $PSScriptRoot
$sdkVersion = & $DotnetPath --version
if ($LASTEXITCODE -ne 0 -or $sdkVersion -notmatch '^10\.') {
    throw 'Building the official Windows helper requires the .NET 10 SDK.'
}
$project = Join-Path $packageRoot 'native/entry/CuaWindows.csproj'
$destination = Join-Path $packageRoot "native/bin/win-$Architecture"
$restoreArguments = @('restore', $project, '--locked-mode', '-p:SelfContained=true', '-p:EnableWindowsTargeting=true')
foreach ($source in $NuGetSource) { $restoreArguments += @('--source', $source) }
& $DotnetPath @restoreArguments
if ($LASTEXITCODE -ne 0) { throw 'Official Windows helper dependency restore failed.' }
& $DotnetPath publish $project --configuration Release --runtime "win-$Architecture" --self-contained true --output $destination --no-restore -p:EnableWindowsTargeting=true
if ($LASTEXITCODE -ne 0) { throw 'Official Windows helper build failed.' }
$files = @{}
Get-ChildItem -LiteralPath $destination -File -Recurse | ForEach-Object {
    $relative = [IO.Path]::GetRelativePath($destination, $_.FullName).Replace('\', '/')
    if ($relative -ne 'BUILD.json') {
        $files[$relative] = (Get-FileHash -LiteralPath $_.FullName -Algorithm SHA256).Hash.ToLowerInvariant()
    }
}
@{
    upstream = 'https://github.com/deepseek-ai/deepseek-harness'
    revision = '39163dd30f703a79079e2d2446545cff3c453738'
    local_patches_sha256 = (Get-FileHash -LiteralPath (Join-Path $packageRoot 'patches/MANIFEST.json') -Algorithm SHA256).Hash.ToLowerInvariant()
    sdk = "$sdkVersion"
    runtime = "win-$Architecture"
    self_contained = $true
    sha256 = $files
} | ConvertTo-Json -Depth 4 | Set-Content -LiteralPath (Join-Path $destination 'BUILD.json') -Encoding utf8
Write-Output "Built DeepSeek Windows helper with recorded local patches: $destination"
