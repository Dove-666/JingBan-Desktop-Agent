# Fixed UI Automation transport. Model data arrives only as JSON on stdin.
# AURA implementation; design references are recorded in desktop_controls.py.
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
[Console]::InputEncoding = [System.Text.UTF8Encoding]::new($false)
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
$script:watch = [Diagnostics.Stopwatch]::StartNew()
$script:visited = 0

function Fail([string]$code) { throw [InvalidOperationException]::new($code) }
function Budget {
    $script:visited += 1
    if ($script:visited -gt 1800 -or $script:watch.Elapsed.TotalSeconds -gt 7) { Fail 'DESKTOP_TIMEOUT' }
}
function RuntimeId($element) { return ($element.GetRuntimeId() -join '.') }
function Pattern($element, $identifier) {
    $value = $null
    if ($element.TryGetCurrentPattern($identifier, [ref]$value)) { return $value }
    return $null
}
function WindowInfo($root, [long]$handle) {
    $current = $root.Current
    $process = [Diagnostics.Process]::GetProcessById($current.ProcessId)
    return @{ handle = $handle; process_id = [int]$current.ProcessId;
        process_started = $process.StartTime.ToUniversalTime().Ticks.ToString();
        runtime_id = (RuntimeId $root); title = $current.Name }
}
function ReadNode($element) {
    Budget
    $current = $element.Current
    $state = @{ enabled = [bool]$current.IsEnabled; focused = [bool]$current.HasKeyboardFocus }
    $actions = [Collections.Generic.List[string]]::new()
    $password = [bool]$current.IsPassword
    if (-not $password) {
        if ($current.IsKeyboardFocusable) { $actions.Add('focus') }
        $invoke = Pattern $element ([Windows.Automation.InvokePattern]::Pattern)
        if ($null -ne $invoke) { $actions.Add('invoke') }
        $toggle = Pattern $element ([Windows.Automation.TogglePattern]::Pattern)
        if ($null -ne $toggle) {
            $toggleState = [int]$toggle.Current.ToggleState
            $state.checked = if ($toggleState -eq 2) { $null } else { $toggleState -eq 1 }
            $actions.Add('check'); $actions.Add('uncheck')
        }
        $value = Pattern $element ([Windows.Automation.ValuePattern]::Pattern)
        if ($null -ne $value) {
            $state.value = $value.Current.Value
            if ($state.value.Length -gt 1024) { $state.value = $state.value.Substring(0, 1024); $state.value_truncated = $true }
            if (-not $value.Current.IsReadOnly) { $actions.Add('set_value') }
        }
        $selection = Pattern $element ([Windows.Automation.SelectionItemPattern]::Pattern)
        if ($null -ne $selection) { $state.selected = [bool]$selection.Current.IsSelected; $actions.Add('select') }
    }
    return @{ runtime_id = (RuntimeId $element); name = $current.Name;
        control_type = $current.ControlType.ProgrammaticName; automation_id = $current.AutomationId;
        password = $password; state = $state; actions = @($actions.ToArray()) }
}
function Resolve($target) {
    $root = [Windows.Automation.AutomationElement]::FromHandle([IntPtr][long]$target.window.handle)
    if ($null -eq $root) { Fail 'DESKTOP_STALE_REF' }
    $window = WindowInfo $root ([long]$target.window.handle)
    foreach ($key in @('handle','process_id','process_started','runtime_id')) {
        if ([string]$window[$key] -cne [string]$target.window.$key) { Fail 'DESKTOP_STALE_REF' }
    }
    $walker = [Windows.Automation.TreeWalker]::ControlViewWalker
    $stack = [Collections.Generic.Stack[object]]::new()
    $stack.Push($root)
    $found = $null
    while ($stack.Count -gt 0) {
        Budget
        $element = $stack.Pop()
        if ((RuntimeId $element) -ceq $target.runtime_id) {
            if ($null -ne $found) { Fail 'DESKTOP_AMBIGUOUS_TARGET' }
            $found = $element
        }
        $child = $walker.GetFirstChild($element)
        while ($null -ne $child) {
            Budget
            $stack.Push($child)
            $child = $walker.GetNextSibling($child)
        }
    }
    if ($null -eq $found) { Fail 'DESKTOP_STALE_REF' }
    $node = ReadNode $found
    if ($node.name -cne $target.name -or $node.control_type -cne $target.control_type -or
        $node.automation_id -cne $target.automation_id) { Fail 'DESKTOP_STALE_REF' }
    if ($node.password) { Fail 'DESKTOP_PASSWORD_CONTROL' }
    return @{ root = $root; element = $found; window = $window; node = $node }
}

