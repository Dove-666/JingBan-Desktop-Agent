param([switch]$Apply)
$ErrorActionPreference = 'Stop'
$taskRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
$taskReport = Join-Path $taskRoot 'artifacts/release-validation/cleanup'
$taskPlan = Join-Path $taskReport 'plan.json'
$taskResult = Join-Path $taskReport 'result.json'
$taskDesktopSource = [IO.File]::ReadAllText((Join-Path $taskRoot 'desktop_pet/__init__.py'))
$taskAndroidSource = [IO.File]::ReadAllText((Join-Path $taskRoot 'android/app/build.gradle.kts'))
$taskDesktopVersion = [regex]::Match($taskDesktopSource, 'APP_VERSION\s*=\s*"([0-9.]+)"').Groups[1].Value
$taskAndroidVersion = [regex]::Match($taskAndroidSource, 'versionName\s*=\s*"([0-9.]+)"').Groups[1].Value
if (-not $taskDesktopVersion -or -not $taskAndroidVersion) { throw 'Cannot identify current releases' }
$taskScopes = @('artifacts', 'deliverables/windows', 'android/deliverables', 'deliverables/软件需求分析')
$taskKeep = @(
    'artifacts/windows-build/worker_distribution',
    'artifacts/windows-build/engine_distribution',
    'artifacts/windows-build/tooling',
    'artifacts/windows-build/build-cache',
    'artifacts/release-validation',
    'artifacts/reliability-fixes',
    'artifacts/asset_pipeline/checkpoints',
    'artifacts/asset_pipeline/vendor',
    "deliverables/windows/$taskDesktopVersion",
    "android/deliverables/$taskAndroidVersion"
)

