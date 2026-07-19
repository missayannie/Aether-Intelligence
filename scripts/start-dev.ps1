<#
  start-dev.ps1 — launch Aether Intelligence in dev mode.
  Starts the Python backend in its own window, waits for it to be healthy, then
  launches the desktop app. First run of the app compiles Rust (a few minutes).

  Run from anywhere:  pwsh -File scripts\start-dev.ps1
#>

$ErrorActionPreference = "Stop"

# Make sure freshly-installed tools are on PATH.
$env:Path = [System.Environment]::GetEnvironmentVariable("Path","Machine") + ";" +
            [System.Environment]::GetEnvironmentVariable("Path","User")

$root    = Split-Path -Parent $PSScriptRoot
$backend = Join-Path $root "backend"
$app     = Join-Path $root "app"
$python  = Join-Path $backend ".venv\Scripts\python.exe"

Write-Host "Starting backend in a new window..." -ForegroundColor Cyan
Start-Process powershell -ArgumentList @(
  "-NoExit", "-Command",
  "`$env:PYTHONIOENCODING='utf-8'; Set-Location '$backend'; " +
  "& '$python' -m uvicorn app:app --host 127.0.0.1 --port 8756"
)

Write-Host "Waiting for backend to be ready..." -ForegroundColor Cyan
$ready = $false
for ($i = 0; $i -lt 30; $i++) {
    try {
        Invoke-RestMethod "http://127.0.0.1:8756/health" -TimeoutSec 2 | Out-Null
        $ready = $true; break
    } catch { Start-Sleep -Milliseconds 700 }
}
if (-not $ready) {
    Write-Host "Backend did not respond. Check the backend window for errors." -ForegroundColor Red
    exit 1
}
Write-Host "Backend is up." -ForegroundColor Green

Write-Host "Launching the app (first run compiles Rust — be patient)..." -ForegroundColor Cyan
Set-Location $app
npm run tauri dev
