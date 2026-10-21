#!/usr/bin/env bash
# Forge account install — wheel runtime + user-scoped environment.
# Works from POSIX shells and native Windows Git Bash.
# The default installs a built wheel (no checkout dependence). Pass --editable
# for the development flow (CLI tied to this checkout).
set -euo pipefail

_forge_is_sourced=0
if [[ "${BASH_SOURCE[0]}" != "$0" ]]; then
  _forge_is_sourced=1
fi
_forge_original_dir=$PWD
_forge_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
cd "$_forge_root"

_forge_is_git_bash=0
case "${MSYSTEM:-}:${OSTYPE:-}" in
  *MSYS*|*MINGW*|*UCRT*|*CYGWIN*) _forge_is_git_bash=1 ;;
esac
_forge_dry_run=0
for arg in "$@"; do
  if [ "$arg" = "--dry-run" ]; then _forge_dry_run=1; break; fi
done

PY=""
PY_ARGS=()
for candidate in python3 python py; do
  if command -v "$candidate" >/dev/null 2>&1; then
    PY="$(command -v "$candidate")"
    case "$(basename -- "$PY")" in
      py|py.exe) PY_ARGS=(-3) ;;
    esac
    break
  fi
done
if [ -z "$PY" ]; then
  echo "install: no python on PATH (need >=3.11)" >&2
  cd "$_forge_original_dir"
  if [ "$_forge_is_sourced" -eq 1 ]; then return 2; fi
  exit 2
fi
ARGS=(--persist-env --allow-download "$@")
if "$PY" "${PY_ARGS[@]}" scripts/forge_bootstrap.py "${ARGS[@]}"; then
  status=0
else
  status=$?
fi
if [ "$status" -eq 0 ]; then
  env_file="${XDG_CONFIG_HOME:-$HOME/.config}/forjas/env.sh"
  if [ "$_forge_dry_run" -eq 1 ]; then
    echo "Install plan complete. No files, variables or registry entries were written."
  elif [ "$_forge_is_git_bash" -eq 1 ]; then
    echo "Install complete. Windows user variables saved in HKCU and Git Bash environment saved in: $env_file"
    echo "Reload current Git Bash with: source '$env_file'"
  else
    echo "Install complete. Open a new shell or run: source '$env_file'"
  fi
  if [ "$_forge_is_sourced" -eq 1 ] && [ -f "$env_file" ]; then
    # A sourced installer can update its caller; an executed child cannot.
    # shellcheck disable=SC1090
    . "$env_file"
  fi
fi
cd "$_forge_original_dir"
if [ "$_forge_is_sourced" -eq 1 ]; then
  return "$status"
fi
exit "$status"