function Get-TaskRelative([string]$Path) {
    $taskAbsolute = [IO.Path]::GetFullPath($Path)
    if (-not $taskAbsolute.StartsWith($taskRoot + '\', [StringComparison]::OrdinalIgnoreCase)) {
        throw 'Cleanup path escaped the project'
    }
    return $taskAbsolute.Substring($taskRoot.Length + 1).Replace('\', '/')
}

function Test-TaskRetained([string]$Relative) {
    if ([IO.Path]::GetExtension($Relative) -ieq '.md') { return $true }
    foreach ($taskPrefix in $taskKeep) {
        if ($Relative -ieq $taskPrefix -or $Relative.StartsWith($taskPrefix + '/', [StringComparison]::OrdinalIgnoreCase)) {
            return $true
        }
    }
    return $false
}

function Assert-TaskPath([string]$Path) {
    $taskRelative = Get-TaskRelative $Path
    $taskAllowed = $false
    foreach ($taskScope in $taskScopes) {
        if ($taskRelative.StartsWith($taskScope + '/', [StringComparison]::OrdinalIgnoreCase)) { $taskAllowed = $true }
    }
    if (-not $taskAllowed -or (Test-TaskRetained $taskRelative)) { throw 'Cleanup selected a protected path' }
    $taskCursor = [IO.Path]::GetFullPath($Path)
    while ($taskCursor -ine $taskRoot) {
        $taskAttributes = [IO.File]::GetAttributes($taskCursor)
        if ($taskAttributes -band [IO.FileAttributes]::ReparsePoint) { throw 'Cleanup refuses redirected paths' }
        $taskCursor = [IO.Path]::GetDirectoryName($taskCursor)
    }
}

New-Item -ItemType Directory -Path $taskReport -Force | Out-Null
if (-not $Apply) {
    $taskFiles = [Collections.Generic.List[object]]::new()
    $taskDirectories = [Collections.Generic.List[string]]::new()
    foreach ($taskScope in $taskScopes) {
        $taskStart = Join-Path $taskRoot $taskScope
        if (-not (Test-Path -LiteralPath $taskStart)) { continue }
        $taskQueue = [Collections.Generic.Stack[IO.DirectoryInfo]]::new()
        $taskQueue.Push((Get-Item -LiteralPath $taskStart))
        while ($taskQueue.Count) {
            $taskDirectory = $taskQueue.Pop()
            if ($taskDirectory.Attributes -band [IO.FileAttributes]::ReparsePoint) { continue }
            foreach ($taskItem in $taskDirectory.EnumerateFileSystemInfos()) {
                if ($taskItem.Attributes -band [IO.FileAttributes]::ReparsePoint) { continue }
                $taskRelative = Get-TaskRelative $taskItem.FullName
                if (Test-TaskRetained $taskRelative) { continue }
                if ($taskItem -is [IO.DirectoryInfo]) {
                    $taskDirectories.Add($taskRelative)
                    $taskQueue.Push($taskItem)
                } else {
                    $taskFiles.Add(@{ path = $taskRelative; bytes = $taskItem.Length; modified = $taskItem.LastWriteTimeUtc.Ticks })
                }
            }
        }
    }
    $taskValue = @{ root = $taskRoot; desktop = $taskDesktopVersion; android = $taskAndroidVersion;
        created = [DateTime]::UtcNow.ToString('o'); files = @($taskFiles.ToArray()); directories = @($taskDirectories.ToArray());
        totalBytes = ($taskFiles | Measure-Object -Property bytes -Sum).Sum }
    $taskValue | ConvertTo-Json -Depth 6 | Set-Content -LiteralPath $taskPlan -Encoding utf8
    @{ plannedFiles = $taskFiles.Count; plannedGiB = [math]::Round($taskValue.totalBytes / 1GB, 3); applied = $false } | ConvertTo-Json
    exit 0
}

$taskReady = Join-Path $taskRoot 'artifacts/release-validation/release-ready.json'
if (-not (Test-Path -LiteralPath $taskReady)) { throw 'Verify the new releases before deleting old files' }
$taskApproved = Get-Content -LiteralPath $taskReady -Raw -Encoding utf8 | ConvertFrom-Json
if (-not $taskApproved.passed -or $taskApproved.desktop -ne $taskDesktopVersion -or $taskApproved.android -ne $taskAndroidVersion) {
    throw 'Release approval does not match current sources'
}
foreach ($taskPackage in $taskApproved.packages) {
    $taskPackagePath = Join-Path $taskRoot $taskPackage.path
    $null = Get-TaskRelative $taskPackagePath
    if ((Get-FileHash -LiteralPath $taskPackagePath -Algorithm SHA256).Hash -ine $taskPackage.sha256) { throw 'New package changed' }
}
$taskValue = Get-Content -LiteralPath $taskPlan -Raw -Encoding utf8 | ConvertFrom-Json
if ($taskValue.root -ine $taskRoot -or $taskValue.desktop -ne $taskDesktopVersion -or $taskValue.android -ne $taskAndroidVersion) {
    throw 'Cleanup plan belongs to different inputs'
}
$taskRemoved = 0
$taskRemovedBytes = 0L
$taskPreviouslyAbsent = 0
$taskPreviouslyAbsentBytes = 0L
$taskSkipped = [Collections.Generic.List[string]]::new()
foreach ($taskFile in $taskValue.files) {
    $taskPath = Join-Path $taskRoot $taskFile.path
    if (-not [IO.File]::Exists($taskPath)) {
        $taskPreviouslyAbsent++
        $taskPreviouslyAbsentBytes += $taskFile.bytes
        continue
    }
    Assert-TaskPath $taskPath
    $taskCurrent = [IO.FileInfo]::new($taskPath)
    if (($taskCurrent.Attributes -band [IO.FileAttributes]::Directory) -or $taskCurrent.Length -ne $taskFile.bytes -or $taskCurrent.LastWriteTimeUtc.Ticks -ne $taskFile.modified) {
        $taskSkipped.Add($taskFile.path); continue
    }
    try {
        Remove-Item -LiteralPath $taskPath -Force -ErrorAction Stop
        $taskRemoved++
        $taskRemovedBytes += $taskFile.bytes
        if ($taskRemoved % 10000 -eq 0) {
            @{ removed = $taskRemoved; previouslyAbsent = $taskPreviouslyAbsent; removedGiB = [math]::Round($taskRemovedBytes / 1GB, 3) } | ConvertTo-Json -Compress
        }
    } catch { $taskSkipped.Add($taskFile.path) }
}
foreach ($taskDirectory in ($taskValue.directories | Sort-Object -Property Length -Descending)) {
    $taskPath = Join-Path $taskRoot $taskDirectory
    if (-not (Test-Path -LiteralPath $taskPath)) { continue }
    Assert-TaskPath $taskPath
    if (-not (Get-ChildItem -LiteralPath $taskPath -Force | Select-Object -First 1)) {
        Remove-Item -LiteralPath $taskPath -Force
    }
}
$taskOutcome = @{ applied = $true; removedFiles = $taskRemoved; removedBytes = $taskRemovedBytes;
    removedGiB = [math]::Round($taskRemovedBytes / 1GB, 3); skipped = @($taskSkipped.ToArray());
    previouslyAbsentFiles = $taskPreviouslyAbsent; previouslyAbsentBytes = $taskPreviouslyAbsentBytes;
    markdownPreserved = $true; developmentSourcesPreserved = $true; newPackagesVerified = $true }
$taskOutcome | ConvertTo-Json -Depth 4 | Set-Content -LiteralPath $taskResult -Encoding utf8
$taskOutcome | ConvertTo-Json -Depth 4
