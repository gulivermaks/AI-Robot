param(
    [switch]$KeepAdb
)

$ErrorActionPreference = "Stop"

$ROOT = Split-Path -Parent $MyInvocation.MyCommand.Path
$RUNTIME = Join-Path $ROOT "runtime"
$STOP_FLAG = Join-Path $RUNTIME "stop.flag"
$PID_FILE = Join-Path $RUNTIME "watchdog.pid"

# Mark intentional stop first so watchdog exits instead of restarting the server
try {
    New-Item -ItemType Directory -Force -Path $RUNTIME | Out-Null
    Set-Content -Path $STOP_FLAG -Value (Get-Date -Format o) -Encoding ascii
} catch {}

# Gracefully exit vacuum manual mode if bridge is still running
try {
    Invoke-RestMethod -Method Post -Uri "http://127.0.0.1:8765/vacuum/exit" -TimeoutSec 2 -ErrorAction SilentlyContinue | Out-Null
} catch {}

$stoppedWatchdogs = [System.Collections.Generic.HashSet[int]]::new()
if (Test-Path $PID_FILE) {
    try {
        $wdPid = [int](Get-Content $PID_FILE -ErrorAction SilentlyContinue | Select-Object -First 1)
        if ($wdPid -gt 0 -and $stoppedWatchdogs.Add($wdPid)) {
            Stop-Process -Id $wdPid -Force -ErrorAction SilentlyContinue
        }
    } catch {}
    Remove-Item -Path $PID_FILE -Force -ErrorAction SilentlyContinue
}
Get-CimInstance Win32_Process -Filter "Name = 'powershell.exe' OR Name = 'pwsh.exe'" -ErrorAction SilentlyContinue | Where-Object {
    $_.CommandLine -match 'head[\\/]watchdog\.ps1'
} | ForEach-Object {
    $pidVal = [int]$_.ProcessId
    if ($pidVal -gt 0 -and $stoppedWatchdogs.Add($pidVal)) {
        Stop-Process -Id $pidVal -Force -ErrorAction SilentlyContinue
    }
}
if ($stoppedWatchdogs.Count -gt 0) {
    Write-Host ("Watchdog остановлен (PID: " + ($stoppedWatchdogs -join ", ") + ").") -ForegroundColor Cyan
}

$stoppedPids = [System.Collections.Generic.HashSet[int]]::new()

Get-NetTCPConnection -LocalPort 8765 -State Listen -ErrorAction SilentlyContinue | ForEach-Object {
    $pidVal = [int]$_.OwningProcess
    if ($pidVal -gt 0 -and $stoppedPids.Add($pidVal)) {
        Stop-Process -Id $pidVal -Force -ErrorAction SilentlyContinue
    }
}

Get-CimInstance Win32_Process -Filter "Name = 'python.exe'" -ErrorAction SilentlyContinue | Where-Object {
    $_.CommandLine -match 'head\.bridge_server'
} | ForEach-Object {
    $pidVal = [int]$_.ProcessId
    if ($pidVal -gt 0 -and $stoppedPids.Add($pidVal)) {
        Stop-Process -Id $pidVal -Force -ErrorAction SilentlyContinue
    }
}

if ($stoppedPids.Count -gt 0) {
    Write-Host ("Bridge-сервер остановлен (PID: " + ($stoppedPids -join ", ") + ").") -ForegroundColor Green
} else {
    Write-Host "Bridge-сервер уже остановлен (порт 8765 свободен)." -ForegroundColor Yellow
}

if (-not $KeepAdb) {
    $adbListening = Get-NetTCPConnection -LocalPort 5037 -State Listen -ErrorAction SilentlyContinue
    if ($adbListening) {
        $adbCmd = Get-Command adb -ErrorAction SilentlyContinue
        $adbPath = if ($adbCmd) { $adbCmd.Source } else { Join-Path $ROOT "platform-tools\adb.exe" }
        if (Test-Path $adbPath) {
            $prevPref = $ErrorActionPreference
            $ErrorActionPreference = "Continue"
            & $adbPath kill-server 2>&1 | Out-Null
            $ErrorActionPreference = $prevPref
            Write-Host "Локальный ADB-сервер остановлен." -ForegroundColor Cyan
        }
    } else {
        Write-Host "Локальный ADB-сервер уже остановлен." -ForegroundColor DarkGray
    }
}
