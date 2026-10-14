# Starts the monitor automatically when you log on to Windows: a shortcut in your own Startup folder (no admin
# rights, no system settings). It waits 60 s, opens TradingView with the CDP port if it is not running, then starts
# the monitor under its watchdog. Remove with uninstall_autostart.ps1 (or delete the shortcut).
$root = Split-Path -Parent $PSScriptRoot
$lnk = Join-Path ([Environment]::GetFolderPath("Startup")) "Trading Copilot Monitor.lnk"
$ws = New-Object -ComObject WScript.Shell
$s = $ws.CreateShortcut($lnk)
$s.TargetPath = "$env:SystemRoot\System32\WindowsPowerShell\v1.0\powershell.exe"
$s.Arguments = "-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$root\scripts\start_monitor.ps1`" -EnsureTradingView -DelaySeconds 60"
$s.WorkingDirectory = "$root\scripts"
$s.WindowStyle = 7
$s.Description = "Trading Co-Pilot monitor (analysis and alerts only; never places orders)"
$s.Save()
Write-Host "Autostart installed: $lnk"
