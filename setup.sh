#!/usr/bin/env bash
# Forge bootstrap — vendored entry point. See scripts/forge_bootstrap.py.
set -euo pipefail
cd "$(dirname "$0")"
PY="$(command -v python3 || command -v python || command -v py)"
if [ -z "$PY" ]; then
  echo "setup: no python on PATH (need >=3.11)" >&2; exit 2
fi
if [ "${PY##*/}" = "py" ]; then exec "$PY" -3 scripts/forge_bootstrap.py "$@"; fi
exec "$PY" scripts/forge_bootstrap.py "$@"
