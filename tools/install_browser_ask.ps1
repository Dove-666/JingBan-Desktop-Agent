[CmdletBinding()]
param(
    [switch]$Apply,
    [switch]$PrepareOnly,
    [ValidateSet('Edge', 'Chrome')][string[]]$Browser = @('Edge', 'Chrome'),
    [string]$InstallDirectory = (Join-Path $env:LOCALAPPDATA 'AURA-Desktop-Agent\browser-ask'),
    [string]$ConfigPath = (Join-Path $env:LOCALAPPDATA 'AURA Desktop Pet\settings.ini'),
    [string]$PythonPath = '',
    [string]$ProjectDirectory = ''
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false)
if ($Apply -and $PrepareOnly) { throw 'Choose -Apply or -PrepareOnly, not both.' }
if (-not $ProjectDirectory) { $ProjectDirectory = Split-Path -Parent $PSScriptRoot }
$ProjectDirectory = [IO.Path]::GetFullPath($ProjectDirectory)
$InstallDirectory = [IO.Path]::GetFullPath($InstallDirectory)
$ConfigPath = [IO.Path]::GetFullPath($ConfigPath)
$packagedHost = Join-Path $ProjectDirectory '鲸伴网页桥.exe'
$usePackagedHost = Test-Path -LiteralPath $packagedHost -PathType Leaf
if (-not $PythonPath -and -not $usePackagedHost) { $PythonPath = Join-Path $ProjectDirectory '.venv\Scripts\python.exe' }
if ($PythonPath) { $PythonPath = [IO.Path]::GetFullPath($PythonPath) }
$hostScript = Join-Path $ProjectDirectory 'tools\browser_ask_host.py'
$manifestPath = Join-Path $InstallDirectory 'native-host.json'
$launcherPath = Join-Path $InstallDirectory 'host.cmd'
$markerPath = Join-Path $InstallDirectory 'installation.json'
$backupPath = Join-Path $InstallDirectory 'registration-backup.json'
$extensionId = 'gbeikpndpckhjeakbckijelidpdpinei'
$hostName = 'aura.whale_ask'
$paths = @{
    Edge = 'Software\Microsoft\Edge\NativeMessagingHosts\aura.whale_ask'
    Chrome = 'Software\Google\Chrome\NativeMessagingHosts\aura.whale_ask'
}
$registryPaths = @($Browser | Select-Object -Unique | ForEach-Object { $paths[$_] })
foreach ($value in @($InstallDirectory, $ConfigPath, $PythonPath, $ProjectDirectory, $hostScript, $packagedHost)) {
    if ($value.IndexOfAny([char[]]@([char]13, [char]10, [char]34)) -ge 0) { throw 'Paths cannot contain newlines or double quotes.' }
}
$plan = [ordered]@{
    mode = $(if ($Apply) { 'apply' } elseif ($PrepareOnly) { 'prepare-only' } else { 'plan' })
    host_name = $hostName
    extension_id = $extensionId
    extension_directory = (Join-Path $ProjectDirectory 'browser-extension\whale-ask')
    install_directory = $InstallDirectory
    config_path = $ConfigPath
    registry = @($registryPaths | ForEach-Object { [ordered]@{ hive = 'HKEY_CURRENT_USER'; key = $_; value = $manifestPath } })
    files = @($launcherPath, $manifestPath, $markerPath, $backupPath)
    browser_extension_installed = $false
    packaged_host = [bool]$usePackagedHost
}
if (-not $Apply -and -not $PrepareOnly) { $plan | ConvertTo-Json -Depth 6; exit 0 }
if (-not $usePackagedHost) {
    if (-not (Test-Path -LiteralPath $PythonPath -PathType Leaf)) { throw 'Project Python was not found. Create .venv and install requirements first.' }
    if (-not (Test-Path -LiteralPath $hostScript -PathType Leaf)) { throw 'tools/browser_ask_host.py was not found in this project.' }
}
if (-not (Test-Path -LiteralPath (Join-Path $ProjectDirectory 'browser-extension\whale-ask\manifest.json') -PathType Leaf)) {
    throw 'The browser extension manifest was not found.'
}
if (Test-Path -LiteralPath $markerPath) {
    $existingMarker = Get-Content -LiteralPath $markerPath -Raw -Encoding UTF8 | ConvertFrom-Json
    if ($existingMarker.host_name -ne $hostName -or $existingMarker.extension_id -ne $extensionId) {
        throw 'The destination belongs to another installation. Choose another -InstallDirectory.'
    }
} else {
    foreach ($file in @($launcherPath, $manifestPath, $backupPath)) {
        if (Test-Path -LiteralPath $file) { throw 'An unrecognized file exists at the destination. Nothing was overwritten.' }
    }
}

