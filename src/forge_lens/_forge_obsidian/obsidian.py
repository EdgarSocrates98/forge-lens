"""Optional Obsidian Markdown vault adapter.

Obsidian is a file format here, not a runtime dependency.  The adapter reads
and writes bounded Markdown under an explicitly configured vault root, refuses
symlinks/reparse points and traversal, and never shells out to the Obsidian
application or CLI.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import tempfile
import time
from collections.abc import Iterator, Mapping
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

import yaml

MAX_VAULT_BYTES = 1 * 1024 * 1024
MAX_INTEROP_BYTES = 1 * 1024 * 1024
_CANVAS_NODE_TYPES = {"text", "file", "link", "group"}
_CANVAS_SIDES = {"top", "right", "bottom", "left"}
_CANVAS_ENDS = {"none", "arrow"}
_CLASSIFICATIONS = {"public", "internal", "confidential", "restricted"}
_SENSITIVE_TEXT = re.compile(
    r"(?i)\b(?:secret|token|password|credential|access[_-]?key|session[_-]?key)\s*[:=]"
)
_PROJECTION_FIELDS = (
    "aliases",
    "wikilinks",
    "embeds",
    "tags",
    "callouts",
    "code_blocks",
    "block_references",
    "relative_links",
    "attachments",
    "security_warnings",
)


class ObsidianVaultError(ValueError):
    _DENIED_CODES = {
        "OBSIDIAN-VAULT-OUTSIDE-AUTHORIZED-ROOT",
        "OBSIDIAN-HIDDEN-REFUSED",
        "OBSIDIAN-PATH-REFUSED",
        "OBSIDIAN-PATH-REQUIRED",
        "OBSIDIAN-SYMLINK-REFUSED",
        "OBSIDIAN-HARDLINK-REFUSED",
        "OBSIDIAN-BINARY-REFUSED",
        "OBSIDIAN-CLASSIFICATION-INVALID",
        "OBSIDIAN-SIZE-LIMIT",
        "OBSIDIAN-BATCH-LIMIT",
        "OBSIDIAN-PARENT-MISSING",
    }

    def __init__(self, code: str, message: str, *, sync_state: str | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.sync_state = sync_state or (
            "conflict"
            if code in {"OBSIDIAN-CONFLICT", "OBSIDIAN-ROLLBACK-CONFLICT"}
            else "denied"
            if code in self._DENIED_CODES
            else "failed"
        )


@runtime_checkable
class VaultLocator(Protocol):
    def locate(self) -> Path: ...


@runtime_checkable
class VaultReader(Protocol):
    def read(self, path: str) -> dict[str, Any]: ...


@runtime_checkable
class VaultWriter(Protocol):
    def write(
        self, path: str, content: str, *, baseline_hash: str | None = None
    ) -> dict[str, Any]: ...


@runtime_checkable
class VaultScanner(Protocol):
    def scan(self, prefix: str = "") -> list[str]: ...


class MarkdownDocumentParser(Protocol):
    def parse(self, content: str) -> dict[str, Any]: ...


class FrontmatterAdapter(Protocol):
    def parse(self, content: str) -> tuple[dict[str, Any], str]: ...


class WikilinkResolver(Protocol):
    def resolve(self, link: str) -> str | None: ...


class AttachmentResolver(Protocol):
    def resolve(self, link: str) -> str | None: ...


class KnowledgeProjector(Protocol):
    def project(self, document: Mapping[str, Any]) -> Mapping[str, Any]: ...


class SyncPlanner(Protocol):
    def plan(
        self, path: str, content: str, *, baseline_hash: str | None = None
    ) -> dict[str, Any]: ...


class ConflictDetector(Protocol):
    def detect(
        self, baseline_hash: str | None, current_hash: str | None, candidate_hash: str
    ) -> str: ...


class ImportReceiptWriter(Protocol):
    def write_import_receipt(self, receipt: Mapping[str, Any]) -> None: ...


class ExportReceiptWriter(Protocol):
    def write_export_receipt(self, receipt: Mapping[str, Any]) -> None: ...


def _classification(value: str) -> str:
    if value not in _CLASSIFICATIONS:
        raise ObsidianVaultError(
            "OBSIDIAN-CLASSIFICATION-INVALID", "unsupported document classification"
        )
    return value


def _bounded_document(document: str | bytes | Mapping[str, Any], *, kind: str) -> Any:
    """Decode one bounded Obsidian interchange document without executing content."""
    if isinstance(document, bytes):
        if len(document) > MAX_INTEROP_BYTES:
            raise ObsidianVaultError(
                "OBSIDIAN-SIZE-LIMIT", f"{kind} document exceeds the safe size limit"
            )
        try:
            text = document.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ObsidianVaultError(
                "OBSIDIAN-UTF8-INVALID", f"{kind} document is not valid UTF-8"
            ) from exc
    elif isinstance(document, str):
        if len(document.encode("utf-8")) > MAX_INTEROP_BYTES:
            raise ObsidianVaultError(
                "OBSIDIAN-SIZE-LIMIT", f"{kind} document exceeds the safe size limit"
            )
        text = document
    elif isinstance(document, Mapping):
        return dict(document)
    else:
        raise ObsidianVaultError(
            f"OBSIDIAN-{kind.upper()}-INVALID", f"{kind} document must be text or an object"
        )

    try:
        value = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ObsidianVaultError(
            f"OBSIDIAN-{kind.upper()}-INVALID", f"{kind} document is malformed"
        ) from exc
    if not isinstance(value, Mapping):
        raise ObsidianVaultError(
            f"OBSIDIAN-{kind.upper()}-INVALID", f"{kind} document must contain an object"
        )
    return dict(value)


def validate_canvas_document(document: str | bytes | Mapping[str, Any]) -> dict[str, Any]:
    """Validate the safe, structural subset of JSON Canvas used for interoperability.

    This deliberately validates rather than renders or executes a canvas.  Unknown
    optional fields are preserved by the caller and do not make a document invalid.
    """
    value = _bounded_document(document, kind="canvas")
    nodes = value.get("nodes")
    edges = value.get("edges")
    if not isinstance(nodes, list) or not isinstance(edges, list):
        raise ObsidianVaultError(
            "OBSIDIAN-CANVAS-INVALID", "canvas document requires nodes and edges arrays"
        )

    node_ids: set[str] = set()
    for node in nodes:
        if not isinstance(node, Mapping):
            raise ObsidianVaultError(
                "OBSIDIAN-CANVAS-INVALID", "every canvas node must be an object"
            )
        node_id = node.get("id")
        node_type = node.get("type")
        if not isinstance(node_id, str) or not node_id.strip() or node_id in node_ids:
            raise ObsidianVaultError(
                "OBSIDIAN-CANVAS-INVALID", "canvas node ids must be unique and non-empty"
            )
        if node_type not in _CANVAS_NODE_TYPES:
            raise ObsidianVaultError("OBSIDIAN-CANVAS-INVALID", "canvas node type is unsupported")
        for field in ("x", "y", "width", "height"):
            number = node.get(field)
            if isinstance(number, bool) or not isinstance(number, (int, float)):
                raise ObsidianVaultError(
                    "OBSIDIAN-CANVAS-INVALID",
                    f"canvas node field '{field}' must be numeric",
                )
        if node["width"] <= 0 or node["height"] <= 0:
            raise ObsidianVaultError(
                "OBSIDIAN-CANVAS-INVALID", "canvas node width and height must be positive"
            )
        required_content = {
            "text": "text",
            "file": "file",
            "link": "url",
        }.get(str(node_type))
        if required_content and not isinstance(node.get(required_content), str):
            raise ObsidianVaultError(
                "OBSIDIAN-CANVAS-INVALID",
                f"canvas {node_type} node requires string field '{required_content}'",
            )
        if node_type == "group" and "label" in node and not isinstance(node["label"], str):
            raise ObsidianVaultError(
                "OBSIDIAN-CANVAS-INVALID", "canvas group label must be a string"
            )
        node_ids.add(node_id)

    edge_ids: set[str] = set()
    for edge in edges:
        if not isinstance(edge, Mapping):
            raise ObsidianVaultError(
                "OBSIDIAN-CANVAS-INVALID", "every canvas edge must be an object"
            )
        edge_id = edge.get("id")
        from_node = edge.get("fromNode")
        to_node = edge.get("toNode")
        if not isinstance(edge_id, str) or not edge_id.strip() or edge_id in edge_ids:
            raise ObsidianVaultError(
                "OBSIDIAN-CANVAS-INVALID", "canvas edge ids must be unique and non-empty"
            )
        if from_node not in node_ids or to_node not in node_ids:
            raise ObsidianVaultError(
                "OBSIDIAN-CANVAS-INVALID", "canvas edge endpoint does not exist"
            )
        for field in ("fromSide", "toSide"):
            if field in edge and edge[field] not in _CANVAS_SIDES:
                raise ObsidianVaultError(
                    "OBSIDIAN-CANVAS-INVALID", f"canvas edge field '{field}' is invalid"
                )
        for field in ("fromEnd", "toEnd"):
            if field in edge and edge[field] not in _CANVAS_ENDS:
                raise ObsidianVaultError(
                    "OBSIDIAN-CANVAS-INVALID", f"canvas edge field '{field}' is invalid"
                )
        edge_ids.add(edge_id)

    return {
        "schema": "productforge/ObsidianCanvasValidation/v1",
        "kind": "canvas",
        "state": "validated",
        "nodes": len(nodes),
        "edges": len(edges),
        "warnings": [],
    }


def validate_base_document(document: str | bytes | Mapping[str, Any]) -> dict[str, Any]:
    """Validate the safe, official YAML shape used by Obsidian Bases.

    This validates the file contract documented by Obsidian without evaluating
    filters or formulas.  View-specific plugin keys are deliberately preserved
    and accepted, while top-level keys and the core view/filter shapes are
    constrained so generated ``.base`` files remain portable.
    """
    value = _bounded_document(document, kind="base")
    allowed_top_level = {"filters", "formulas", "properties", "summaries", "views"}
    unknown = sorted(set(value) - allowed_top_level)
    if unknown:
        raise ObsidianVaultError(
            "OBSIDIAN-BASE-INVALID",
            f"base contains unsupported top-level keys: {', '.join(str(item) for item in unknown)}",
        )

    def validate_filter(item: Any, *, location: str = "filters") -> None:
        if isinstance(item, str):
            if not item.strip():
                raise ObsidianVaultError(
                    "OBSIDIAN-BASE-INVALID", f"base {location} cannot be empty"
                )
            return
        if not isinstance(item, Mapping) or len(item) != 1:
            raise ObsidianVaultError(
                "OBSIDIAN-BASE-INVALID",
                f"base {location} must be a string or one logical filter object",
            )
        operator, clauses = next(iter(item.items()))
        if operator not in {"and", "or", "not"} or not isinstance(clauses, list) or not clauses:
            raise ObsidianVaultError(
                "OBSIDIAN-BASE-INVALID",
                f"base {location} must use non-empty and/or/not lists",
            )
        for index, clause in enumerate(clauses):
            validate_filter(clause, location=f"{location}.{operator}[{index}]")

    if "filters" in value:
        validate_filter(value["filters"])
    for section in ("formulas", "summaries"):
        if section in value:
            if not isinstance(value[section], Mapping):
                raise ObsidianVaultError(
                    "OBSIDIAN-BASE-INVALID", f"base field '{section}' has an invalid shape"
                )
            if any(
                not isinstance(key, str) or not isinstance(item, str)
                for key, item in value[section].items()
            ):
                raise ObsidianVaultError(
                    "OBSIDIAN-BASE-INVALID", f"base field '{section}' must map names to strings"
                )
    if "properties" in value:
        if not isinstance(value["properties"], Mapping) or any(
            not isinstance(key, str) or not isinstance(item, Mapping)
            for key, item in value["properties"].items()
        ):
            raise ObsidianVaultError(
                "OBSIDIAN-BASE-INVALID", "base field 'properties' must map names to objects"
            )
    if "views" in value:
        if not isinstance(value["views"], list):
            raise ObsidianVaultError(
                "OBSIDIAN-BASE-INVALID", "base field 'views' has an invalid shape"
            )
        for index, view in enumerate(value["views"]):
            if not isinstance(view, Mapping):
                raise ObsidianVaultError(
                    "OBSIDIAN-BASE-INVALID", "every base view must be an object"
                )
            if not isinstance(view.get("type"), str) or not view["type"].strip():
                raise ObsidianVaultError(
                    "OBSIDIAN-BASE-INVALID", f"base view {index} requires a non-empty string type"
                )
            if not isinstance(view.get("name"), str) or not view["name"].strip():
                raise ObsidianVaultError(
                    "OBSIDIAN-BASE-INVALID", f"base view {index} requires a non-empty string name"
                )
            if "filters" in view:
                validate_filter(view["filters"], location=f"views[{index}].filters")
            if "limit" in view and (
                isinstance(view["limit"], bool)
                or not isinstance(view["limit"], int)
                or view["limit"] < 0
            ):
                raise ObsidianVaultError(
                    "OBSIDIAN-BASE-INVALID",
                    f"base view {index} limit must be a non-negative integer",
                )
            if "order" in view and (
                not isinstance(view["order"], list)
                or any(not isinstance(item, str) or not item.strip() for item in view["order"])
            ):
                raise ObsidianVaultError(
                    "OBSIDIAN-BASE-INVALID", f"base view {index} order must be a list of strings"
                )
            if "groupBy" in view:
                group_by = view["groupBy"]
                if not isinstance(group_by, Mapping) or not isinstance(
                    group_by.get("property"), str
                ):
                    raise ObsidianVaultError(
                        "OBSIDIAN-BASE-INVALID", f"base view {index} groupBy must name a property"
                    )
                if group_by.get("direction") not in {"ASC", "DESC"}:
                    raise ObsidianVaultError(
                        "OBSIDIAN-BASE-INVALID",
                        f"base view {index} groupBy direction must be ASC or DESC",
                    )
            if "groupOrder" in view and not isinstance(view["groupOrder"], list):
                raise ObsidianVaultError(
                    "OBSIDIAN-BASE-INVALID", f"base view {index} groupOrder must be a list"
                )

    warnings = [
        "filters and formulas are validated as official YAML shapes but are not evaluated by Product Forge; "
        "Obsidian remains the execution engine"
    ]
    return {
        "schema": "productforge/ObsidianBaseValidation/v1",
        "kind": "base",
        "state": "validated",
        "sections": sorted(value),
        "views": len(value.get("views", [])) if isinstance(value.get("views"), list) else 0,
        "warnings": warnings,
    }


def serialize_base_document(document: Mapping[str, Any]) -> str:
    """Serialize a validated Bases document as deterministic official YAML.

    Serialization is intentionally passive: it never evaluates filters or
    formulas.  The returned text is ready to save as an Obsidian ``.base``
    file after the caller applies its own authorized vault write policy.
    """
    value = _bounded_document(document, kind="base")
    validate_base_document(value)
    try:
        return yaml.safe_dump(
            value,
            sort_keys=False,
            allow_unicode=True,
            default_flow_style=False,
        )
    except yaml.YAMLError as exc:
        raise ObsidianVaultError(
            "OBSIDIAN-BASE-INVALID", "base document could not be serialized"
        ) from exc


def _resolved(path: Path) -> Path:
    return path.expanduser().resolve(strict=False)


def _inside(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _reparse(path: Path) -> bool:
    try:
        mode = path.lstat().st_mode
        if stat.S_ISLNK(mode):
            return True
        attrs = getattr(path.stat(), "st_file_attributes", 0)
        return bool(attrs & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))
    except OSError:
        return True


def _reparse_stat(info: os.stat_result) -> bool:
    if stat.S_ISLNK(info.st_mode):
        return True
    return bool(
        getattr(info, "st_file_attributes", 0)
        & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    )


def _safe(
    root: Path,
    raw_path: str,
    *,
    for_write: bool = False,
    checked_directories: set[Path] | None = None,
) -> Path:
    if not raw_path or "\x00" in raw_path:
        raise ObsidianVaultError("OBSIDIAN-PATH-REQUIRED", "vault path is required")
    normalized = raw_path.replace("\\", "/")
    if normalized.startswith(("/", "//")) or ":" in normalized.split("/", 1)[0]:
        raise ObsidianVaultError("OBSIDIAN-PATH-REFUSED", "path must be Markdown inside the vault")
    parts = tuple(part for part in normalized.split("/") if part not in {"", "."})
    if not parts or ".." in parts:
        raise ObsidianVaultError("OBSIDIAN-PATH-REFUSED", "path traversal is not allowed")
    candidate = root.joinpath(*parts)
    relative_parts = candidate.relative_to(root).parts
    blocked_dirs = {".git", ".obsidian", ".forge", ".cache", "__pycache__"}
    if any(part.lower() in blocked_dirs for part in relative_parts[:-1]):
        raise ObsidianVaultError(
            "OBSIDIAN-HIDDEN-REFUSED",
            "vault internal or cache directories are not import/export targets",
        )
    if candidate.suffix.lower() != ".md":
        raise ObsidianVaultError("OBSIDIAN-PATH-REFUSED", "path must be Markdown inside the vault")
    current = root
    for part in candidate.relative_to(current).parts[:-1]:
        current = current / part
        if checked_directories is not None and current in checked_directories:
            continue
        try:
            directory_info = current.lstat()
        except FileNotFoundError as exc:
            if for_write:
                continue  # missing parents are created by the atomic writer
            raise ObsidianVaultError("OBSIDIAN-NOT-FOUND", "vault document was not found") from exc
        except OSError as exc:
            raise ObsidianVaultError(
                "OBSIDIAN-SYMLINK-REFUSED", "vault path contains an unreadable directory"
            ) from exc
        if _reparse_stat(directory_info):
            raise ObsidianVaultError(
                "OBSIDIAN-SYMLINK-REFUSED", "vault path contains a symlink or reparse point"
            )
        if checked_directories is not None:
            checked_directories.add(current)
    try:
        file_info = candidate.lstat()
    except FileNotFoundError:
        file_info = None
    except OSError as exc:
        raise ObsidianVaultError(
            "OBSIDIAN-READ-FAILED", "vault document could not be inspected"
        ) from exc
    if file_info is not None:
        if _reparse_stat(file_info):
            raise ObsidianVaultError(
                "OBSIDIAN-SYMLINK-REFUSED", "vault path is a symlink or reparse point"
            )
        if file_info.st_nlink > 1:
            raise ObsidianVaultError("OBSIDIAN-HARDLINK-REFUSED", "vault document is a hardlink")
        if not stat.S_ISREG(file_info.st_mode):
            raise ObsidianVaultError("OBSIDIAN-NOT-FOUND", "vault document was not a regular file")
    elif not for_write:
        raise ObsidianVaultError("OBSIDIAN-NOT-FOUND", "vault document was not found")
    if for_write and not candidate.parent.is_dir():
        raise ObsidianVaultError(
            "OBSIDIAN-PARENT-MISSING", "vault destination directory was not found"
        )
    return candidate


def _title(path: Path, content: str) -> str:
    for line in content.splitlines():
        value = line.strip()
        if value.startswith("# ") and value[2:].strip():
            return value[2:].strip()[:160]
    return path.stem.replace("-", " ").replace("_", " ").strip().title()


def parse_markdown_document(content: str) -> dict[str, Any]:
    """Extract Obsidian-native metadata without rewriting the source text.

    The raw Markdown remains the authority.  This bounded parser only projects
    stable references useful to graph and knowledge adapters; unknown syntax is
    deliberately retained in ``content`` and marked as preserved.
    """
    if not isinstance(content, str):
        raise ObsidianVaultError("OBSIDIAN-MARKDOWN-TYPE", "Markdown content must be text")
    frontmatter: dict[str, Any] = {}
    frontmatter_raw = ""
    body = content
    lines = content.splitlines()
    if lines and lines[0].strip() == "---":
        end = next((i for i in range(1, min(len(lines), 201)) if lines[i].strip() == "---"), None)
        if end is not None:
            frontmatter_raw = "\n".join(lines[1:end])
            body = "\n".join(lines[end + 1 :])
            for line in lines[1:end]:
                key, separator, value = line.partition(":")
                if not separator or not key.strip():
                    continue
                normalized = value.strip()
                if normalized.lower() in {"true", "false"}:
                    parsed: Any = normalized.lower() == "true"
                else:
                    parsed = normalized.strip("'\"")
                frontmatter[key.strip()] = parsed
            try:
                parsed_frontmatter = yaml.safe_load(frontmatter_raw)
            except yaml.YAMLError:
                parsed_frontmatter = None
            if isinstance(parsed_frontmatter, Mapping):
                frontmatter = dict(parsed_frontmatter)
    wikilinks = sorted(
        {
            match.group(1).strip()
            for match in re.finditer(r"(?<!!)\[\[([^\]|#]+)(?:#[^\]|]+)?(?:\|[^\]]+)?\]\]", body)
            if match.group(1).strip()
        }
    )
    embeds = sorted(
        {
            match.group(1).strip()
            for match in re.finditer(r"!\[\[([^\]|]+)(?:\|[^\]]+)?\]\]", body)
            if match.group(1).strip()
        }
    )
    tags = sorted(set(re.findall(r"(?<![\w#])#([A-Za-z][\w/-]*)", body)))
    callouts = sorted(set(re.findall(r"^>\s*\[!([^\]]+)\]", body, flags=re.MULTILINE)))
    code_blocks = [
        language.strip()
        for language in re.findall(r"^```([^\r\n]*)", body, flags=re.MULTILINE)
        if language.strip()
    ]

    def _string_list(value: Any) -> list[str]:
        values = value if isinstance(value, list) else [value]
        return sorted({str(item).strip() for item in values if str(item).strip()})

    aliases = _string_list(frontmatter.get("aliases", frontmatter.get("alias", [])))
    block_references = sorted(
        set(re.findall(r"(?<![\w])\^([A-Za-z0-9][A-Za-z0-9_-]{0,79})\b", body))
    )
    relative_links = sorted(
        {
            match.group(1).strip()
            for match in re.finditer(r"(?<!!)\[[^\]]*\]\(([^)\s]+)(?:\s+['\"][^)]*['\"])?\)", body)
            if match.group(1).strip()
            and not match.group(1).startswith(("#", "/", "//"))
            and not re.match(r"^[A-Za-z][A-Za-z0-9+.-]*:", match.group(1))
        }
    )
    attachment_extensions = {
        ".avif",
        ".bmp",
        ".gif",
        ".jpeg",
        ".jpg",
        ".m4a",
        ".mp3",
        ".mp4",
        ".ogg",
        ".pdf",
        ".png",
        ".svg",
        ".wav",
        ".webm",
        ".webp",
    }
    attachments = sorted(
        {
            reference.split("#", 1)[0].split("?", 1)[0]
            for reference in [*embeds, *relative_links]
            if Path(reference.split("#", 1)[0].split("?", 1)[0]).suffix.lower()
            in attachment_extensions
        }
    )
    security_warnings = (
        ["possible-secret-like-text-detected"]
        if _SENSITIVE_TEXT.search(frontmatter_raw) or _SENSITIVE_TEXT.search(body)
        else []
    )
    return {
        "schema": "productforge/ObsidianMarkdown/v1",
        "frontmatter": frontmatter,
        "frontmatter_raw": frontmatter_raw,
        "wikilinks": wikilinks,
        "embeds": embeds,
        "tags": tags,
        "callouts": callouts,
        "code_blocks": code_blocks,
        "aliases": aliases,
        "block_references": block_references,
        "relative_links": relative_links,
        "attachments": attachments,
        "security_warnings": security_warnings,
        "content": content,
        "unknown_syntax_preserved": True,
    }


def obsidian_projection_metadata(markdown: Mapping[str, Any]) -> dict[str, Any]:
    """Return body-free structural metadata safe to persist with a document."""
    return {field: list(markdown.get(field) or []) for field in _PROJECTION_FIELDS}


def validate_vault_authorization(vault: Path | str) -> Path:
    """Apply an optional operator-managed allowed root to a configured vault."""
    root = _resolved(Path(vault))
    allowed_raw = os.environ.get("FORGE_OBSIDIAN_ALLOWED_ROOT", "").strip()
    if allowed_raw and not _inside(root, _resolved(Path(allowed_raw))):
        raise ObsidianVaultError(
            "OBSIDIAN-VAULT-OUTSIDE-AUTHORIZED-ROOT",
            "configured vault is outside the authorized Obsidian root",
        )
    return root


def import_vault_document(vault: Path | str, raw_path: str) -> dict[str, Any]:
    root = validate_vault_authorization(vault)
    return _import_vault_document_from_root(root, raw_path)


def _import_vault_document_from_root(
    root: Path,
    raw_path: str,
    *,
    checked_directories: set[Path] | None = None,
) -> dict[str, Any]:
    if not root.is_dir():
        raise ObsidianVaultError(
            "OBSIDIAN-VAULT-MISSING", "configured Obsidian vault is not a directory"
        )
    path = _safe(root, raw_path, checked_directories=checked_directories)
    try:
        raw_content = path.read_bytes()
    except OSError as exc:
        raise ObsidianVaultError(
            "OBSIDIAN-READ-FAILED", "vault document could not be read"
        ) from exc
    size = len(raw_content)
    if size > MAX_VAULT_BYTES:
        raise ObsidianVaultError(
            "OBSIDIAN-SIZE-LIMIT", "vault document exceeds the safe size limit"
        )
    try:
        content = raw_content.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ObsidianVaultError(
            "OBSIDIAN-UTF8-INVALID", "vault document is not valid UTF-8"
        ) from exc
    if "\x00" in content:
        raise ObsidianVaultError(
            "OBSIDIAN-BINARY-REFUSED", "binary content is not a Markdown document"
        )
    return {
        "schema": "productforge/ObsidianDocument/v1",
        "path": path.relative_to(root).as_posix(),
        "title": _title(path, content),
        "content": content,
        # Hash the bounded bytes already read; re-encoding the decoded body
        # doubles the work in the 10k-note import path without changing the
        # UTF-8 identity of the document.
        "content_hash": hashlib.sha256(raw_content).hexdigest(),
        "size": size,
        "state": "ready_for_import",
        "markdown": parse_markdown_document(content),
        "provenance": {
            "adapter": "stdlib-obsidian-markdown",
            "vault": root.name,
            "cli_required": False,
            "imported_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        },
    }


def import_vault_documents(
    vault: Path | str, raw_paths: list[str] | tuple[str, ...]
) -> list[dict[str, Any]]:
    """Import a bounded selection with one vault authorization/canonicalization pass."""
    if len(raw_paths) > 10_000:
        raise ObsidianVaultError(
            "OBSIDIAN-BATCH-LIMIT", "batch import is limited to 10000 documents"
        )
    root = validate_vault_authorization(vault)
    if not root.is_dir():
        raise ObsidianVaultError(
            "OBSIDIAN-VAULT-MISSING", "configured Obsidian vault is not a directory"
        )
    checked_directories: set[Path] = set()

    def _read(raw_path: str) -> dict[str, Any]:
        return _import_vault_document_from_root(
            root,
            raw_path,
            checked_directories=checked_directories,
        )

    workers = min(8, max(1, len(raw_paths)))
    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="obsidian-read") as pool:
        return list(pool.map(_read, raw_paths))


def scan_vault_documents(
    vault: Path | str,
    *,
    prefix: str = "",
    limit: int = 200,
) -> dict[str, Any]:
    """List safe Markdown paths without reading document bodies.

    The scanner is metadata-only: callers choose a path here and then invoke
    the bounded preview/import operation for the selected document. Traversal
    never follows symlinks/reparse points and Forge-managed directories are
    excluded by policy.
    """
    if not 1 <= limit <= 10_000:
        raise ObsidianVaultError("OBSIDIAN-SCAN-LIMIT", "scan limit must be between 1 and 10000")
    root = validate_vault_authorization(vault)
    if not root.is_dir():
        raise ObsidianVaultError(
            "OBSIDIAN-VAULT-MISSING", "configured Obsidian vault is not a directory"
        )
    normalized = prefix.replace("\\", "/").strip("/")
    invalid_prefix = any(part in {"", ".", ".."} for part in normalized.split("/"))
    if normalized and ("\x00" in normalized or invalid_prefix):
        raise ObsidianVaultError("OBSIDIAN-PATH-REFUSED", "scan prefix must stay inside the vault")
    start = root.joinpath(*normalized.split("/")) if normalized else root
    if (
        not _inside(start, root)
        or (_reparse(start) if start.exists() else False)
        or not start.is_dir()
    ):
        raise ObsidianVaultError(
            "OBSIDIAN-PATH-REFUSED", "scan prefix is not a safe vault directory"
        )

    blocked_dirs = {".git", ".obsidian", ".forge", ".cache", "__pycache__"}
    items: list[dict[str, Any]] = []
    skipped = 0
    stack = [start]
    while stack and len(items) < limit:
        directory = stack.pop()
        try:
            entries = sorted(os.scandir(directory), key=lambda item: item.name.casefold())
        except OSError as exc:
            raise ObsidianVaultError(
                "OBSIDIAN-SCAN-FAILED", "vault directory could not be scanned"
            ) from exc
        for entry in entries:
            if entry.name.lower() in blocked_dirs and entry.is_dir(follow_symlinks=False):
                continue
            try:
                info = entry.stat(follow_symlinks=False)
            except OSError:
                skipped += 1
                continue
            if _reparse_stat(info):
                skipped += 1
                continue
            if entry.is_dir(follow_symlinks=False):
                stack.append(Path(entry.path))
                continue
            if not entry.is_file(follow_symlinks=False) or not entry.name.lower().endswith(".md"):
                continue
            if info.st_nlink > 1 or info.st_size > MAX_VAULT_BYTES:
                skipped += 1
                continue
            relative = Path(entry.path).relative_to(root).as_posix()
            items.append(
                {
                    "path": relative,
                    "title": Path(relative)
                    .stem.replace("-", " ")
                    .replace("_", " ")
                    .strip()
                    .title(),
                    "size": info.st_size,
                    "modified_at": (
                        datetime.fromtimestamp(info.st_mtime, timezone.utc)
                        .isoformat()
                        .replace("+00:00", "Z")
                    ),
                }
            )
            if len(items) >= limit:
                break
    items.sort(key=lambda item: str(item["path"]).casefold())
    return {
        "schema": "theforge/ObsidianVaultFileIndex/v1",
        "version": "v1",
        "prefix": normalized,
        "items": items,
        "count": len(items),
        "truncated": bool(stack),
        "warnings": [f"{skipped} unsafe or unreadable entries were omitted"] if skipped else [],
    }


def _internal_dir(root: Path, name: str) -> Path:
    """Return a safe adapter-owned directory below the vault's .forge area."""
    forge_dir = root / ".forge"
    if forge_dir.exists() and (_reparse(forge_dir) or not forge_dir.is_dir()):
        raise ObsidianVaultError(
            "OBSIDIAN-PATH-REFUSED", "vault adapter directory is not a safe directory"
        )
    try:
        forge_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise ObsidianVaultError(
            "OBSIDIAN-PATH-REFUSED", "vault adapter directory could not be created"
        ) from exc
    if _reparse(forge_dir) or not forge_dir.is_dir():
        raise ObsidianVaultError(
            "OBSIDIAN-PATH-REFUSED", "vault adapter directory is not a safe directory"
        )
    directory = forge_dir / name
    if directory.exists() and (_reparse(directory) or not directory.is_dir()):
        raise ObsidianVaultError(
            "OBSIDIAN-PATH-REFUSED", "vault adapter directory is not a safe directory"
        )
    try:
        directory.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise ObsidianVaultError(
            "OBSIDIAN-PATH-REFUSED", "vault adapter directory could not be created"
        ) from exc
    if _reparse(directory) or not directory.is_dir():
        raise ObsidianVaultError(
            "OBSIDIAN-PATH-REFUSED", "vault adapter directory is not a safe directory"
        )
    return directory


