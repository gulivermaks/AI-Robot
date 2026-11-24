$ErrorActionPreference = "Stop"

$ROOT = Split-Path -Parent $MyInvocation.MyCommand.Path
$CACHE = Join-Path $ROOT ".cache"
$RUNTIME = Join-Path $ROOT "runtime"
$VENV_PY = Join-Path $ROOT ".venv\Scripts\python.exe"

foreach ($d in @(
    $CACHE,
    (Join-Path $CACHE "pip"),
    (Join-Path $CACHE "hf"),
    (Join-Path $CACHE "hf\hub"),
    (Join-Path $CACHE "torch"),
    (Join-Path $CACHE "xdg"),
    (Join-Path $CACHE "tmp"),
    (Join-Path $CACHE "whisper"),
    $RUNTIME,
    (Join-Path $RUNTIME "frames")
)) {
    New-Item -ItemType Directory -Force -Path $d | Out-Null
}

# Строго держим все кэши и временные файлы в папке проекта
$env:PIP_CACHE_DIR = Join-Path $CACHE "pip"
$env:HF_HOME = Join-Path $CACHE "hf"
$env:HUGGINGFACE_HUB_CACHE = Join-Path $CACHE "hf\hub"
$env:TORCH_HOME = Join-Path $CACHE "torch"
$env:XDG_CACHE_HOME = Join-Path $CACHE "xdg"
$env:TEMP = Join-Path $CACHE "tmp"
$env:TMP = Join-Path $CACHE "tmp"
$env:PYTHONUTF8 = "1"
$env:PYTHONIOENCODING = "utf-8"

if (-not (Test-Path $VENV_PY)) {
    Write-Error "Не найден $VENV_PY. Сначала создайте окружение .venv в корне проекта."
    exit 1
}

$healthUrl = "http://127.0.0.1:8765/health"
$serverUp = $false
try {
    $resp = Invoke-RestMethod -UseBasicParsing -Uri $healthUrl -TimeoutSec 2
    if ($resp.ok) { $serverUp = $true }
} catch {}

if (-not $serverUp) {
    Write-Host "[1/3] Запускаю локальный bridge-сервер с авто-перезапуском (watchdog)..." -ForegroundColor Cyan
    $logOut = Join-Path $RUNTIME "bridge.log"
    $logErr = Join-Path $RUNTIME "bridge.err.log"
    $stopFlag = Join-Path $RUNTIME "stop.flag"
    $watchdogScript = Join-Path $ROOT "head\watchdog.ps1"
    Remove-Item -Path $stopFlag -Force -ErrorAction SilentlyContinue
    Start-Process -FilePath "powershell.exe" `
        -ArgumentList "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", $watchdogScript `
        -WorkingDirectory $ROOT `
        -WindowStyle Hidden

    $deadline = (Get-Date).AddSeconds(60)
    while ((Get-Date) -lt $deadline) {
        Start-Sleep -Milliseconds 700
        try {
            $resp = Invoke-RestMethod -UseBasicParsing -Uri $healthUrl -TimeoutSec 2
            if ($resp.ok -and $resp.stt_ready) {
                $serverUp = $true
                Write-Host ("      Сервер готов: STT=" + $resp.stt_device + " (" + $resp.stt_compute_type + ")") -ForegroundColor Green
                break
            }
        } catch {}
    }
    if (-not $serverUp) {
        Write-Warning "Сервер ещё загружается или возникла ошибка. Проверьте $logErr"
    }
} else {
    Write-Host "[1/3] Bridge-сервер уже работает на http://127.0.0.1:8765" -ForegroundColor Green
}

Write-Host "[2/3] Настраиваю Android-телефон (голова) через ADB (adb reverse tcp:8765)..." -ForegroundColor Cyan
& $VENV_PY (Join-Path $ROOT "head\adb_setup.py")
if ($LASTEXITCODE -ne 0) {
    Write-Warning "Android-телефон (голова) не ответил по ADB. Разблокируйте телефон / проверьте Wi-Fi ADB или подключите USB и повторите head\adb_setup.py."
}

Write-Host "[3/3] Проверяю связь с пылесосом Xiaomi..." -ForegroundColor Cyan
& $VENV_PY (Join-Path $ROOT "robot_cli.py") status
if ($LASTEXITCODE -ne 0) {
    Write-Warning "Пылесос ещё не авторизован или не в сети. Для входа по QR выполните: .\.venv\Scripts\python.exe body\discover_xiaomi.py --qr --open"
}

$pcIp = Get-NetIPAddress -AddressFamily IPv4 -ErrorAction SilentlyContinue |
    Where-Object { $_.IPAddress -notlike '127.*' -and $_.IPAddress -notlike '169.254.*' -and $_.PrefixOrigin -ne 'WellKnown' } |
    Select-Object -ExpandProperty IPAddress -First 1
if (-not $pcIp) { $pcIp = "127.0.0.1" }

Write-Host ""
Write-Host ("Веб-пульт управления: http://" + $pcIp + ":8765/panel") -ForegroundColor Green
Write-Host "Авто-перезапуск watchdog активен (остановка: .\stop.ps1)" -ForegroundColor Cyan
Write-Host ""
Write-Host "Готово! Основные команды агента:" -ForegroundColor Yellow
Write-Host "  .\.venv\Scripts\python.exe robot_cli.py health"
Write-Host "  .\.venv\Scripts\python.exe robot_cli.py frame"
Write-Host "  .\.venv\Scripts\python.exe robot_cli.py say `"Привет! Я готов.`""
Write-Host "  .\.venv\Scripts\python.exe robot_cli.py listen 15"
Write-Host "  .\.venv\Scripts\python.exe robot_cli.py step f0.5 --say `"Пробую шаг вперёд`""
Write-Host "Промпт для ИИ-агента: prompts\voice_robot_prompt.md" -ForegroundColor DarkGray
