"""Smart vault layout helpers shared by every Forge obsidian surface.

One ecosystem, one layout: every Forge writes under ``forges/<forge-id>/``,
frontmatter carries the federation fields (``forge``, ``kind``,
``classification``, ``tags`` with ``#forge/<id>``), wikilinks cross-reference
sibling notes, and a ``forges/index.md`` map-of-content can be rebuilt from
the receipts ledger.  Bodies stay bounded Markdown — the vault file remains
the human-facing artifact while receipts carry the machine evidence.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Iterable, Mapping
from datetime import datetime, timezone
from typing import Any

import yaml

_SENSITIVE = ("secret", "token", "password", "credential", "access_key", "session_key")
_SENSITIVE_TEXT = re.compile(
    r"(?i)(\b(?:secret|token|password|credential|access[_-]?key|session[_-]?key)\s*[:=]\s*)([^\s,;]+)"
)


def redact_value(value: Any, key: str = "") -> Any:
    """Credential-safe copy: sensitive keys/text are replaced, never logged."""
    if any(marker in key.lower() for marker in _SENSITIVE):
        return "[REDACTED]"
    if isinstance(value, dict):
        return {str(k): redact_value(v, str(k)) for k, v in value.items()}
    if isinstance(value, list):
        return [redact_value(item, key) for item in value]
    if isinstance(value, str):
        return _SENSITIVE_TEXT.sub(r"\1[REDACTED]", value)
    return value


def section_yaml(title: str, values: Any) -> str:
    """``## Title`` + YAML block (or '- None reported') — redaction applied."""
    safe = redact_value(values)
    if safe in (None, "", [], {}):
        return f"## {title}\n- None reported\n"
    return (
        f"## {title}\n```yaml\n"
        + yaml.safe_dump(safe, sort_keys=True, allow_unicode=True).rstrip()
        + "\n```\n"
    )


