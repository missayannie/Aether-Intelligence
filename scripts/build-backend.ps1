<#
  build-backend.ps1 — compile the Python backend into a single standalone exe
  with PyInstaller, so the packaged app needs no Python installed.

  These deps hide imports/data that PyInstaller misses without --collect-all:
    litellm (model-cost data, providers), uvicorn (protocol impls),
    curl_cffi (bundled libcurl DLL), keyring (Windows backend), pydantic core,
    claude-agent-sdk. Output: backend\dist\backend.exe
#>
$ErrorActionPreference = "Stop"
$env:Path = [System.Environment]::GetEnvironmentVariable("Path","Machine") + ";" +
            [System.Environment]::GetEnvironmentVariable("Path","User")

$root    = Split-Path -Parent $PSScriptRoot
$backend = Join-Path $root "backend"
$python  = Join-Path $backend ".venv\Scripts\python.exe"

Set-Location $backend

& $python -m PyInstaller `
  --noconfirm --clean --onefile --name backend `
  --add-data "sources\exd_schema.json;sources" `
  --collect-all litellm `
  --collect-all uvicorn `
  --collect-all curl_cffi `
  --collect-all keyring `
  --collect-all claude_agent_sdk `
  --collect-all pydantic `
  --collect-all pydantic_core `
  --collect-all tiktoken `
  --collect-all tiktoken_ext `
  --collect-submodules keyring.backends `
  --hidden-import keyring.backends.Windows `
  --collect-all win32ctypes `
  --collect-all cffi `
  --hidden-import _cffi_backend `
  --hidden-import win32ctypes.core.cffi._authentication `
  --hidden-import win32ctypes.core.ctypes._authentication `
  --hidden-import win32cred `
  --hidden-import pywintypes `
  --hidden-import win32timezone `
  --hidden-import python_multipart `
  --hidden-import pypdf `
  --hidden-import orjson `
  run_backend.py

Write-Host "Built: $(Join-Path $backend 'dist\backend.exe')" -ForegroundColor Green
