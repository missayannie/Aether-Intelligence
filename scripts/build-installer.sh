#!/usr/bin/env bash
# build-installer.sh — produce the macOS installer end to end (.dmg / .app).
# The Windows equivalent is build-installer.ps1. Run ON a Mac.
#
#   1. PyInstaller-compiles the backend
#   2. Places it as the Tauri sidecar (named with the Mac target triple)
#   3. tauri build -> .app + .dmg
#
# Output: app/src-tauri/target/release/bundle/{macos,dmg}/
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"

echo "[1/3] Building the Python backend (PyInstaller)…"
bash "$ROOT/scripts/build-backend.sh"

echo "[2/3] Placing the backend as the Tauri sidecar…"
TRIPLE="$(rustc -Vv | awk '/^host:/ {print $2}')"   # e.g. aarch64-apple-darwin
BIN="$ROOT/app/src-tauri/binaries"
mkdir -p "$BIN"
cp "$ROOT/backend/dist/backend" "$BIN/backend-$TRIPLE"
chmod +x "$BIN/backend-$TRIPLE"
echo "      sidecar: backend-$TRIPLE"

echo "[3/3] Building the installer (tauri build)…"
cd "$ROOT/app"
npm run tauri build

echo ""
echo "Done. App + .dmg are in app/src-tauri/target/release/bundle/"
