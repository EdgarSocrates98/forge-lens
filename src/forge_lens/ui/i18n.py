"""Minimal i18n for the UI kit — pt-BR default, en fallback.

Strings are keyed; a missing key falls back to English then to the key
itself (never a crash). ``FORGE_LANG`` or locale env decides.
"""

from __future__ import annotations

import locale
import os

_STRINGS: dict[str, dict[str, str]] = {
    "en": {
        "choose": "Choose",
        "continue": "Continue",
        "cancel": "Cancel",
        "back": "Back",
        "quit": "Quit",
        "yes": "yes",
        "no": "no",
        "confirm_q": "Continue?",
        "select_hint": "up/down or j/k + enter; q or esc to cancel",
        "toggle_hint": "space toggles, enter confirms; j/k moves",
        "install_scope_q": "Where do you want to install?",
        "install_profile_q": "Installation profile?",
        "install_gateway_profile_q": "Execution profile?",
        "install_hosts_q": "Configure AI hosts?",
        "install_components_q": "Optional components?",
        "review": "Review installation",
        "scope_project": "Current repository",
        "scope_workspace": "Entire workspace",
        "scope_user": "My user account",
        "profile_balanced": "Balanced (Recommended)",
        "profile_economy": "Economy",
        "profile_full": "Full",
        "env_detected": "Environment detected",
        "diff_old": "OLD contract/dir",
        "diff_new": "NEW contract/dir",
        "dry_run_banner": "plan only — nothing was written",
        "approved_banner": "changes applied",
        "need_decision": "required decision missing in non-interactive mode",
        "health": "Health",
        "status": "Status",
        "doc_status": "status",
        "doc_target": "target",
        "doc_scope": "scope",
        "doc_version": "version",
        "doc_profile": "profile",
        "doc_state_dir": "state dir",
        "doc_managed": "managed",
        "doc_entries": "entries",
        "doc_checks": "CHECKS",
        "doc_repairable": "(repairable)",
        "doc_drift": "DRIFT",
        "doc_modified": "modified",
        "doc_missing": "missing",
        "doc_managed_files": "MANAGED FILES",
        "doc_planned_files": "PLANNED FILES",
        "doc_workspace": "WORKSPACE",
        "doc_members": "member repo(s)",
        "doc_unknown_member": "(not a discovered repo)",
        "doc_verification": "verification",
        "doc_refused": "refused:",
        "doc_unlock": "unlock:",
        "doc_repair": "repair:",
        "doc_portal_vendored": "vendored assets — no local install",
        "apply_q": "Apply this plan?",
    },
    "pt": {
        "choose": "Escolha",
        "continue": "Continuar",
        "cancel": "Cancelar",
        "back": "Voltar",
        "quit": "Sair",
        "yes": "sim",
        "no": "não",
        "confirm_q": "Continuar?",
        "select_hint": "setas ou j/k + enter; q ou esc cancela",
        "toggle_hint": "espaço alterna, enter confirma; j/k move",
        "install_scope_q": "Onde instalar?",
        "install_profile_q": "Perfil de instalação?",
        "install_gateway_profile_q": "Perfil de execução?",
        "install_hosts_q": "Configurar hosts de IA?",
        "install_components_q": "Componentes opcionais?",
        "review": "Revisar instalação",
        "scope_project": "Repositório atual",
        "scope_workspace": "Workspace inteira",
        "scope_user": "Minha conta de usuário",
        "profile_balanced": "Balanced (Recomendado)",
        "profile_economy": "Economy",
        "profile_full": "Full",
        "env_detected": "Ambiente detectado",
        "diff_old": "Contrato/diretório ANTIGO",
        "diff_new": "Contrato/diretório NOVO",
        "dry_run_banner": "somente plano — nada foi escrito",
        "approved_banner": "alterações aplicadas",
        "need_decision": "decisão obrigatória ausente em modo não-interativo",
        "health": "Saúde",
        "status": "Status",
        "doc_status": "status",
        "doc_target": "alvo",
        "doc_scope": "escopo",
        "doc_version": "versão",
        "doc_profile": "perfil",
        "doc_state_dir": "dir de estado",
        "doc_managed": "gerenciados",
        "doc_entries": "entradas",
        "doc_checks": "CHECKS",
        "doc_repairable": "(reparável)",
        "doc_drift": "DRIFT",
        "doc_modified": "modificados",
        "doc_missing": "ausentes",
        "doc_managed_files": "ARQUIVOS GERENCIADOS",
        "doc_planned_files": "ARQUIVOS PLANEJADOS",
        "doc_workspace": "WORKSPACE",
        "doc_members": "repo(s) membro",
        "doc_unknown_member": "(não é um repo descoberto)",
        "doc_verification": "verificação",
        "doc_refused": "recusado:",
        "doc_unlock": "desbloqueio:",
        "doc_repair": "reparo:",
        "doc_portal_vendored": "assets embutidos — sem instalação local",
        "apply_q": "Aplicar este plano?",
    },
}


def lang() -> str:
    explicit = os.environ.get("FORGE_LANG", "").lower()
    if explicit.startswith("pt"):
        return "pt"
    if explicit.startswith("en"):
        return "en"
    try:
        loc = (locale.getlocale()[0] or "").lower()
    except (ValueError, AttributeError):
        loc = ""
    return "pt" if loc.startswith("pt") else "en"


def t(key: str, language: str | None = None) -> str:
    language = language or lang()
    return _STRINGS.get(language, _STRINGS["en"]).get(key, _STRINGS["en"].get(key, key))
