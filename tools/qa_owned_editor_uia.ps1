# Narrow QA-only UIA transport. It never enumerates the desktop/root windows.
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
[Console]::InputEncoding = [Text.UTF8Encoding]::new($false)
[Console]::OutputEncoding = [Text.UTF8Encoding]::new($false)
$script:clock = [Diagnostics.Stopwatch]::StartNew()
$script:visited = 0
function Fail([string]$code) { throw [InvalidOperationException]::new($code) }
function Budget {
    $script:visited += 1
    if ($script:visited -gt 1000 -or $script:clock.Elapsed.TotalSeconds -gt 7) { Fail 'DESKTOP_TIMEOUT' }
}
function RuntimeId($element) { return ($element.GetRuntimeId() -join '.') }
function Guard {
    Budget
    $owner = $script:request.owner
    if ([string]$owner.nonce -cnotmatch '^[a-f0-9]{32}$' -or [long]$owner.hwnd -le 0 -or [long]$owner.pid -le 0) { Fail 'DESKTOP_STALE_REF' }
    $actualPid = [uint32]0
    [OwnedEditorNative]::GetWindowThreadProcessId([IntPtr][long]$owner.hwnd, [ref]$actualPid) | Out-Null
    if ($actualPid -ne [long]$owner.pid) { Fail 'DESKTOP_STALE_REF' }
    $process = [Diagnostics.Process]::GetProcessById([int]$owner.pid)
    if ($process.StartTime.ToUniversalTime().Ticks.ToString() -cne [string]$owner.started) { Fail 'DESKTOP_STALE_REF' }
    if ([OwnedEditorNative]::GetForegroundWindow().ToInt64() -ne [long]$owner.hwnd) { Fail 'DESKTOP_ACCESS_DENIED' }
}
function Root {
    Guard
    $root = [Windows.Automation.AutomationElement]::FromHandle([IntPtr][long]$script:request.owner.hwnd)
    if ($null -eq $root -or $root.Current.ProcessId -ne [long]$script:request.owner.pid -or
        $root.Current.Name -cne ('AURA Owned Editor ' + $script:request.owner.nonce)) { Fail 'DESKTOP_STALE_REF' }
    $window = @{handle=[long]$script:request.owner.hwnd; process_id=[int]$script:request.owner.pid;
        process_started=[string]$script:request.owner.started; runtime_id=(RuntimeId $root); title=$root.Current.Name}
    if ($null -ne $script:request.window) {
        foreach ($key in @('handle','process_id','process_started','runtime_id')) {
            if ([string]$window[$key] -cne [string]$script:request.window.$key) { Fail 'DESKTOP_STALE_REF' }
        }
    }
    return @{root=$root; window=$window}
}
function Pattern($element, $identifier) {
    $value=$null
    if ($element.TryGetCurrentPattern($identifier, [ref]$value)) { return $value }
    return $null
}
function Node($element) {
    Budget
    $current=$element.Current
    if ($current.ProcessId -ne [long]$script:request.owner.pid) { Fail 'DESKTOP_STALE_REF' }
    $state=@{enabled=[bool]$current.IsEnabled; focused=[bool]$current.HasKeyboardFocus}
    $actions=@()
    if (-not $current.IsPassword) {
        $value=Pattern $element ([Windows.Automation.ValuePattern]::Pattern)
        if ($null -ne $value) {
            $state.value=$value.Current.Value
            if ($state.value.Length -gt 1024) { $state.value=$state.value.Substring(0,1024); $state.value_truncated=$true }
            if ($current.Name -ceq 'Document text' -and -not $value.Current.IsReadOnly) { $actions+= 'set_value' }
        }
        if ($current.Name -cin @('Save document','Close document','Reopen document')) {
            $invoke=Pattern $element ([Windows.Automation.InvokePattern]::Pattern)
            if ($null -ne $invoke) { $actions+='invoke' }
        }
    }
    return @{runtime_id=(RuntimeId $element); name=$current.Name; control_type=$current.ControlType.ProgrammaticName;
        automation_id=$current.AutomationId; password=[bool]$current.IsPassword; state=$state; actions=@($actions)}
}
function Walk($root) {
    $walker=[Windows.Automation.TreeWalker]::ControlViewWalker
    $stack=[Collections.Generic.Stack[object]]::new(); $stack.Push($root)
    $elements=[Collections.Generic.List[object]]::new()
    while ($stack.Count -gt 0) {
        Budget
        $element=$stack.Pop(); $elements.Add($element)
        if ($elements.Count -gt 120) { Fail 'DESKTOP_TIMEOUT' }
        $child=$walker.GetFirstChild($element)
        while ($null -ne $child) { Budget; $stack.Push($child); $child=$walker.GetNextSibling($child) }
    }
    return ,$elements
}
try {
    Add-Type -AssemblyName UIAutomationClient
    Add-Type -AssemblyName UIAutomationTypes
    Add-Type -TypeDefinition @'
using System;
using System.Runtime.InteropServices;
public static class OwnedEditorNative {
 [DllImport("user32.dll")] public static extern IntPtr GetForegroundWindow();
 [DllImport("user32.dll")] public static extern uint GetWindowThreadProcessId(IntPtr h, out uint pid);
}
'@
    $script:request=[Console]::In.ReadToEnd() | ConvertFrom-Json
    if ($script:request.operation -cnotin @('bind','snapshot','read','act')) { Fail 'DESKTOP_UNSUPPORTED_ACTION' }
    $selected=Root
    if ($script:request.operation -ceq 'bind') { $result=@{window=$selected.window} }
    elseif ($script:request.operation -ceq 'snapshot') {
        $controls=[Collections.Generic.List[object]]::new()
        foreach ($element in (Walk $selected.root)) {
            $node=Node $element
            if ($script:request.query -and $node.name.IndexOf([string]$script:request.query,[StringComparison]::OrdinalIgnoreCase) -lt 0) { continue }
            $controls.Add($node)
        }
        $offset=0; if ($null -ne $script:request.offset) { $offset=[int]$script:request.offset }
        $limit=120; if ($null -ne $script:request.limit) { $limit=[int]$script:request.limit }
        if ($offset -lt 0 -or $limit -lt 1 -or $limit -gt 120) { Fail 'DESKTOP_INVALID_ARGUMENT' }
        Guard
        $result=@{window=$selected.window; nodes=@($controls.ToArray() | Select-Object -Skip $offset -First $limit);
            total=$controls.Count; truncated=($controls.Count -gt $offset+$limit)}
    } else {
        $target=$script:request.target
        foreach ($key in @('handle','process_id','process_started','runtime_id')) {
            if ([string]$target.window.$key -cne [string]$selected.window[$key]) { Fail 'DESKTOP_STALE_REF' }
        }
        $found=$null
        foreach ($element in (Walk $selected.root)) {
            if ((RuntimeId $element) -ceq [string]$target.runtime_id) {
                if ($null -ne $found) { Fail 'DESKTOP_AMBIGUOUS_TARGET' }
                $found=$element
            }
        }
        if ($null -eq $found) { Fail 'DESKTOP_STALE_REF' }
        $node=Node $found
        foreach ($key in @('name','control_type','automation_id')) {
            if ([string]$node[$key] -cne [string]$target.$key) { Fail 'DESKTOP_STALE_REF' }
        }
        if ($node.password) { Fail 'DESKTOP_PASSWORD_CONTROL' }
        $dispatched=$false
        if ($script:request.operation -ceq 'act') {
            if (-not $node.state.enabled -or [string]$script:request.action -cnotin $node.actions) { Fail 'DESKTOP_UNSUPPORTED_ACTION' }
            # The last foreground/owner check is inside this same native
            # transport immediately before the exact UIA pattern operation.
            Guard
            if ($script:request.action -ceq 'set_value') {
                if ($script:request.value -isnot [string] -or $script:request.value.Length -gt 1024) { Fail 'DESKTOP_INVALID_ARGUMENT' }
                (Pattern $found ([Windows.Automation.ValuePattern]::Pattern)).SetValue([string]$script:request.value)
            } elseif ($script:request.action -ceq 'invoke') {
                (Pattern $found ([Windows.Automation.InvokePattern]::Pattern)).Invoke()
            } else { Fail 'DESKTOP_UNSUPPORTED_ACTION' }
            $dispatched=$true
        }
        Guard
        $result=@{window=$selected.window; node=(Node $found); dispatched=$dispatched}
    }
    [Console]::Out.WriteLine(($result | ConvertTo-Json -Depth 12 -Compress))
} catch {
    $code=$_.Exception.Message
    if ($code -cnotmatch '^DESKTOP_[A-Z_]+$') { $code='DESKTOP_UNAVAILABLE' }
    [Console]::Out.WriteLine((@{error=$code} | ConvertTo-Json -Compress))
}
