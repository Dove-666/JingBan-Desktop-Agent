param([long]$Handle,[int]$ExpectedPid,[string]$Action='inspect',[string]$Name='',
      [string]$Type='', [string]$Value='', [string]$Keys='')
$ErrorActionPreference='Stop'
[Console]::OutputEncoding=[Text.UTF8Encoding]::new($false)
Add-Type -AssemblyName UIAutomationClient
Add-Type -AssemblyName UIAutomationTypes
Add-Type @'
using System;
using System.Runtime.InteropServices;
public static class ReleaseOwnedNative {
 [DllImport("user32.dll")]public static extern uint GetWindowThreadProcessId(IntPtr h,out uint pid);
 [DllImport("user32.dll",EntryPoint="PostMessageW",CharSet=CharSet.Unicode)]public static extern bool PostMessage(IntPtr h,uint msg,IntPtr w,IntPtr l);
}
'@
$hwnd=[IntPtr]$Handle
[uint32]$owner=0
[ReleaseOwnedNative]::GetWindowThreadProcessId($hwnd,[ref]$owner)|Out-Null
if($owner -ne $ExpectedPid){throw 'Window is not owned by the acceptance process.'}
if($Action -eq 'close'){
 [ReleaseOwnedNative]::PostMessage($hwnd,0x10,[IntPtr]::Zero,[IntPtr]::Zero)|Out-Null
 '{"completed":"close"}'
 exit
}
$root=[System.Windows.Automation.AutomationElement]::FromHandle($hwnd)
$nodes=$root.FindAll([System.Windows.Automation.TreeScope]::Descendants,[System.Windows.Automation.Condition]::TrueCondition)
if($Action -eq 'inspect'){
 @($nodes|ForEach-Object{
  $i=$_.Current
  if(-not $i.IsPassword){@{name=$i.Name;type=$i.ControlType.ProgrammaticName;
    enabled=$i.IsEnabled;offscreen=$i.IsOffscreen;patterns=($_.GetSupportedPatterns().ProgrammaticName -join ',')}}
 })|ConvertTo-Json -Depth 3 -Compress
 exit
}
$matches=@($nodes|Where-Object{$_.Current.Name -eq $Name -and (-not $Type -or $_.Current.ControlType.ProgrammaticName -eq $Type)})
if($matches.Count -ne 1){throw "Expected one owned control: $Name ($($matches.Count) found)."}
$target=$matches[0]
if(-not $target.Current.IsEnabled){throw 'Owned target is disabled.'}
switch($Action){
 'invoke'{([System.Windows.Automation.InvokePattern]$target.GetCurrentPattern([System.Windows.Automation.InvokePattern]::Pattern)).Invoke()}
 'select'{([System.Windows.Automation.SelectionItemPattern]$target.GetCurrentPattern([System.Windows.Automation.SelectionItemPattern]::Pattern)).Select()}
 'set'{([System.Windows.Automation.ValuePattern]$target.GetCurrentPattern([System.Windows.Automation.ValuePattern]::Pattern)).SetValue($Value)}
 'type'{
  $target.SetFocus()
  Start-Sleep -Milliseconds 200
  foreach($character in $Value.ToCharArray()){
   [ReleaseOwnedNative]::PostMessage($hwnd,0x102,[IntPtr]([int][char]$character),[IntPtr]1)|Out-Null
   Start-Sleep -Milliseconds 8
  }
 }
 'value'{
  if($target.Current.IsPassword){throw 'Password fields are never read.'}
  @{value=([System.Windows.Automation.ValuePattern]$target.GetCurrentPattern([System.Windows.Automation.ValuePattern]::Pattern)).Current.Value}|ConvertTo-Json -Compress
  exit
 }
 'keys'{
  $target.SetFocus()
  foreach($keyText in $Keys.Split(',')){
   $key=[int]$keyText
   [ReleaseOwnedNative]::PostMessage($hwnd,0x100,[IntPtr]$key,[IntPtr]1)|Out-Null
   [ReleaseOwnedNative]::PostMessage($hwnd,0x101,[IntPtr]$key,[IntPtr](-1073741823))|Out-Null
   Start-Sleep -Milliseconds 90
  }
 }
 default{throw 'Unsupported owned action.'}
}
'{"completed":"owned_action"}'
