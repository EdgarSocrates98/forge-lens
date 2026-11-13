#!/usr/bin/env bash
# Forge account install — Windows Git Bash route (MSYS2 / Git for Windows).
#
# Thin adapter over scripts/forge_bootstrap.py — the SAME shared Python
# bootstrap install.sh and install.ps1 exec. Nothing here reimplements a
# pipeline stage: env discovery → python resolution → venv → runtime →
# launcher → HKCU + Git Bash env.sh persistence → InstallReceipt → smoke
# verify all live in the bootstrap; this script only picks the
# interpreter, invokes it and surfaces FORGE-* refusal codes.
#
# Primary target: Git Bash. On a non-MSYS POSIX shell the script still
# runs (documented decision — the shared bootstrap is platform-agnostic)
# but install.sh stays the canonical POSIX route and the script says so.
# It NEVER spawns powershell.exe/pwsh — PowerShell is not a fallback on
# this route; a shell that is not bash refuses FORGE-SHELL-UNAVAILABLE.
#
# CRLF: keep LF endings. A checkout cloned with core.autocrlf=true can
# hand bash a CRLF file it cannot parse (the \r lands inside tokens) —
# repair a converted copy once with:  sed -i 's/\r$//' install.windows.sh
if [ -z "${BASH_VERSION:-}" ]; then
  echo "install.windows.sh: FORGE-SHELL-UNAVAILABLE — esta rota exige bash (Git Bash); a shell atual nao e bash" >&2
  echo "  remedy: run 'bash install.windows.sh' inside Git Bash, or use install.ps1 under PowerShell" >&2
  exit 2
fi
set -euo pipefail

# -- decoration (plain when not interactive; honors NO_COLOR/CI) -----------
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
  *)
    # Tolerated, documented (lf-0018): the shared bootstrap is
    # platform-agnostic so a stray POSIX invocation still works — but
    # install.sh is the canonical POSIX route and we say so.
    echo "install.windows.sh: esta e a rota Windows Git Bash; em POSIX o caminho canonico e ./install.sh — continuando (mesmo bootstrap compartilhado)" >&2
    echo "install.windows.sh: this is the Windows Git Bash route; on POSIX the canonical route is ./install.sh — continuing (same shared bootstrap)" >&2
    ;;
esac
_forge_dry_run=0
for arg in "$@"; do
  if [ "$arg" = "--dry-run" ]; then _forge_dry_run=1; break; fi
done

# Interpreter selection, same priority as the lf-0017 pipeline:
# --python flag > FORGE_PYTHON > PATH scan (python3|python|py -3).
_forge_python_arg=""
_forge_prev=""
for arg in "$@"; do
  if [ "$_forge_prev" = "--python" ]; then _forge_python_arg="$arg"; fi
  case "$arg" in
    --python=*) _forge_python_arg="${arg#--python=}" ;;
  esac
  _forge_prev="$arg"
done

PY=""
PY_ARGS=()
if [ -n "$_forge_python_arg" ]; then
  PY="$_forge_python_arg"
elif [ -n "${FORGE_PYTHON:-}" ]; then
  PY="$FORGE_PYTHON"
fi
if [ -n "$PY" ]; then
  # Intentional word-split: FORGE_PYTHON/--python may carry an
  # invocation ("py -3.11") — the bootstrap validates it independently
  # (FORGE_PYTHON reaches find_python via the environment itself).
  # shellcheck disable=SC2206
  _forge_py_argv=($PY)
  PY="${_forge_py_argv[0]}"
  PY_ARGS=("${_forge_py_argv[@]:1}")
else
  for candidate in python3 python py; do
    if command -v "$candidate" >/dev/null 2>&1; then
      PY="$(command -v "$candidate")"
      case "$(basename -- "$PY")" in
        py|py.exe) PY_ARGS=(-3) ;;
      esac
      break
    fi
  done
fi
if [ -z "$PY" ]; then
  printf '  %s%s%s install.windows.sh: FORGE-PYTHON-NOT-FOUND %s nenhum interpretador em PATH (precisa %s)\n' \
      "$_f_red" "$_m_fail" "$_f_reset" "$_m_dash" ">=3.11" >&2
  printf '    %sremedio:%s instale Python e reabra o Git Bash, ou aponte FORGE_PYTHON=<caminho/python.exe>, ou rode ./install.windows.sh --python <exe>\n' \
      "$_f_yellow" "$_f_reset" >&2
  printf '    %sremedy:%s install Python and reopen Git Bash, or set FORGE_PYTHON=<python.exe path>, or run ./install.windows.sh --python <exe>%s\n' \
      "$_f_yellow" "$_f_reset" "$_f_dim" >&2
  printf '%s\n' "$_f_reset" >&2
  cd "$_forge_original_dir"
  if [ "$_forge_is_sourced" -eq 1 ]; then return 2; fi
  exit 2