@contextmanager
def _vault_write_lock(vault: Path | str, raw_path: str) -> Iterator[None]:
    """Serialize sync mutations for one canonical vault/document across processes."""
    root = validate_vault_authorization(vault)
    if not root.is_dir():
        raise ObsidianVaultError(
            "OBSIDIAN-VAULT-MISSING", "configured Obsidian vault is not a directory"
        )
    path = _safe(root, raw_path, for_write=True)
    lock_dir = _internal_dir(root, "obsidian-locks")
    lock_name = hashlib.sha256(path.relative_to(root).as_posix().encode("utf-8")).hexdigest()
    lock_path = lock_dir / f"{lock_name}.lock"
    if lock_path.exists() and _reparse(lock_path):
        raise ObsidianVaultError("OBSIDIAN-PATH-REFUSED", "vault lock is not a safe regular file")
    open_flags = os.O_CREAT | os.O_RDWR
    if hasattr(os, "O_NOFOLLOW"):
        open_flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(lock_path, open_flags, 0o600)
    except OSError as exc:
        raise ObsidianVaultError(
            "OBSIDIAN-LOCK-FAILED", "vault document lock could not be opened"
        ) from exc
    lock_file = os.fdopen(descriptor, "a+b")
    acquired = False
    try:
        if lock_file.seek(0, os.SEEK_END) == 0:
            lock_file.write(b"0")
            lock_file.flush()
        lock_file.seek(0)
        if os.name == "nt":
            import msvcrt

            acquired = False
            while not acquired:
                try:
                    msvcrt.locking(lock_file.fileno(), msvcrt.LK_NBLCK, 1)
                    acquired = True
                except OSError:
                    time.sleep(0.05)
        else:
            import fcntl

            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        acquired = True
        yield
    except OSError as exc:
        raise ObsidianVaultError(
            "OBSIDIAN-LOCK-FAILED", "vault document lock could not be acquired"
        ) from exc
    finally:
        if acquired:
            try:
                lock_file.seek(0)
                if os.name == "nt":
                    import msvcrt

                    msvcrt.locking(lock_file.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
            except OSError:
                # A full or revoked filesystem must not mask the operation's
                # structured result while releasing the advisory lock.
                pass
        try:
            lock_file.close()
        except OSError:
            # close() may flush a buffered byte after an ENOSPC during setup.
            pass


def _persist_sync_receipt(root: Path, receipt: dict[str, Any]) -> dict[str, Any]:
    receipt_key = str(receipt.pop("_receipt_key", receipt["content_hash"]))
    receipt_path = _internal_dir(root, "obsidian-receipts") / f"{receipt_key}.json"
    receipt["receipt_path"] = receipt_path.relative_to(root).as_posix()
    _atomic_write(
        receipt_path,
        json.dumps(receipt, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
            "utf-8"
        ),
    )
    return receipt


def _export_vault_document_unlocked(
    vault: Path | str,
    raw_path: str,
    content: str,
    *,
    baseline_hash: str | None = None,
    classification: str = "internal",
    source: str = "product-forge",
) -> dict[str, Any]:
    """Export without silently overwriting a manually changed note.

    A new note can be created directly. An existing note requires its current
    hash as ``baseline_hash``; the old bytes are snapshotted before an atomic
    replace and the post-write hash is verified.
    """
    root = validate_vault_authorization(vault)
    if not root.is_dir():
        raise ObsidianVaultError(
            "OBSIDIAN-VAULT-MISSING", "configured Obsidian vault is not a directory"
        )
    if len(content.encode("utf-8")) > MAX_VAULT_BYTES:
        raise ObsidianVaultError(
            "OBSIDIAN-SIZE-LIMIT", "vault document exceeds the safe size limit"
        )
    classification = _classification(classification)
    path = _safe(root, raw_path, for_write=True)
    candidate_bytes = content.encode("utf-8")
    candidate_hash = hashlib.sha256(candidate_bytes).hexdigest()
    previous_hash = None
    backup_path = None
    if path.exists():
        previous_bytes = path.read_bytes()
        previous_hash = hashlib.sha256(previous_bytes).hexdigest()
        if previous_hash == candidate_hash:
            return _persist_sync_receipt(
                root,
                {
                    "schema": "productforge/ObsidianDocument/v1",
                    "receipt_schema": "theforge/ObsidianSyncReceipt/v1",
                    "version": "v1",
                    "workspace": str(root.parent),
                    "vault_id": root.name,
                    "path": path.relative_to(root).as_posix(),
                    "title": _title(path, content),
                    "content_hash": candidate_hash,
                    "previous_hash": previous_hash,
                    "size": len(candidate_bytes),
                    "state": "exported",
                    "sync_state": "unchanged",
                    "outcome": "unchanged",
                    "classification": classification,
                    "source": source,
                    "backup_path": None,
                    "warnings": ["candidate already matches the vault document"],
                    "unresolved": [],
                    "exported_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                    "provenance": {
                        "adapter": "stdlib-obsidian-markdown",
                        "vault": root.name,
                        "cli_required": False,
                    },
                },
            )
        if baseline_hash is None:
            raise ObsidianVaultError(
                "OBSIDIAN-CONFLICT",
                "destination exists; provide baseline_hash before replacing it",
            )
        if baseline_hash != previous_hash:
            raise ObsidianVaultError(
                "OBSIDIAN-CONFLICT",
                "destination changed after the sync baseline was captured",
            )
        backup_dir = _internal_dir(root, "obsidian-backups")
        backup_path = backup_dir / f"{previous_hash}.md"
        if not backup_path.exists():
            _atomic_write(backup_path, previous_bytes)
    # Revalidate the destination immediately before the replace. This closes
    # the common parent-swap window without pretending a portable pathlib
    # check is an OS-level capability handle.
    if _safe(root, raw_path, for_write=True) != path:
        raise ObsidianVaultError(
            "OBSIDIAN-PATH-REFUSED", "vault destination changed during validation"
        )
    _atomic_write(path, candidate_bytes)
    if hashlib.sha256(path.read_bytes()).hexdigest() != candidate_hash:
        raise ObsidianVaultError("OBSIDIAN-INTEGRITY", "post-write hash verification failed")
    sync_state = "created" if previous_hash is None else "updated"
    receipt = {
        # Keep the original document schema/state for existing API consumers;
        # the additive receipt fields expose the stronger sync contract.
        "schema": "productforge/ObsidianDocument/v1",
        "receipt_schema": "theforge/ObsidianSyncReceipt/v1",
        "version": "v1",
        "workspace": str(root.parent),
        "vault_id": root.name,
        "path": path.relative_to(root).as_posix(),
        "title": _title(path, content),
        "content_hash": candidate_hash,
        "previous_hash": previous_hash,
        "size": len(candidate_bytes),
        "state": "exported",
        "sync_state": sync_state,
        "outcome": sync_state,
        "classification": classification,
        "source": source,
        "backup_path": backup_path.relative_to(root).as_posix() if backup_path else None,
        "warnings": [],
        "unresolved": [],
        "exported_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "provenance": {
            "adapter": "stdlib-obsidian-markdown",
            "vault": root.name,
            "cli_required": False,
        },
    }
    return _persist_sync_receipt(root, receipt)


