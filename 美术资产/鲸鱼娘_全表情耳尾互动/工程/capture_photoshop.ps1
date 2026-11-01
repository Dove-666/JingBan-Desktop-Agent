$ErrorActionPreference = 'Stop'
$photoshop = New-Object -ComObject Photoshop.Application
$photoshop.Visible = $true
$photoshop.DoJavaScriptFile((Join-Path $PSScriptRoot 'open_evidence.jsx'))
Add-Type -AssemblyName System.Drawing
Add-Type @'
using System;
using System.Runtime.InteropServices;
public class NativePSCapture {
 [StructLayout(LayoutKind.Sequential)] public struct RECT { public int Left,Top,Right,Bottom; }
 [DllImport("user32.dll")] public static extern bool SetProcessDPIAware();
 [DllImport("user32.dll")] public static extern bool ShowWindow(IntPtr h,int cmd);
 [DllImport("user32.dll")] public static extern bool GetWindowRect(IntPtr h,out RECT r);
 [DllImport("user32.dll")] public static extern bool PrintWindow(IntPtr h,IntPtr dc,uint flags);
}
'@
[NativePSCapture]::SetProcessDPIAware() | Out-Null
$process = Get-Process Photoshop | Where-Object MainWindowHandle -ne 0 | Select-Object -First 1
[NativePSCapture]::ShowWindow($process.MainWindowHandle,3) | Out-Null
Start-Sleep -Milliseconds 500
$rect = New-Object NativePSCapture+RECT
[NativePSCapture]::GetWindowRect($process.MainWindowHandle,[ref]$rect) | Out-Null
$bitmap = New-Object System.Drawing.Bitmap(($rect.Right-$rect.Left),($rect.Bottom-$rect.Top))
$graphics = [System.Drawing.Graphics]::FromImage($bitmap)
$dc = $graphics.GetHdc()
[NativePSCapture]::PrintWindow($process.MainWindowHandle,$dc,2) | Out-Null
$graphics.ReleaseHdc($dc)
$bitmap.Save((Join-Path $PSScriptRoot 'Photoshop_native_layers.png'),[System.Drawing.Imaging.ImageFormat]::Png)
$graphics.Dispose(); $bitmap.Dispose()
