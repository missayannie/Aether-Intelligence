<#
  mirror-to-drive.ps1
  Copies the SOURCE of this project from the local working copy (C:) back to the
  Google Drive folder, so it stays visible/backed-up on your Mac. Build artifacts
  (node_modules, target, dist, venvs) are excluded — they must never touch Drive.

  Usage:  pwsh -File scripts\mirror-to-drive.ps1
#>

$ErrorActionPreference = "Stop"

$source = "C:\Users\cryst\Projects\ffxiv-guide"
$drive  = "G:\Other computers\My Mac\Documents\Projects\ffxiv-guide"

# Directories to skip entirely (heavy / machine-specific / regenerable).
$excludeDirs = @(
    "node_modules",
    "target",
    "dist",
    "build",         # PyInstaller intermediates
    "binaries",      # bundled sidecar exe (~147MB)
    "data",          # per-user chat data
    ".vite",
    ".venv",
    "venv",
    "__pycache__"
)

# Files to skip.
$excludeFiles = @("*.log", "*.pyc", ".env", "*.local")

Write-Host "Mirroring source -> Drive (build artifacts excluded)..." -ForegroundColor Cyan

# /MIR mirrors (deletes extra files on the Drive side so it tracks source).
# /XD excludes directories; /XF excludes files. /R:1 /W:1 keeps retries short on
# the flaky Drive filesystem. Exit codes 0-7 are success for robocopy.
robocopy $source $drive /MIR /XD $excludeDirs /XF $excludeFiles /R:1 /W:1 /NFL /NDL /NP /NJH

$code = $LASTEXITCODE
if ($code -ge 8) {
    Write-Host "Mirror FAILED (robocopy exit $code)." -ForegroundColor Red
    exit 1
}
Write-Host "Mirror complete (robocopy exit $code)." -ForegroundColor Green