fi
if ! command -v "$PY" >/dev/null 2>&1 && [ ! -x "$PY" ]; then
  printf '  %s%s%s install.windows.sh: FORGE-PYTHON-UNEXECUTABLE %s o interpretador apontado nao executa: %s\n' \
      "$_f_red" "$_m_fail" "$_f_reset" "$_m_dash" "$PY" >&2
  printf '    %sremedio:%s corrija --python/FORGE_PYTHON ou remova a variavel para a varredura de PATH\n' \
      "$_f_yellow" "$_f_reset" >&2
  printf '    %sremedy:%s fix --python/FORGE_PYTHON or unset it to fall back to the PATH scan%s\n' \
      "$_f_yellow" "$_f_reset" "$_f_dim" >&2
  printf '%s\n' "$_f_reset" >&2
  cd "$_forge_original_dir"
  if [ "$_forge_is_sourced" -eq 1 ]; then return 2; fi
  exit 2
fi

ARGS=(--persist-env --allow-download "$@")
_forge_log="${TMPDIR:-${TMP:-/tmp}}/forge-install-windows.$$.log"
status=0
if command -v tee >/dev/null 2>&1; then
  # tee on STDOUT ONLY: stderr stays attached to the terminal so the
  # bootstrap's animated UI still runs, while the log captures the pure
  # JSON report for FORGE-* kind/detail extraction on failure.
  "$PY" "${PY_ARGS[@]}" scripts/forge_bootstrap.py "${ARGS[@]}" | tee "$_forge_log" || status=${PIPESTATUS[0]}
else
  "$PY" "${PY_ARGS[@]}" scripts/forge_bootstrap.py "${ARGS[@]}" || status=$?
fi

if [ "$status" -eq 0 ]; then
  rm -f "$_forge_log"
  env_file="${XDG_CONFIG_HOME:-$HOME/.config}/forjas/env.sh"
  # Human guidance on stderr — the tee'd stdout stays pure JSON.
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
else
  # Lift the FORGE-* kind/detail. The tee'd stdout log carries THIS run's
  # JSON report (stdout is piped → always emitted) — primary source. The
  # persisted receipt is the fallback for the tee-less path, where an
  # interactive TTY suppresses the stdout JSON.
  _forge_kind=""
  _forge_detail=""
  _forge_id="$(sed -n 's/.*"forge_id"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' forge.json | head -n 1)"
  _forge_receipt="${FORGE_HOME_OVERRIDE:-$HOME}/.forge/installations/.${_forge_id}-bootstrap.json"
  for _src in "$_forge_log" "$_forge_receipt"; do
    if [ -n "$_src" ] && [ -f "$_src" ]; then
      _forge_kind="$(sed -n 's/.*"kind"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' "$_src" | tail -n 1)"
      _forge_detail="$(sed -n 's/.*"detail"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' "$_src" | tail -n 1)"
      if [ -n "$_forge_kind" ]; then break; fi
    fi
  done
  rm -f "$_forge_log"
  if [ -n "$_forge_kind" ]; then
    printf '  %s%s%s install.windows.sh: %s %s o bootstrap recusou: %s\n' \
      "$_f_red" "$_m_fail" "$_f_reset" "$_forge_kind" "$_m_dash" "${_forge_detail:-ver o receipt acima}" >&2
    printf '  %s%s%s install.windows.sh: %s %s bootstrap refused: %s\n' \
      "$_f_red" "$_m_fail" "$_f_reset" "$_forge_kind" "$_m_dash" "${_forge_detail:-see the receipt above}" >&2
  else
    # Bootstrap crashed without a named refusal (traceback, no JSON).
    printf '  %s%s%s install.windows.sh: FORGE-INSTALL-BOOTSTRAP-FAILED %s scripts/forge_bootstrap.py saiu com codigo %s sem recusa nomeada\n' \
      "$_f_red" "$_m_fail" "$_f_reset" "$_m_dash" "$status" >&2
    printf '  %s%s%s install.windows.sh: FORGE-INSTALL-BOOTSTRAP-FAILED %s scripts/forge_bootstrap.py exited %s with no named refusal\n' \
      "$_f_red" "$_m_fail" "$_f_reset" "$_m_dash" "$status" >&2
  fi
  printf '    %sremedio:%s corrija a causa indicada pelo codigo FORGE-* e reexecute ./install.windows.sh; o receipt fica em ~/.forge/installations/\n' \
    "$_f_yellow" "$_f_reset" >&2
  printf '    %sremedy:%s fix the cause named by the FORGE-* code and re-run ./install.windows.sh; the receipt lives under ~/.forge/installations/%s\n' \
    "$_f_yellow" "$_f_reset" "$_f_dim" >&2
  printf '%s\n' "$_f_reset" >&2
fi
cd "$_forge_original_dir"
if [ "$_forge_is_sourced" -eq 1 ]; then
  return "$status"
fi
exit "$status"
