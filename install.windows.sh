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
  echo "install.windows.sh: FORGE-PYTHON-NOT-FOUND — nenhum interpretador em PATH (precisa >=3.11)" >&2
  echo "  remedio: instale Python e reabra o Git Bash, ou aponte FORGE_PYTHON=<caminho/python.exe>, ou rode ./install.windows.sh --python <exe>" >&2
  echo "  remedy: install Python and reopen Git Bash, or set FORGE_PYTHON=<python.exe path>, or run ./install.windows.sh --python <exe>" >&2
  cd "$_forge_original_dir"
  if [ "$_forge_is_sourced" -eq 1 ]; then return 2; fi
  exit 2
fi
if ! command -v "$PY" >/dev/null 2>&1 && [ ! -x "$PY" ]; then
  echo "install.windows.sh: FORGE-PYTHON-UNEXECUTABLE — o interpretador apontado nao executa: $PY" >&2
  echo "  remedio: corrija --python/FORGE_PYTHON ou remova a variavel para a varredura de PATH" >&2
  echo "  remedy: fix --python/FORGE_PYTHON or unset it to fall back to the PATH scan" >&2
  cd "$_forge_original_dir"
  if [ "$_forge_is_sourced" -eq 1 ]; then return 2; fi
  exit 2
fi

ARGS=(--persist-env --allow-download "$@")
_forge_log="${TMPDIR:-${TMP:-/tmp}}/forge-install-windows.$$.log"
status=0
if command -v tee >/dev/null 2>&1; then
  # tee keeps live output AND lets us lift the FORGE-* kind from the
  # bootstrap's JSON report on failure.
  "$PY" "${PY_ARGS[@]}" scripts/forge_bootstrap.py "${ARGS[@]}" 2>&1 | tee "$_forge_log" || status=${PIPESTATUS[0]}
else
  "$PY" "${PY_ARGS[@]}" scripts/forge_bootstrap.py "${ARGS[@]}" 2>&1 || status=$?
fi

if [ "$status" -eq 0 ]; then
  rm -f "$_forge_log"
  env_file="${XDG_CONFIG_HOME:-$HOME/.config}/forjas/env.sh"
  if [ "$_forge_dry_run" -eq 1 ]; then
    echo "Install plan complete. No files, variables or registry entries were written."
  elif [ "$_forge_is_git_bash" -eq 1 ]; then
    echo "Install complete. Windows user variables saved in HKCU\Environment and Git Bash environment saved in: $env_file"
    echo "Reload current Git Bash with: source '$env_file'"
  else
    echo "Install complete. Open a new shell or run: source '$env_file'"
  fi
  if [ "$_forge_is_sourced" -eq 1 ] && [ -f "$env_file" ]; then
    # A sourced installer can update its caller; an executed child cannot.
    # shellcheck disable=SC1090
    . "$env_file"
  fi
else
  _forge_kind=""
  _forge_detail=""
  if [ -f "$_forge_log" ]; then
    _forge_kind="$(sed -n 's/.*"kind"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' "$_forge_log" | tail -n 1)"
    _forge_detail="$(sed -n 's/.*"detail"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' "$_forge_log" | tail -n 1)"
  fi
  rm -f "$_forge_log"
  if [ -n "$_forge_kind" ]; then
    echo "install.windows.sh: ${_forge_kind} — o bootstrap recusou: ${_forge_detail:-ver o relatorio JSON acima}" >&2
    echo "install.windows.sh: ${_forge_kind} — bootstrap refused: ${_forge_detail:-see the JSON report above}" >&2
  else
    # Bootstrap crashed without a named refusal (traceback, no JSON).
    echo "install.windows.sh: FORGE-INSTALL-BOOTSTRAP-FAILED — scripts/forge_bootstrap.py saiu com codigo $status sem recusa nomeada" >&2
    echo "install.windows.sh: FORGE-INSTALL-BOOTSTRAP-FAILED — scripts/forge_bootstrap.py exited $status with no named refusal" >&2
  fi
  echo "  remedio: corrija a causa indicada pelo codigo FORGE-* e reexecute ./install.windows.sh; o receipt fica em ~/.forge/installations/" >&2
  echo "  remedy: fix the cause named by the FORGE-* code and re-run ./install.windows.sh; the receipt lives under ~/.forge/installations/" >&2
fi
cd "$_forge_original_dir"
if [ "$_forge_is_sourced" -eq 1 ]; then
  return "$status"
fi
exit "$status"
