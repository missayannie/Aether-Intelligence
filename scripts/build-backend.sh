#!/usr/bin/env bash
# build-backend.sh — compile the Python backend into a standalone binary with
# PyInstaller (macOS / Linux). The Windows equivalent is build-backend.ps1.
# Output: backend/dist/backend
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
BACKEND="$ROOT/backend"
PY="$BACKEND/.venv/bin/python"

cd "$BACKEND"
"$PY" -m PyInstaller \
  --noconfirm --clean --onefile --name backend \
  --collect-all litellm \
  --collect-all uvicorn \
  --collect-all curl_cffi \
  --collect-all keyring \
  --collect-all claude_agent_sdk \
  --collect-all pydantic \
  --collect-all pydantic_core \
  --collect-all tiktoken \
  --collect-all tiktoken_ext \
  --collect-submodules keyring.backends \
  --hidden-import keyring.backends.macOS \
  --hidden-import keyring.backends.SecretService \
  --hidden-import python_multipart \
  --hidden-import pypdf \
  run_backend.py

echo "Built: $BACKEND/dist/backend"