try {
    Add-Type -AssemblyName UIAutomationClient
    Add-Type -AssemblyName UIAutomationTypes
    Add-Type -TypeDefinition @'
using System;
using System.Runtime.InteropServices;
public static class AuraUiaNative {
    [DllImport("user32.dll")] public static extern IntPtr GetForegroundWindow();
}
'@
    $request = [Console]::In.ReadToEnd() | ConvertFrom-Json
    if ($request.operation -eq 'snapshot') {
        $handle = [AuraUiaNative]::GetForegroundWindow()
        if ($handle -eq [IntPtr]::Zero) { Fail 'DESKTOP_UNAVAILABLE' }
        $root = [Windows.Automation.AutomationElement]::FromHandle($handle)
        $window = WindowInfo $root $handle.ToInt64()
        $limit = [Math]::Min(120, [Math]::Max(1, [int]$request.limit))
        $nodes = [Collections.Generic.List[object]]::new()
        $queue = [Collections.Generic.Queue[object]]::new()
        $queue.Enqueue($root)
        $walker = [Windows.Automation.TreeWalker]::ControlViewWalker
        while ($queue.Count -gt 0 -and $nodes.Count -lt $limit -and $script:visited -lt 1200) {
            $element = $queue.Dequeue()
            $node = ReadNode $element
            if ($node.name -or $node.actions.Count -gt 0) { $nodes.Add($node) }
            $child = $walker.GetFirstChild($element)
            while ($null -ne $child -and $queue.Count -lt 500 -and $script:visited -lt 1200) {
                Budget
                $queue.Enqueue($child)
                $child = $walker.GetNextSibling($child)
            }
        }
        $result = @{ window = $window; nodes = @($nodes.ToArray()); truncated = $queue.Count -gt 0 }
    } elseif ($request.operation -in @('read', 'act')) {
        $resolved = Resolve $request.target
        if ($request.operation -eq 'read') { $result = @{ window = $resolved.window; node = $resolved.node } }
        else {
            if (-not $resolved.node.state.enabled) { Fail 'DESKTOP_ACCESS_DENIED' }
            $element = $resolved.element
            switch -CaseSensitive ($request.action) {
                'invoke' {
                    $pattern = Pattern $element ([Windows.Automation.InvokePattern]::Pattern)
                    if ($null -eq $pattern) { Fail 'DESKTOP_UNSUPPORTED_ACTION' }
                    $pattern.Invoke()
                }
                'set_value' {
                    $pattern = Pattern $element ([Windows.Automation.ValuePattern]::Pattern)
                    if ($null -eq $pattern -or $pattern.Current.IsReadOnly) { Fail 'DESKTOP_UNSUPPORTED_ACTION' }
                    $pattern.SetValue([string]$request.value)
                }
                { $_ -in @('check', 'uncheck') } {
                    $pattern = Pattern $element ([Windows.Automation.TogglePattern]::Pattern)
                    if ($null -eq $pattern) { Fail 'DESKTOP_UNSUPPORTED_ACTION' }
                    $wanted = $request.action -ceq 'check'
                    $state = [int]$pattern.Current.ToggleState
                    if ($state -eq 2) { Fail 'DESKTOP_UNSUPPORTED_ACTION' }
                    if (($state -eq 1) -ne $wanted) { $pattern.Toggle() }
                }
                'select' {
                    $pattern = Pattern $element ([Windows.Automation.SelectionItemPattern]::Pattern)
                    if ($null -eq $pattern) { Fail 'DESKTOP_UNSUPPORTED_ACTION' }
                    if (-not $pattern.Current.IsSelected) { $pattern.Select() }
                }
                'focus' {
                    if (-not $element.Current.IsKeyboardFocusable) { Fail 'DESKTOP_UNSUPPORTED_ACTION' }
                    $element.SetFocus()
                }
                default { Fail 'DESKTOP_UNSUPPORTED_ACTION' }
            }
            $result = @{ dispatched = $true }
        }
    } else { Fail 'DESKTOP_INVALID_ARGUMENT' }
    [Console]::Write(($result | ConvertTo-Json -Depth 12 -Compress))
} catch {
    $code = 'DESKTOP_UNAVAILABLE'
    $message = $_.Exception.Message
    if ($message -match '^DESKTOP_[A-Z_]+$') { $code = $message }
    elseif ($_.Exception -is [UnauthorizedAccessException]) { $code = 'DESKTOP_ACCESS_DENIED' }
    [Console]::Write((@{ error = $code } | ConvertTo-Json -Compress))
}
