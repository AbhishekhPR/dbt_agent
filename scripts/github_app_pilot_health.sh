#!/usr/bin/env bash
set -euo pipefail

port="${RELIUM_PORT:-8000}"
url="http://127.0.0.1:${port}/healthz"

if ! response="$(curl --fail --silent --show-error "$url")"; then
  printf 'Relium pilot health check failed.\n' >&2
  exit 1
fi

compact_response="$(printf '%s' "$response" | tr -d '[:space:]')"
case "$compact_response" in
  *'"status":"ok"'*)
    printf 'Relium GitHub App is healthy.\n'
    ;;
  *)
    printf 'Relium pilot health check returned an unhealthy response.\n' >&2
    exit 1
    ;;
esac