function Write-Utf8File([string]$Path, [string]$Content) {
    [IO.File]::WriteAllText($Path, $Content, (New-Object System.Text.UTF8Encoding($false)))
}
function Read-RegistryDefault([string]$Path) {
    $key = [Microsoft.Win32.Registry]::CurrentUser.OpenSubKey($Path)
    try {
        $hasValue = $null -ne $key -and @($key.GetValueNames()) -contains ''
        [ordered]@{ key = $Path; existed = $hasValue; value = $(if ($hasValue) { $key.GetValue('', $null, [Microsoft.Win32.RegistryValueOptions]::DoNotExpandEnvironmentNames) } else { $null }); kind = $(if ($hasValue) { [string]$key.GetValueKind('') } else { 'String' }) }
    } finally { if ($null -ne $key) { $key.Dispose() } }
}
function Restore-RegistryEntry($Entry) {
    $key = [Microsoft.Win32.Registry]::CurrentUser.CreateSubKey([string]$Entry.key)
    try {
        if ($Entry.existed) { $kind = [Microsoft.Win32.RegistryValueKind]([string]$Entry.kind); $key.SetValue('', $Entry.value, $kind) }
        else { $key.DeleteValue('', $false) }
    } finally { $key.Dispose() }
}

[IO.Directory]::CreateDirectory($InstallDirectory) | Out-Null
$backupEntries = @()
if (Test-Path -LiteralPath $backupPath) {
    $backup = Get-Content -LiteralPath $backupPath -Raw -Encoding UTF8 | ConvertFrom-Json
    if ($backup.host_name -ne $hostName) { throw 'Unexpected registration backup. Nothing was registered.' }
    $backupEntries = @($backup.entries)
}
if ($Apply) {
    foreach ($path in $registryPaths) {
        if (-not @($backupEntries | Where-Object { $_.key -eq $path }).Count) {
            $backupEntries += Read-RegistryDefault $path
        }
    }
}
Write-Utf8File $backupPath (([ordered]@{ host_name = $hostName; entries = @($backupEntries) }) | ConvertTo-Json -Depth 6)
$escapePath = { param([string]$Value) $Value.Replace('%', '%%') }
if ($usePackagedHost) {
    $launcher = "@echo off`r`n@setlocal DisableDelayedExpansion`r`n@chcp 65001 >nul`r`n@cd /d `"$(& $escapePath $ProjectDirectory)`"`r`n@`"$(& $escapePath $packagedHost)`" --config-path `"$(& $escapePath $ConfigPath)`" %*`r`n"
} else {
    $launcher = "@echo off`r`n@setlocal DisableDelayedExpansion`r`n@chcp 65001 >nul`r`n@cd /d `"$(& $escapePath $ProjectDirectory)`"`r`n@`"$(& $escapePath $PythonPath)`" -u `"$(& $escapePath $hostScript)`" --config-path `"$(& $escapePath $ConfigPath)`" %*`r`n"
}
Write-Utf8File $launcherPath $launcher
Write-Utf8File $manifestPath (([ordered]@{
    name = $hostName; description = '鲸伴 · 网页提问连接桥'; path = $launcherPath
    type = 'stdio'; allowed_origins = @("chrome-extension://$extensionId/")
}) | ConvertTo-Json -Depth 4)
$registered = @()
if (Test-Path -LiteralPath $markerPath) { $registered = @($existingMarker.registered_keys) }
$applied = @()
try {
    if ($Apply) {
        foreach ($path in $registryPaths) {
            $previous = Read-RegistryDefault $path
            $key = [Microsoft.Win32.Registry]::CurrentUser.CreateSubKey($path)
            try { $key.SetValue('', $manifestPath, [Microsoft.Win32.RegistryValueKind]::String) }
            finally { $key.Dispose() }
            $applied += $previous
            $registered += $path
        }
    }
    Write-Utf8File $markerPath (([ordered]@{
        host_name = $hostName; extension_id = $extensionId; project_directory = $ProjectDirectory
        manifest_path = $manifestPath; registered_keys = @($registered | Select-Object -Unique)
    }) | ConvertTo-Json -Depth 6)
} catch {
    foreach ($entry in $applied) { Restore-RegistryEntry $entry }
    throw
}
$plan | ConvertTo-Json -Depth 6
