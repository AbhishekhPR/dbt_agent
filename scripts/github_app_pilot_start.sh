#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
"${repo_root}/scripts/github_app_pilot_preflight.sh"

if [[ -n "${VIRTUAL_ENV:-}" && -x "${VIRTUAL_ENV}/bin/python" ]]; then
  python_bin="${VIRTUAL_ENV}/bin/python"
elif command -v python >/dev/null 2>&1; then
  python_bin="$(command -v python)"
elif command -v python3 >/dev/null 2>&1; then
  python_bin="$(command -v python3)"
else
  printf 'Relium pilot startup error: activate the virtual environment or install Python.\n' >&2
  exit 2
fi

cd "$repo_root"
exec "$python_bin" -m agent.github_app.server
