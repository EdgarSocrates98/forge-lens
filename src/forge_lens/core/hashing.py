"""Deterministic hashing for evidence, dedupe and integrity checks."""

from __future__ import annotations

import hashlib
import json
from typing import Any


def canonical_json(value: Any) -> str:
    """Stable serialization: sorted keys, compact separators, UTF-8."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_json(value: Any) -> str:
    """SHA-256 over the canonical JSON form of ``value``."""
    return sha256_bytes(canonical_json(value).encode("utf-8"))