def export_vault_document(
    vault: Path | str,
    raw_path: str,
    content: str,
    *,
    baseline_hash: str | None = None,
    classification: str = "internal",
    source: str = "product-forge",
) -> dict[str, Any]:
    """Export a document while serializing concurrent writers for its path."""
    with _vault_write_lock(vault, raw_path):
        return _export_vault_document_unlocked(
            vault,
            raw_path,
            content,
            baseline_hash=baseline_hash,
            classification=classification,
            source=source,
        )


def _skip_vault_sync_unlocked(
    vault: Path | str,
    raw_path: str,
    content: str,
    *,
    reason: str = "operation skipped by policy",
    classification: str = "internal",
    source: str = "product-forge",
) -> dict[str, Any]:
    """Record an intentional no-op without mutating the note body."""
    root = validate_vault_authorization(vault)
    if not root.is_dir():
        raise ObsidianVaultError(
            "OBSIDIAN-VAULT-MISSING", "configured Obsidian vault is not a directory"
        )
    if len(content.encode("utf-8")) > MAX_VAULT_BYTES:
        raise ObsidianVaultError(
            "OBSIDIAN-SIZE-LIMIT", "vault document exceeds the safe size limit"
        )
    classification = _classification(classification)
    path = _safe(root, raw_path, for_write=True)
    previous_hash = hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else None
    candidate_bytes = content.encode("utf-8")
    return _persist_sync_receipt(
        root,
        {
            "schema": "productforge/ObsidianDocument/v1",
            "receipt_schema": "theforge/ObsidianSyncReceipt/v1",
            "version": "v1",
            "workspace": str(root.parent),
            "vault_id": root.name,
            "path": path.relative_to(root).as_posix(),
            "title": _title(path, content),
            "content_hash": hashlib.sha256(candidate_bytes).hexdigest(),
            "previous_hash": previous_hash,
            "size": len(candidate_bytes),
            "state": "skipped",
            "sync_state": "skipped",
            "outcome": "skipped",
            "classification": classification,
            "source": source,
            "backup_path": None,
            "warnings": [reason[:240]],
            "unresolved": [],
            "exported_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "provenance": {
                "adapter": "stdlib-obsidian-markdown",
                "vault": root.name,
                "cli_required": False,
                "mutation": "none",
            },
        },
    )


