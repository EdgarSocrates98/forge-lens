"""Security & governance — Cycle 08.

Key lifecycle (hashed-at-rest, secret shown once), role resolution
(reader/writer/admin), tenant scoping by project, and the MCP
security observer. The Lens is an *observer* — it flags, never blocks
tool calls at instrumentation time.
"""

from forge_lens.security.keys import (
    ROLE_ORDER,
    ROLES,
    KeyIdentity,
    generate_key,
    hash_secret,
    project_allowed,
    role_at_least,
)

__all__ = [
    "ROLES",
    "ROLE_ORDER",
    "KeyIdentity",
    "generate_key",
    "hash_secret",
    "project_allowed",
    "role_at_least",
]
