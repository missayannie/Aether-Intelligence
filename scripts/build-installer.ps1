<#
  build-installer.ps1 — produce the standalone Windows installer end to end.

  1. PyInstaller-compiles the Python backend into backend.exe
  2. Copies it in as the Tauri sidecar (with the required target-triple name)
  3. Runs `tauri build` to produce the .msi / .exe installers

  Output installers land in:
    app\src-tauri\target\release\bundle\msi\   and   \nsis\

  Run:  pwsh -File scripts\build-installer.ps1
#>
$ErrorActionPreference = "Stop"
$env:Path = [System.Environment]::GetEnvironmentVariable("Path","Machine") + ";" +
            [System.Environment]::GetEnvironmentVariable("Path","User")

$root = Split-Path -Parent $PSScriptRoot

Write-Host "[1/3] Building the Python backend (PyInstaller)..." -ForegroundColor Cyan
& (Join-Path $PSScriptRoot "build-backend.ps1")

Write-Host "[2/3] Placing the backend as the Tauri sidecar..." -ForegroundColor Cyan
$triple = (rustc -Vv | Select-String "host:").ToString().Split()[-1]
$bin = Join-Path $root "app\src-tauri\binaries"
New-Item -ItemType Directory -Force -Path $bin | Out-Null
Copy-Item (Join-Path $root "backend\dist\backend.exe") (Join-Path $bin "backend-$triple.exe") -Force
Write-Host "      sidecar: backend-$triple.exe" -ForegroundColor DarkGray

Write-Host "[3/3] Building the installer (tauri build)..." -ForegroundColor Cyan
Set-Location (Join-Path $root "app")
npm run tauri build

Write-Host "`nDone. Installers are in app\src-tauri\target\release\bundle\" -ForegroundColor Green
