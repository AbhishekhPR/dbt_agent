#!/usr/bin/env bash
set -euo pipefail

fail() {
  printf 'Relium pilot preflight error: %s\n' "$1" >&2
  exit 2
}

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
cd "$repo_root"
export PYTHONPATH="${repo_root}:${PYTHONPATH:-}"

if [[ -n "${VIRTUAL_ENV:-}" && -x "${VIRTUAL_ENV}/bin/python" ]]; then
  python_bin="${VIRTUAL_ENV}/bin/python"
elif command -v python >/dev/null 2>&1; then
  python_bin="$(command -v python)"
elif command -v python3 >/dev/null 2>&1; then
  python_bin="$(command -v python3)"
else
  fail "activate the virtual environment or install Python."
fi

required_variables=(
  RELIUM_GITHUB_APP_ID
  RELIUM_GITHUB_WEBHOOK_SECRET
  RELIUM_GITHUB_PRIVATE_KEY_PATH
  RELIUM_STORAGE_ROOT
)
for variable_name in "${required_variables[@]}"; do
  if [[ -z "${!variable_name:-}" ]]; then
    fail "${variable_name} is required."
  fi
done

if [[ ! -f "$RELIUM_GITHUB_PRIVATE_KEY_PATH" || ! -r "$RELIUM_GITHUB_PRIVATE_KEY_PATH" ]]; then
  fail "RELIUM_GITHUB_PRIVATE_KEY_PATH must name a readable file."
fi

if ! mkdir -p -- "$RELIUM_STORAGE_ROOT" 2>/dev/null; then
  fail "RELIUM_STORAGE_ROOT could not be created."
fi
if [[ ! -d "$RELIUM_STORAGE_ROOT" || ! -w "$RELIUM_STORAGE_ROOT" ]]; then
  fail "RELIUM_STORAGE_ROOT must be a writable directory."
fi

if ! "$python_bin" -B -c \
  'import sys; sys.path.insert(0, "'"${repo_root}"'"); import cryptography, httpx, starlette, uvicorn, yaml; from agent.github_app.server import main' \
  >/dev/null 2>&1; then
  fail "required Python dependencies could not be imported."
fi

if ! "$python_bin" -B - <<'PY' >/dev/null 2>&1
import os
import socket

host = os.environ.get("RELIUM_HOST", "127.0.0.1")
port = int(os.environ.get("RELIUM_PORT", "8000"))
with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
    probe.bind((host, port))
PY
then
  fail "the configured host or port is unavailable."
fi

if ! "$python_bin" -B - <<'PY' >/dev/null 2>&1
import sys
sys.path.insert(0, """$repo_root""")
from agent.github_app.settings import load_settings

load_settings()
PY
then
  fail "GitHub App server configuration is invalid."
fi

printf 'Relium GitHub App pilot preflight passed.\n'
