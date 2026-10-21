"""Default sanitization for ingested attributes.

Runs on every ingestion path (OTLP and native) before persistence:

1. Attribute keys matching sensitive patterns are replaced by
   ``[REDACTED]`` (key preserved, value destroyed).
2. String values matching credential patterns (API keys, bearer tokens,
   PEM blocks, e-mail addresses at level=metadata) are replaced inline.
3. ``capture=metadata`` drops content-bearing attributes (prompts,
   tool I/O bodies) and replaces them with ``{"_dropped": <bytes>}``.
4. Values longer than ``max_attr_value_chars`` are truncated with an
   explicit marker — never silently.

All replacements are countable: ``SanitizeReport`` feeds self-metrics.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from forge_lens.contracts.v1.trace import Trace
from forge_lens.core.config import CaptureLevel
from forge_lens.core.metrics import M_ATTRS_REDACTED, M_ATTRS_TRUNCATED, METRICS
from forge_lens.otlp.openinference import CONTENT_ATTRIBUTES

# Explicit credential-key patterns. Token-bearing *metric* keys are safe:
# ``llm.token_count.*``, ``gen_ai.usage.*_tokens`` and ``forge_lens.token_usage``
# never end in a bare ``token`` — so the ``token$`` suffix rule catches
# ``github.token``, ``npm_token`` *and* camelCase ``accessToken``/``idToken``
# without touching counters. Prefixes are also enumerated explicitly.
SENSITIVE_KEY_RE = re.compile(
    r"(?i)(api[-_.]?key|secret|password|passwd|pwd|authorization|"
    r"auth[-_.]?header|credential|private[-_.]?key|client[-_.]?secret|"
    r"access[-_.]?key|token$|bearer$|x-api-key|private-key|"
    r"cookie|set-cookie|ssn|credit[-_.]?card|"
    r"pass[-_.]?phrase|"
    # `*key` is enumerated, not wildcarded — `[-_.]key$` would also eat
    # legitimate telemetry keys (messaging.kafka.message.key, routing_key,
    # partition_key, primary_key, sort_key, hash_key, cache_key,
    # idempotency_key…). Only key names that are credential-shaped.
    r"(^|[-_.])(signing|ssh|session|hmac|encryption|decryption|master|"
    r"license|auth|client|pgp|gpg|tls|ssl|aws|kms|wallet|"
    r"service[-_.]?account)[-_.]?keys?$|"
    # pin: bare or credential-prefixed, separators optional —
    # `pinCode`/`cardPin` are camelCase credentials.
    # `pin_count`/`login.pin_retries` are counters. Accepted collision:
    # `pincode` (postal code) redacts — the credential-safe direction.
    r"(^|[-_.])(card|user|login|bank|account)?[-_.]?pin"
    r"([-_.]?(code|number|value|hash|secret))?$|"
    # otp with the same prefix set plus the canonical RFC 6238/4226
    # names (t|h): `totp`, `hotp`, `loginOtp`, `device.otp` redact;
    # `stop`/`worktop`/`otp_count` stay clear via the anchor
    r"(^|[-_.])(card|user|login|bank|account|device|my|t|h)?[-_.]?otp"
    r"([-_.]?(code|number|value|hash|secret))?$|"
    # cvv/cvc: bare, digits, or named — optional separator like every
    # sibling branch (`cvvNumber`); `cvv_count` is a counter
    r"(^|[-_.])(cvv|cvc)([0-9]*|[-_.]?(code|number|num|no|value|hash)s?)$|"
    # both sides anchored — `discard_number`, `wildcard.number`,
    # `card_number_count`, `security_code_review` stay clear
    r"(^|[-_.])card[-_.]?(number|num|no)s?$|"
    r"(^|[-_.])security[-_.]?code$|"
    r"(^|[-_.])social[-_.]?security([-_.]?(number|num|no)s?)?$|"
    # auth/verification-code family — enumerated so `discount_code`,
    # `error_code`, `status_code`, `zip_code`, `barcode`, `unicode`,
    # `promoCode`, `morse_code`, `qr_code` stay clear
    r"(^|[-_.])(auth|mfa|pass|verification|recovery|backup|activation|"
    r"access|login|confirm(ation)?|reset|one[-_.]?time)[-_.]?codes?$|"
    # crypto seed/recovery/mnemonic phrases + bare `mnemonic`
    r"(^|[-_.])(seed|recovery|backup|mnemonic|pass|secret)[-_.]?phrases?$|"
    r"(^|[-_.])mnemonics?$|"
    # plural token sets — mirrors the credential namespaces of the `*key`
    # family plus the flow names; every alternative left-anchored so
    # `valid_tokens`, `paid_tokens`, `rapidTokens`, `capitalTokens`,
    # `xauthTokens`, `invalidSessionTokens` (metric counters / lookalikes)
    # stay clear; bare `tokens$` must NOT match (see comment above)
    r"(^|[-_.])(access|refresh|id|session|auth|api|bearer|csrf|jwt|oauth|"
    r"client|master|signing|ssh|hmac|encryption|private|secret)"
    r"[-_.]?tokens$)"
)

VALUE_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"sk-ant-[A-Za-z0-9\-_]{20,}"), "[REDACTED:anthropic-key]"),
    (re.compile(r"sk-[A-Za-z0-9\-_]{20,}"), "[REDACTED:api-key]"),
    (re.compile(r"AIza[0-9A-Za-z\-_]{20,}"), "[REDACTED:gcp-key]"),
    (re.compile(r"AKIA[0-9A-Z]{16}"), "[REDACTED:aws-access-key]"),
    (re.compile(r"gh[pousr]_[A-Za-z0-9]{20,}"), "[REDACTED:github-token]"),
    (re.compile(r"github_pat_[A-Za-z0-9_]{20,}"), "[REDACTED:github-pat]"),
    (re.compile(r"glpat-[A-Za-z0-9\-_]{20,}"), "[REDACTED:gitlab-pat]"),
    (re.compile(r"npm_[A-Za-z0-9]{30,}"), "[REDACTED:npm-token]"),
    (re.compile(r"pypi-[A-Za-z0-9\-_]{30,}"), "[REDACTED:pypi-token]"),
    (re.compile(r"hf_[A-Za-z0-9]{30,}"), "[REDACTED:huggingface-token]"),
    (re.compile(r"xox[baprs]-[A-Za-z0-9\-]{10,}"), "[REDACTED:slack-token]"),
    (re.compile(r"xapp-[A-Za-z0-9\-]{10,}"), "[REDACTED:slack-token]"),
    (
        re.compile(
            r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.S
        ),
        "[REDACTED:pem]",
    ),
    (re.compile(r"(?i)bearer\s+[A-Za-z0-9\-._~+/]+=*"), "Bearer [REDACTED]"),
    (re.compile(r"eyJ[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]*"), "[REDACTED:jwt]"),
)

EMAIL_RE = re.compile(r"\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b")

TRUNCATION_MARKER = "...[TRUNCATED]"
DROPPED_MARKER = "_forge_lens.dropped"
MAX_LIST_ITEMS = 10_000


@dataclass
class SanitizeReport:
    redacted_keys: int = 0
    redacted_values: int = 0
    dropped_content: int = 0
    truncated_values: int = 0
    emails_masked: int = 0
    normalized_keys: int = 0
    """Non-str dict keys coerced with ``str()`` — JSON object keys are
    strings; a tuple/int key would otherwise raise in the serializer
    (and collide deterministically: ``1`` and ``"1"`` both → ``"1"``)."""


def _is_sensitive_key(key: str) -> bool:
    return bool(SENSITIVE_KEY_RE.search(key))


def _redact_string(value: str, report: SanitizeReport) -> str:
    out = value
    for pattern, replacement in VALUE_PATTERNS:
        out, n = pattern.subn(replacement, out)
        report.redacted_values += n
    return out


def _scrub_text(
    value: str, report: SanitizeReport, *, mask_emails: bool
) -> str:
    """Value-pattern scrub for free-text contract fields.

    ``status.message`` routinely carries exception text with embedded
    credentials, and names/source fields are arbitrary producer strings —
    same exposure class as attribute values, same policy.
    """
    out = _redact_string(value, report)
    if mask_emails:
        out, n = EMAIL_RE.subn("[MASKED:email]", out)
        report.emails_masked += n
    return out


def _sanitize_value(
    value: Any,
    report: SanitizeReport,
    *,
    capture: CaptureLevel,
    max_chars: int,
    mask_emails: bool,
    depth: int = 0,
) -> Any:
    if depth > 8:
        report.dropped_content += 1  # never a silent drop
        return "[DROPPED:depth-limit]"
    if isinstance(value, str):
        value = _redact_string(value, report)
        if mask_emails:
            value, n = EMAIL_RE.subn("[MASKED:email]", value)
            report.emails_masked += n
        if len(value) > max_chars:
            report.truncated_values += 1
            value = value[: max_chars] + TRUNCATION_MARKER
        return value
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for k, v in value.items():
            if not isinstance(k, str):
                k = str(k)
                report.normalized_keys += 1
            if _is_sensitive_key(k):
                report.redacted_keys += 1
                out[k] = "[REDACTED:key]"
            else:
                out[k] = _sanitize_value(
                    v, report, capture=capture, max_chars=max_chars,
                    mask_emails=mask_emails, depth=depth + 1,
                )
        return out
    if isinstance(value, list):
        items = [
            _sanitize_value(
                v, report, capture=capture, max_chars=max_chars,
                mask_emails=mask_emails, depth=depth + 1,
            )
            for v in value[:MAX_LIST_ITEMS]
        ]
        if len(value) > MAX_LIST_ITEMS:
            report.truncated_values += 1
            items.append(f"[TRUNCATED: {len(value) - MAX_LIST_ITEMS} items dropped]")
        return items
    return value


def sanitize_attributes(
    # declared str keys — producers can still hand us int/tuple keys
    # (contract models don't constrain dict keys), so the guard below
    # is load-bearing, not dead code
    attributes: dict[Any, Any],
    report: SanitizeReport,
    *,
    capture: CaptureLevel,
    max_chars: int,
) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in attributes.items():
        if not isinstance(key, str):
            key = str(key)
            report.normalized_keys += 1
        if _is_sensitive_key(key):
            report.redacted_keys += 1
            out[key] = "[REDACTED:key]"
            continue
        if capture == CaptureLevel.METADATA and key in CONTENT_ATTRIBUTES:
            report.dropped_content += 1
            approx = len(str(value)) if value is not None else 0
            out[key] = {DROPPED_MARKER: True, "_approx_bytes": approx}
            continue
        out[key] = _sanitize_value(
            value, report, capture=capture, max_chars=max_chars,
            mask_emails=(capture == CaptureLevel.METADATA),
        )
    return out


def sanitize_trace(
    trace: Trace,
    *,
    capture: CaptureLevel = CaptureLevel.METADATA,
    max_chars: int = 4_096,
) -> SanitizeReport:
    """Sanitize a trace in place. Returns what was touched.

    Attributes carry the obvious risk, but credentials also leak through
    free-text fields — ``status.message`` embeds exception text, span
    names are producer-controlled. Identity fields (trace/span ids) are
    never touched. ``session_id`` and ``project_id`` get credential
    patterns only (they are lookup/partition keys — e-mail masking would
    silently collapse or repartition them).
    """
    report = SanitizeReport()
    mask = capture == CaptureLevel.METADATA
    for span in trace.spans:
        span.attributes = sanitize_attributes(
            span.attributes, report, capture=capture, max_chars=max_chars
        )
        span.name = _scrub_text(span.name, report, mask_emails=mask)
        if span.status.message is not None:
            span.status.message = _scrub_text(
                span.status.message, report, mask_emails=mask
            )
        for event in span.events:
            event.attributes = sanitize_attributes(
                event.attributes, report, capture=capture, max_chars=max_chars
            )
            event.name = _scrub_text(event.name, report, mask_emails=mask)
        for link in span.links:
            link.attributes = sanitize_attributes(
                link.attributes, report, capture=capture, max_chars=max_chars
            )
    trace.resource_attributes = sanitize_attributes(
        trace.resource_attributes, report, capture=capture, max_chars=max_chars
    )
    if trace.status.message is not None:
        trace.status.message = _scrub_text(
            trace.status.message, report, mask_emails=mask
        )
    if trace.session_id is not None:
        trace.session_id = _scrub_text(
            trace.session_id, report, mask_emails=False
        )
    # project_id is a join key (like session_id): credential patterns
    # apply, e-mail masking does not — masking would silently repartition
    trace.project_id = _scrub_text(trace.project_id, report, mask_emails=False)
    for field_name in (
        "system",
        "sdk_name",
        "sdk_version",
        "host",
        "runtime",
        "language",
    ):
        value = getattr(trace.source, field_name)
        if value is not None:
            setattr(
                trace.source,
                field_name,
                _scrub_text(value, report, mask_emails=mask),
            )
    METRICS.inc(M_ATTRS_REDACTED, report.redacted_keys + report.redacted_values)
    METRICS.inc(M_ATTRS_TRUNCATED, report.truncated_values)
    return report
