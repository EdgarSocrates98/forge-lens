"""API-key lifecycle and role/tenant resolution.

Secrets are ``flk_`` + ``secrets.token_urlsafe`` — shown to the caller
exactly once; the store keeps only the SHA-256 hex digest, so a leaked
database never yields usable keys.

Roles (ordered): ``reader`` ⊂ ``writer`` ⊂ ``admin``. A key's
``projects`` list scopes which ``project_id`` values it may touch;
an empty list means *unscoped* (all projects) — the honest default
for a local single-user tool.
"""

from __future__ import annotations

import hashlib
import secrets
from dataclasses import dataclass

ROLES = ("reader", "writer", "admin")
ROLE_ORDER = {r: i for i, r in enumerate(ROLES)}

_KEY_PREFIX = "flk_"


@dataclass(frozen=True)
class KeyIdentity:
    """A resolved credential — never carries the secret itself."""

    actor: str  # "key:flk_ab12" | "local" | "config-key"
    role: str  # reader|writer|admin
    projects: tuple[str, ...]  # empty = unscoped
    key_id: str | None = None


def generate_key() -> tuple[str, str, str]:
    """→ ``(key_id, secret, hash)``: secret is the only thing to discard."""
    key_id = _KEY_PREFIX + secrets.token_hex(6)
    secret = _KEY_PREFIX + "sk_" + secrets.token_urlsafe(24)
    return key_id, secret, hash_secret(secret)


def hash_secret(secret: str) -> str:
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()


def role_at_least(role: str, required: str) -> bool:
    return ROLE_ORDER.get(role, -1) >= ROLE_ORDER.get(required, 99)


def project_allowed(identity: KeyIdentity, project_id: str | None) -> bool:
    """Tenant isolation: a scoped key may only touch its projects.

    ``project_id=None`` (unscoped requests) is allowed only for
    unscoped keys — a scoped key listing "everything" would silently
    widen its tenancy."""
    if not identity.projects:
        return True
    return project_id is not None and project_id in identity.projects
