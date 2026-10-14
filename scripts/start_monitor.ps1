# Starts the co-pilot monitoring loop under its watchdog as an independent hidden background process
# (analysis + alerts only; it never places orders). The watchdog restarts the monitor if it crashes and sends a
# Telegram alert. Log: data\logs\monitor.log (rotated at 10 MB, 5 kept). Refuses to start a second copy.
#   -EnsureTradingView : if TradingView Desktop is not running, start it with the CDP port the monitor reads from
#   -DelaySeconds N    : wait first (used at logon so the network / TradingView login can settle)
param([switch]$EnsureTradingView, [int]$DelaySeconds = 0)
$root = Split-Path -Parent $PSScriptRoot
if ($DelaySeconds -gt 0) { Start-Sleep -Seconds $DelaySeconds }

if ($EnsureTradingView) {
    $tv = Get-CimInstance Win32_Process -Filter "Name='TradingView.exe'"
    if (-not $tv) {
        $pkg = Get-AppxPackage -Name "TradingView.Desktop" | Select-Object -First 1
        if ($pkg) {
            Start-Process -FilePath (Join-Path $pkg.InstallLocation "TradingView.exe") -ArgumentList "--remote-debugging-port=9222"
            Write-Host "TradingView started with the CDP port."
            Start-Sleep -Seconds 20
        } else {
            Write-Host "TradingView Desktop package not found -- the monitor will report TradingView as unreachable."
        }
    } elseif (-not ($tv | Where-Object { $_.CommandLine -like '*--remote-debugging-port=9222*' })) {
        Write-Host "TradingView is running WITHOUT the CDP port -- close it; the next start will open it correctly."
    }
}

$running = Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
           Where-Object { $_.CommandLine -like '*copilot supervise*' -or $_.CommandLine -like '*copilot run*' }
if ($running) {
    Write-Host ("Monitor already running (PID " + ($running.ProcessId -join ", ") + "). Nothing started.")
    exit 0
}
New-Item -ItemType Directory -Force -Path "$root\data\logs" | Out-Null
$env:PYTHONIOENCODING = "utf-8"
$py = if (Test-Path "$root\.venv\Scripts\python.exe") { "$root\.venv\Scripts\python.exe" } else { "python" }   # project venv first
$p = Start-Process -FilePath $py -ArgumentList "-u", "-m", "copilot", "supervise" -WorkingDirectory "$root\src" `
     -WindowStyle Hidden -RedirectStandardOutput "$root\data\logs\supervisor.out.log" `
     -RedirectStandardError "$root\data\logs\supervisor.err.log" -PassThru
Write-Host ("Monitor started under watchdog (PID " + $p.Id + "). Log: $root\data\logs\monitor.log")
