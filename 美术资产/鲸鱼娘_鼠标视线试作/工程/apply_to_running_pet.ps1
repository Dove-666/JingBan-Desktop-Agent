$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName UIAutomationClient
Add-Type @'
using System;
using System.Runtime.InteropServices;
public static class GazePetWindowMessage {
    [DllImport("user32.dll", SetLastError=true)]
    public static extern bool PostMessage(IntPtr hwnd, uint message, IntPtr wParam, IntPtr lParam);
}
'@
$project = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..\..\..')).Path
$pythonw = Join-Path $project '.venv\Scripts\pythonw.exe'
$rig = Join-Path $project 'assets\character\eyes\neutral\rig.json'
if (-not (Test-Path -LiteralPath $rig)) { throw 'The verified eye rig is missing.' }
$snapshot = @(Get-CimInstance Win32_Process)
$launchers = @($snapshot | Where-Object {
    $_.ExecutablePath -eq $pythonw -and $_.CommandLine -like '*main.py*'
})
$relatedIds = @($launchers | ForEach-Object { [int]$_.ProcessId })
foreach ($launcher in $launchers) {
    $relatedIds += @($snapshot | Where-Object { $_.ParentProcessId -eq $launcher.ProcessId } |
        ForEach-Object { [int]$_.ProcessId })
}
$petWindows = @()
foreach ($relatedId in $relatedIds) {
    $condition = New-Object System.Windows.Automation.PropertyCondition(
        [System.Windows.Automation.AutomationElement]::ProcessIdProperty, [int]$relatedId)
    $windows = [System.Windows.Automation.AutomationElement]::RootElement.FindAll(
        [System.Windows.Automation.TreeScope]::Children, $condition)
    foreach ($window in $windows) {
        $busyCondition = New-Object System.Windows.Automation.PropertyCondition(
            [System.Windows.Automation.AutomationElement]::NameProperty, '等待回复…')
        if ($null -ne $window.FindFirst([System.Windows.Automation.TreeScope]::Descendants, $busyCondition)) {
            throw 'A chat request is active; leave this session running.'
        }
        if ($window.Current.Name -eq 'AURA · 鲸鱼娘桌宠') { $petWindows += $window }
    }
}
if ($petWindows.Count -gt 1) { throw 'More than one project pet is visible; no process was closed.' }
foreach ($petWindow in $petWindows) {
    $handle = [IntPtr]$petWindow.Current.NativeWindowHandle
    if (-not [GazePetWindowMessage]::PostMessage($handle, 0x0010, [IntPtr]::Zero, [IntPtr]::Zero)) {
        throw 'Could not request normal application shutdown.'
    }
}
if ($launchers.Count -and -not $petWindows.Count) { throw 'Could not identify the project pet window.' }
$deadline = [DateTime]::UtcNow.AddSeconds(12)
do {
    $remaining = @($relatedIds | Where-Object { Get-Process -Id $_ -ErrorAction SilentlyContinue })
    if ($remaining.Count -eq 0) { break }
    Start-Sleep -Milliseconds 200
} while ([DateTime]::UtcNow -lt $deadline)
if ($remaining.Count) { throw 'Application is still shutting down; it was not force-terminated.' }
$stdout = Join-Path $PSScriptRoot 'live_stdout.log'
$stderr = Join-Path $PSScriptRoot 'live_stderr.log'
$started = Start-Process -FilePath $pythonw -ArgumentList @('main.py', '--debug-panel') `
    -WorkingDirectory $project -WindowStyle Hidden -PassThru `
    -RedirectStandardOutput $stdout -RedirectStandardError $stderr
$report = [ordered]@{
    requested_at = (Get-Date -Format o)
    normal_shutdown_requested = ($petWindows.Count -eq 1)
    old_processes_exited = $true
    forced_termination = $false
    new_launcher_pid = $started.Id
    neutral_rig = 'assets/character/eyes/neutral/rig.json'
    settings_changed = $false
    command = '.venv/Scripts/pythonw.exe main.py --debug-panel'
}
[IO.File]::WriteAllText((Join-Path $PSScriptRoot 'live_restart.json'),
    ($report | ConvertTo-Json -Depth 4), (New-Object Text.UTF8Encoding($false)))
$report | ConvertTo-Json -Compress
