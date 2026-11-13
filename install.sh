#!/usr/bin/env bash
# Forge account install — wheel runtime + user-scoped environment.
# Works from POSIX shells and native Windows Git Bash.
# The default installs a built wheel (no checkout dependence). Pass --editable
# for the development flow (CLI tied to this checkout).
#
# Presentation: the animated UI lives in scripts/forge_bootstrap.py (stderr);
# this wrapper only styles its own pre/post messages. Colors are TTY-gated
# and honor NO_COLOR / CI / TERM=dumb — piped runs stay plain.
set -euo pipefail

_forge_is_sourced=0
if [[ "${BASH_SOURCE[0]}" != "$0" ]]; then
  _forge_is_sourced=1
fi
_forge_original_dir=$PWD
_forge_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
cd "$_forge_root"

# -- decoration (plain when not interactive) -------------------------------
_f_dim=""; _f_green=""; _f_yellow=""; _f_red=""; _f_reset=""
if [ -t 1 ] && [ -t 2 ] \
    && [ -z "${NO_COLOR:-}" ] && [ -z "${FORGE_NO_UI:-}" ] \
    && [ -z "${CI:-}" ] && [ -z "${GITHUB_ACTIONS:-}" ] \
    && [ "${TERM:-dumb}" != "dumb" ]; then
  _f_dim=$'\033[2m'; _f_green=$'\033[32m'; _f_yellow=$'\033[33m'
  _f_red=$'\033[31m'; _f_reset=$'\033[0m'
fi
# Marks: Unicode on UTF-8 locales, ASCII otherwise (mojibake-safe).
_m_ok="OK"; _m_next="->"; _m_plan="o"; _m_fail="x"; _m_dash="-"
case "${LC_ALL:-${LC_CTYPE:-${LANG:-}}}" in
  *[Uu][Tt][Ff]*|*[Uu][Tt][Ff]8*) _m_ok="✓"; _m_next="→"; _m_plan="◌"; _m_fail="✗"; _m_dash="—" ;;
esac

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
  printf '  %s%s%s install: FORGE-PYTHON-NOT-FOUND %s no python on PATH (need %s)\n' \
      "$_f_red" "$_m_fail" "$_f_reset" "$_m_dash" ">=3.11" >&2
  printf '    %sremedy:%s install Python and reopen the shell, or run %s --python <exe>%s\n' \
      "$_f_yellow" "$_f_reset" "${BASH_SOURCE[0]}" "$_f_dim" >&2
  printf '%s\n' "$_f_reset" >&2
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
  # Human guidance goes to stderr: stdout stays pure JSON so
  # `install.sh --json | jq` is clean end-to-end.
  if [ "$_forge_dry_run" -eq 1 ]; then
    printf '  %s%s%s plan only %s no files, variables or registry entries were written.\n' \
      "$_f_yellow" "$_m_plan" "$_f_reset" "$_m_dash" >&2
  elif [ "$_forge_is_git_bash" -eq 1 ]; then
    printf '  %s%s%s user variables persisted in HKCU\\Environment and %s\n' \
      "$_f_green" "$_m_next" "$_f_reset" "$env_file" >&2
    printf '  %s%s%s reload this Git Bash: %ssource %s%s\n' \
      "$_f_green" "$_m_next" "$_f_reset" "$_f_dim" "$env_file" "$_f_reset" >&2
  else
    printf '  %s%s%s open a new shell, or reload now: %ssource %s%s\n' \
      "$_f_green" "$_m_next" "$_f_reset" "$_f_dim" "$env_file" "$_f_reset" >&2
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
