$ErrorActionPreference = "Continue"

$HEAD_DIR = Split-Path -Parent $MyInvocation.MyCommand.Path
$ROOT = Split-Path -Parent $HEAD_DIR
$CACHE = Join-Path $ROOT ".cache"
$RUNTIME = Join-Path $ROOT "runtime"
$VENV_PY = Join-Path $ROOT ".venv\Scripts\python.exe"
$PID_FILE = Join-Path $RUNTIME "watchdog.pid"
$STOP_FLAG = Join-Path $RUNTIME "stop.flag"
$LOG_OUT = Join-Path $RUNTIME "bridge.log"
$LOG_ERR = Join-Path $RUNTIME "bridge.err.log"

$env:PIP_CACHE_DIR = Join-Path $CACHE "pip"
$env:HF_HOME = Join-Path $CACHE "hf"
$env:HUGGINGFACE_HUB_CACHE = Join-Path $CACHE "hf\hub"
$env:TORCH_HOME = Join-Path $CACHE "torch"
$env:XDG_CACHE_HOME = Join-Path $CACHE "xdg"
$env:TEMP = Join-Path $CACHE "tmp"
$env:TMP = Join-Path $CACHE "tmp"
$env:PYTHONUTF8 = "1"
$env:PYTHONIOENCODING = "utf-8"

New-Item -ItemType Directory -Force -Path $RUNTIME | Out-Null
Remove-Item -Path $STOP_FLAG -Force -ErrorAction SilentlyContinue
Set-Content -Path $PID_FILE -Value $PID -Encoding ascii

Set-Location $ROOT

try {
    while (-not (Test-Path $STOP_FLAG)) {
        & $VENV_PY -m head.bridge_server >> $LOG_OUT 2>> $LOG_ERR
        $code = $LASTEXITCODE
        if (Test-Path $STOP_FLAG) {
            break
        }
        $ts = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
        Add-Content -Path $LOG_ERR -Value "[$ts] [watchdog] bridge_server exited with code $code; restarting in 2s..." -Encoding utf8
        Start-Sleep -Seconds 2
        if (Test-Path $STOP_FLAG) {
            break
        }
        try {
            & $VENV_PY (Join-Path $HEAD_DIR "adb_setup.py") --no-open >> $LOG_OUT 2>> $LOG_ERR
        } catch {}
    }
} finally {
    Remove-Item -Path $PID_FILE -Force -ErrorAction SilentlyContinue
}
