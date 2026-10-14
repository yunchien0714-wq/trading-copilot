# Stops the co-pilot monitoring loop: the watchdog first (so it does not restart the monitor), then the monitor.
$procs = Get-CimInstance Win32_Process -Filter "Name='python.exe'"
$sup = $procs | Where-Object { $_.CommandLine -like '*copilot supervise*' }
$mon = $procs | Where-Object { $_.CommandLine -like '*copilot run*' }
if (-not $sup -and -not $mon) { Write-Host "Monitor is not running."; exit 0 }
foreach ($p in @($sup) + @($mon)) {
    if ($p) { Stop-Process -Id $p.ProcessId -Confirm:$false -ErrorAction SilentlyContinue; Write-Host ("Stopped PID " + $p.ProcessId) }
}
