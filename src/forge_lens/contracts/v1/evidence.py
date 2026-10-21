"""Evidence contract (v1)."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Literal

from forge_lens.contracts.base import LensModel
from forge_lens.contracts.provenance import Provenance


class DataClassification(StrEnum):
    """Sensitivity classes used by the privacy pipeline."""

    METADATA = "metadata"
    TOOL_ARGUMENTS = "tool_arguments"
    TOOL_OUTPUTS = "tool_outputs"
    USER_PROMPT = "user_prompt"
    MODEL_RESPONSE = "model_response"
    PERSONAL_DATA = "personal_data"
    CREDENTIALS = "credentials"
    SENSITIVE_CODE = "sensitive_code"
    UNKNOWN = "unknown"


class SanitizationStatus(StrEnum):
    NONE = "none"
    REDACTED = "redacted"
    HASHED = "hashed"
    TRUNCATED = "truncated"
    DROPPED = "dropped"


class Evidence(LensModel):
    """A verifiable artifact that supports a claim."""

    schema_version: Literal["1.0.0"] = "1.0.0"
    evidence_id: str
    source: str
    """Producing system/component, e.g. ``forge-lens-sdk``, ``otlp``."""

    origin_type: Provenance = Provenance.OBSERVED
    timestamp: datetime
    hash: str | None = None
    """SHA-256 of the underlying payload, when available."""

    reference: str | None = None
    """Locator: span id, file path, artifact id, URI."""

    classification: DataClassification = DataClassification.UNKNOWN
    sanitization_status: SanitizationStatus = SanitizationStatus.NONE
