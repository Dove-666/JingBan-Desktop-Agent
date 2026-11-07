[CmdletBinding()]
param(
    [switch]$Apply,
    [string]$InstallDirectory = (Join-Path $env:LOCALAPPDATA 'AURA-Desktop-Agent\browser-ask')
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false)
$InstallDirectory = [IO.Path]::GetFullPath($InstallDirectory)
$markerPath = Join-Path $InstallDirectory 'installation.json'
$backupPath = Join-Path $InstallDirectory 'registration-backup.json'
$manifestPath = Join-Path $InstallDirectory 'native-host.json'
$files = @('host.cmd', 'native-host.json', 'installation.json', 'registration-backup.json')
$allowed = @('Software\Microsoft\Edge\NativeMessagingHosts\aura.whale_ask', 'Software\Google\Chrome\NativeMessagingHosts\aura.whale_ask')
$plan = [ordered]@{ mode = $(if ($Apply) { 'apply' } else { 'plan' }); install_directory = $InstallDirectory; files = $files; registry = @(); browser_extension_removed = $false }
if (-not (Test-Path -LiteralPath $markerPath -PathType Leaf)) {
    if ($Apply) { throw '未找到鲸伴安装标记，未移除任何文件或注册项。' }
    $plan | ConvertTo-Json -Depth 5; exit 0
}
$marker = Get-Content -LiteralPath $markerPath -Raw -Encoding UTF8 | ConvertFrom-Json
if ($marker.host_name -ne 'aura.whale_ask' -or $marker.extension_id -ne 'gbeikpndpckhjeakbckijelidpdpinei' -or
    [IO.Path]::GetFullPath([string]$marker.manifest_path) -ne $manifestPath) {
    throw 'The installation marker does not match this directory. Nothing was removed.'
}
$registered = @($marker.registered_keys)
if (@($registered | Where-Object { $_ -notin $allowed }).Count) { throw 'Unexpected registry key in installation marker. Nothing was removed.' }
$plan.registry = $registered
if (-not $Apply) { $plan | ConvertTo-Json -Depth 5; exit 0 }
$backup = Get-Content -LiteralPath $backupPath -Raw -Encoding UTF8 | ConvertFrom-Json
if ($backup.host_name -ne 'aura.whale_ask') { throw 'The registration backup is invalid. Nothing was removed.' }
$restore = @()
# Preflight every key before changing any. A half-completed earlier uninstall
# may already have restored a key; accept exactly the recorded previous value.
foreach ($path in $registered) {
    $entry = @($backup.entries | Where-Object { $_.key -eq $path })
    if ($entry.Count -ne 1) { throw 'The registry rollback information is incomplete. Nothing was removed.' }
    $key = [Microsoft.Win32.Registry]::CurrentUser.OpenSubKey($path)
    if ($null -eq $key) { continue }
    try {
        $current = $key.GetValue('', $null, [Microsoft.Win32.RegistryValueOptions]::DoNotExpandEnvironmentNames)
        if ($current -ne $manifestPath) {
            $alreadyRestored = ($entry[0].existed -and $current -eq $entry[0].value) -or (-not $entry[0].existed -and $null -eq $current)
            if ($alreadyRestored) { continue }
            throw 'Another registration now owns this host. No foreign registry value will be overwritten.'
        }
        $restore += [pscustomobject]@{ key = $path; previous = $entry[0] }
    } finally { $key.Dispose() }
}
foreach ($action in $restore) {
    $path = $action.key
    $entry = @($action.previous)
    $key = [Microsoft.Win32.Registry]::CurrentUser.OpenSubKey($path, $true)
    if ($null -eq $key) { continue }
    try {
        $current = $key.GetValue('', $null, [Microsoft.Win32.RegistryValueOptions]::DoNotExpandEnvironmentNames)
        if ($current -ne $manifestPath) { throw 'Registration changed during uninstall. No foreign value will be overwritten.' }
        if ($entry[0].existed) { $kind = [Microsoft.Win32.RegistryValueKind]([string]$entry[0].kind); $key.SetValue('', $entry[0].value, $kind) }
        else { $key.DeleteValue('', $false) }
        $empty = $key.ValueCount -eq 0 -and $key.SubKeyCount -eq 0
    } finally { $key.Dispose() }
    if ($empty) { [Microsoft.Win32.Registry]::CurrentUser.DeleteSubKey($path, $false) }
}
# Delete only our four named files. Never recursively delete the destination.
foreach ($name in $files) {
    $file = Join-Path $InstallDirectory $name
    if (Test-Path -LiteralPath $file -PathType Leaf) { Remove-Item -LiteralPath $file -Force }
}
if ((Test-Path -LiteralPath $InstallDirectory -PathType Container) -and
    @(Get-ChildItem -LiteralPath $InstallDirectory -Force).Count -eq 0) {
    Remove-Item -LiteralPath $InstallDirectory
}
$plan | ConvertTo-Json -Depth 5