def build_note_ref(
    *,
    forge_id: str,
    title: str,
    summary: str,
    workspace: str,
    vault_id: str,
    kind: str,
    body_sections: Iterable[str] = (),
    note_path: str | None = None,
    classification: str = "internal",
    confidence: str = "unknown",
    tags: Iterable[str] = (),
    related: Iterable[str] = (),
    extra_frontmatter: Mapping[str, Any] | None = None,
    provenance: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Compose a ``theforge/ObsidianNoteRef/v1`` note + its vault ``content``.

    The returned mapping is contract-shaped (minus ``content`` it validates
    against the NoteRef schema) so a forge can hand it to the federation
    layer, write it to the vault itself via :func:`export_vault_document`,
    or both.  Everything user-supplied passes :func:`redact_value` first.
    """
    safe_title = str(redact_value(title.strip() or f"{forge_id} note"))
    safe_summary = str(redact_value(summary.strip()))
    body = "\n".join([safe_summary, "", *body_sections]).rstrip() + "\n"
    content = render_note(
        title=safe_title,
        body=body,
        forge_id=forge_id,
        kind=kind,
        classification=classification,
        confidence=confidence,
        source=forge_id,
        tags=tags,
        related=related,
        extra_frontmatter=extra_frontmatter,
    )
    path = note_path or forge_note_path(forge_id, slugify(safe_title))
    return {
        "schema": "theforge/ObsidianNoteRef/v1",
        "version": "v1",
        "workspace": workspace,
        "vault_id": vault_id,
        "path": path,
        "source": forge_id,
        "classification": classification,
        "content_hash": hashlib.sha256(content.encode("utf-8")).hexdigest(),
        "updated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "outcome": "projected",
        "provenance": {"adapter": forge_id, **redact_value(dict(provenance or {}))},
        "content": content,
        "warnings": [],
        "unresolved": [],
    }


MAX_TITLE = 160
_SLUG_RE = re.compile(r"[^a-z0-9/_-]+")
_FRONTMATTER_KEYS = (
    "forge",
    "kind",
    "classification",
    "confidence",
    "source",
    "tags",
    "related",
)


def slugify(value: str, *, default: str = "note") -> str:
    """Lowercase vault-safe slug; path segments are preserved."""
    slug = _SLUG_RE.sub("-", value.strip().lower()).strip("-")
    slug = re.sub(r"-{2,}", "-", slug)
    slug = re.sub(r"(?:^|/)-+|-+(?=/|$)", lambda m: m.group(0).strip("-"), slug)
    parts = [p for p in slug.split("/") if p and p not in {".", ".."}]
    return "/".join(parts) or default


def forge_note_path(forge_id: str, name: str) -> str:
    """Canonical note location ``forges/<forge-id>/<slug>.md``."""
    forge = slugify(forge_id)
    leaf = slugify(name)
    if leaf.endswith(".md"):
        leaf = leaf[:-3]
    return f"forges/{forge}/{leaf}.md"


def render_frontmatter(fields: Mapping[str, Any]) -> str:
    """Deterministic YAML frontmatter block (with the --- fences)."""
    ordered = {k: fields[k] for k in _FRONTMATTER_KEYS if k in fields}
    for key in sorted(fields):
        if key not in ordered:
            ordered[key] = fields[key]
    return (
        "---\n" + yaml.safe_dump(ordered, sort_keys=False, allow_unicode=True).rstrip() + "\n---\n"
    )


def render_note(
    *,
    title: str,
    body: str,
    forge_id: str,
    kind: str,
    classification: str = "internal",
    confidence: str = "unknown",
    source: str | None = None,
    tags: Iterable[str] = (),
    related: Iterable[str] = (),
    extra_frontmatter: Mapping[str, Any] | None = None,
) -> str:
    """Compose a complete vault note: frontmatter + ``# title`` + body.

    ``related`` entries become ``[[wikilink]]`` targets appended to a
    ``## Related`` section so the vault graph stays navigable by hand.
    """
    fields: dict[str, Any] = {
        "forge": forge_id,
        "kind": kind,
        "classification": classification,
        "confidence": confidence,
        "source": source or forge_id,
        "tags": sorted({f"forge/{forge_id}", *tags}),
    }
    rel = [slugify(r[:-3] if r.strip().lower().endswith(".md") else r) for r in related]
    rel = [r for r in dict.fromkeys(rel) if r]
    if rel:
        fields["related"] = rel
    if extra_frontmatter:
        for key, value in extra_frontmatter.items():
            if key not in fields:
                fields[key] = value
    lines = [render_frontmatter(fields), f"# {title.strip()[:MAX_TITLE]}", "", body.rstrip(), ""]
    if rel:
        lines += ["## Related", ""]
        lines += [f"- [[{target}]]" for target in rel]
        lines.append("")
    return "\n".join(lines)


def moc_note(entries: Iterable[Mapping[str, str]]) -> str:
    """Rebuild ``forges/index.md`` — a map of content for the whole ecosystem.

    Entries are receipt-shaped mappings carrying ``source`` (forge id),
    ``path`` and ``title``; anything missing a field is skipped rather than
    fabricated.
    """
    grouped: dict[str, list[Mapping[str, str]]] = {}
    for entry in entries:
        forge = str(entry.get("source") or "").strip()
        path = str(entry.get("path") or "").strip()
        if not forge or not path or not path.endswith(".md"):
            continue
        grouped.setdefault(forge, []).append(entry)
    lines = [
        render_frontmatter(
            {
                "forge": "forge-ecosystem",
                "kind": "moc",
                "classification": "internal",
                "source": "forge-obsidian",
                "tags": ["forge/ecosystem", "moc"],
            }
        ),
        "# Forge Ecosystem",
        "",
        "Mapa de conteúdo das notas gravadas pelas Forjas neste vault.",
        "",
    ]
    for forge in sorted(grouped):
        lines += [f"## {forge}", ""]
        for entry in sorted(grouped[forge], key=lambda e: str(e.get("path", ""))):
            path = str(entry["path"])[:-3]
            title = str(entry.get("title") or path.rsplit("/", 1)[-1])
            lines.append(f"- [[{path}|{title}]]")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"
