# Removes the logon autostart shortcut created by install_autostart.ps1. The running monitor is not stopped.
$lnk = Join-Path ([Environment]::GetFolderPath("Startup")) "Trading Copilot Monitor.lnk"
if (Test-Path $lnk) { Remove-Item $lnk -Confirm:$false; Write-Host "Autostart removed." } else { Write-Host "Autostart was not installed." }