def skip_vault_sync(
    vault: Path | str,
    raw_path: str,
    content: str,
    *,
    reason: str = "operation skipped by policy",
    classification: str = "internal",
    source: str = "product-forge",
) -> dict[str, Any]:
    """Record an intentional no-op while serializing concurrent sync decisions."""
    with _vault_write_lock(vault, raw_path):
        return _skip_vault_sync_unlocked(
            vault,
            raw_path,
            content,
            reason=reason,
            classification=classification,
            source=source,
        )


def _rollback_vault_document_unlocked(
    vault: Path | str,
    raw_path: str,
    *,
    receipt_path: str,
    source: str = "product-forge",
) -> dict[str, Any]:
    """Restore the exact backup referenced by a receipt after explicit review.

    Rollback is conditional: the destination must still contain the hash from
    the receipt being reverted. A later manual edit therefore becomes a
    conflict instead of being silently replaced.
    """
    root = validate_vault_authorization(vault)
    if not root.is_dir():
        raise ObsidianVaultError(
            "OBSIDIAN-VAULT-MISSING", "configured Obsidian vault is not a directory"
        )
    receipt_file = _resolved(root / receipt_path)
    if not _inside(receipt_file, root) or receipt_file.parts[-3:] != (
        ".forge",
        "obsidian-receipts",
        receipt_file.name,
    ):
        raise ObsidianVaultError(
            "OBSIDIAN-RECEIPT-REFUSED",
            "receipt must come from the vault receipt ledger",
        )
    if (
        _reparse(receipt_file)
        or not receipt_file.is_file()
        or receipt_file.stat().st_size > MAX_INTEROP_BYTES
    ):
        raise ObsidianVaultError("OBSIDIAN-RECEIPT-REFUSED", "receipt is not a safe regular file")
    try:
        receipt = json.loads(receipt_file.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ObsidianVaultError("OBSIDIAN-RECEIPT-INVALID", "receipt is not valid JSON") from exc
    if (
        not isinstance(receipt, dict)
        or receipt.get("receipt_schema") != "theforge/ObsidianSyncReceipt/v1"
    ):
        raise ObsidianVaultError("OBSIDIAN-RECEIPT-INVALID", "receipt schema is unsupported")
    if str(receipt.get("path")) != raw_path.replace("\\", "/"):
        raise ObsidianVaultError(
            "OBSIDIAN-ROLLBACK-PATH", "rollback path does not match the receipt"
        )
    backup_path = str(receipt.get("backup_path") or "")
    backup_file = _resolved(root / backup_path)
    if (
        not backup_path
        or not _inside(backup_file, root)
        or not backup_path.startswith(".forge/obsidian-backups/")
    ):
        raise ObsidianVaultError(
            "OBSIDIAN-BACKUP-REFUSED", "receipt does not reference a vault backup"
        )
    if (
        _reparse(backup_file)
        or not backup_file.is_file()
        or backup_file.stat().st_size > MAX_VAULT_BYTES
    ):
        raise ObsidianVaultError("OBSIDIAN-BACKUP-REFUSED", "backup is not a safe regular file")
    path = _safe(root, raw_path, for_write=True)
    if not path.exists():
        raise ObsidianVaultError(
            "OBSIDIAN-ROLLBACK-CONFLICT", "destination was removed after export"
        )
    current_bytes = path.read_bytes()
    current_hash = hashlib.sha256(current_bytes).hexdigest()
    if current_hash != str(receipt.get("content_hash")):
        raise ObsidianVaultError(
            "OBSIDIAN-ROLLBACK-CONFLICT",
            "destination changed after the export; review the manual edit before rollback",
        )
    restored_bytes = backup_file.read_bytes()
    restored_hash = hashlib.sha256(restored_bytes).hexdigest()
    if restored_hash != str(receipt.get("previous_hash")):
        raise ObsidianVaultError(
            "OBSIDIAN-BACKUP-INTEGRITY", "backup hash does not match the receipt"
        )
    _atomic_write(path, restored_bytes)
    if hashlib.sha256(path.read_bytes()).hexdigest() != restored_hash:
        raise ObsidianVaultError("OBSIDIAN-INTEGRITY", "post-rollback hash verification failed")
    timestamp = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    rollback_id = hashlib.sha256(
        f"{current_hash}:{restored_hash}:{timestamp}".encode()
    ).hexdigest()[:24]
    return _persist_sync_receipt(
        root,
        {
            "schema": "productforge/ObsidianDocument/v1",
            "receipt_schema": "theforge/ObsidianSyncReceipt/v1",
            "version": "v1",
            "workspace": str(root.parent),
            "vault_id": root.name,
            "path": path.relative_to(root).as_posix(),
            "title": _title(path, restored_bytes.decode("utf-8")),
            "content_hash": restored_hash,
            "previous_hash": current_hash,
            "size": len(restored_bytes),
            "state": "exported",
            "sync_state": "rolled_back",
            "outcome": "rolled_back",
            "classification": str(receipt.get("classification") or "internal"),
            "source": source,
            "backup_path": backup_path,
            "warnings": [f"rollback of receipt {receipt_file.name}"],
            "unresolved": [],
            "exported_at": timestamp,
            "provenance": {
                "adapter": "stdlib-obsidian-markdown",
                "vault": root.name,
                "cli_required": False,
                "rollback_id": rollback_id,
            },
            "_receipt_key": f"{restored_hash}.rollback-{rollback_id}",
        },
    )


def rollback_vault_document(
    vault: Path | str,
    raw_path: str,
    *,
    receipt_path: str,
    source: str = "product-forge",
) -> dict[str, Any]:
    """Restore a reviewed backup while serializing concurrent mutations."""
    with _vault_write_lock(vault, raw_path):
        return _rollback_vault_document_unlocked(
            vault,
            raw_path,
            receipt_path=receipt_path,
            source=source,
        )


def plan_vault_sync(
    vault: Path | str,
    raw_path: str,
    content: str,
    *,
    baseline_hash: str | None = None,
    classification: str = "internal",
    source: str = "product-forge",
) -> dict[str, Any]:
    """Create a dry-run sync plan; it never mutates the vault."""
    root = validate_vault_authorization(vault)
    if not root.is_dir():
        raise ObsidianVaultError(
            "OBSIDIAN-VAULT-MISSING", "configured Obsidian vault is not a directory"
        )
    if len(content.encode("utf-8")) > MAX_VAULT_BYTES:
        raise ObsidianVaultError(
            "OBSIDIAN-SIZE-LIMIT", "vault document exceeds the safe size limit"
        )
    classification = _classification(classification)
    path = _safe(root, raw_path, for_write=True)
    candidate_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()
    current_hash = hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else None
    if current_hash is None:
        state = "created"
    elif current_hash == candidate_hash:
        state = "unchanged"
    elif baseline_hash is None or baseline_hash != current_hash:
        state = "conflict"
    else:
        state = "updated"
    return {
        "schema": "theforge/ObsidianSyncPlan/v1",
        "version": "v1",
        "workspace": str(root.parent),
        "vault_id": root.name,
        "path": path.relative_to(root).as_posix(),
        "source": source,
        "classification": classification,
        "content_hash": candidate_hash,
        "previous_hash": current_hash,
        "baseline_hash": baseline_hash,
        "outcome": state,
        "warnings": ["confirmation required for mutation"]
        if state in {"created", "updated"}
        else [],
        "unresolved": ["manual conflict resolution required"] if state == "conflict" else [],
        "planned_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
    }


def list_obsidian_receipts(vault: Path | str, *, limit: int = 50) -> list[dict[str, Any]]:
    """Return bounded, body-free export history from the vault's Forge ledger."""
    if not 1 <= limit <= 100:
        raise ObsidianVaultError(
            "OBSIDIAN-HISTORY-LIMIT", "history limit must be between 1 and 100"
        )
    root = validate_vault_authorization(vault)
    if not root.is_dir():
        raise ObsidianVaultError(
            "OBSIDIAN-VAULT-MISSING", "configured Obsidian vault is not a directory"
        )
    forge_dir = root / ".forge"
    if not forge_dir.exists():
        return []
    if _reparse(forge_dir) or not forge_dir.is_dir():
        raise ObsidianVaultError(
            "OBSIDIAN-SYMLINK-REFUSED",
            "vault receipt ledger parent is a symlink or reparse point",
        )
    receipt_dir = forge_dir / "obsidian-receipts"
    if not receipt_dir.exists():
        return []
    if _reparse(receipt_dir) or not receipt_dir.is_dir():
        raise ObsidianVaultError(
            "OBSIDIAN-SYMLINK-REFUSED",
            "vault receipt ledger is a symlink or reparse point",
        )
    entries: list[tuple[float, dict[str, Any]]] = []
    for path in receipt_dir.glob("*.json"):
        try:
            if not path.is_file() or _reparse(path) or path.stat().st_size > MAX_INTEROP_BYTES:
                continue
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            continue
        if (
            not isinstance(value, dict)
            or value.get("receipt_schema") != "theforge/ObsidianSyncReceipt/v1"
        ):
            continue
        entries.append((path.stat().st_mtime, {k: v for k, v in value.items() if k != "content"}))
    entries.sort(key=lambda item: (-item[0], str(item[1].get("path", ""))))
    return [value for _, value in entries[:limit]]


def _atomic_write(path: Path, content: bytes) -> None:
    temporary: str | None = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        original_mode = stat.S_IMODE(path.stat().st_mode) if path.exists() else None
        fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
        if original_mode is not None:
            os.chmod(temporary, original_mode)
        with os.fdopen(fd, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.replace(temporary, path)
        except OSError as exc:
            raise ObsidianVaultError(
                "OBSIDIAN-WRITE-FAILED", "atomic vault replace failed"
            ) from exc
    except ObsidianVaultError:
        raise
    except OSError as exc:
        raise ObsidianVaultError("OBSIDIAN-WRITE-FAILED", "atomic vault write failed") from exc
    finally:
        if temporary is not None:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass
