from __future__ import annotations

from datetime import UTC, datetime

from forge_lens.contracts.v1.trace import Trace
from forge_lens.core.config import CaptureLevel
from forge_lens.sanitize.redact import (
    TRUNCATION_MARKER,
    SanitizeReport,
    sanitize_attributes,
    sanitize_trace,
)


def _attrs() -> dict:
    return {
        "openai.api_key": "sk-ABCDEFGHIJKLMNOPQRSTUVWXYZ1234",
        "prompt": "call me at john.doe@example.com please",
        "auth": {"access_token": "bearer abcdefghijklmnopqrstuvwxyz0123456789"},
        "input.value": "the user prompt body",
        "safe.key": "harmless",
    }


def test_sensitive_keys_redacted() -> None:
    report = SanitizeReport()
    out = sanitize_attributes(
        _attrs(), report, capture=CaptureLevel.FULL, max_chars=4096
    )
    assert out["openai.api_key"] == "[REDACTED:key]"
    assert out["auth"]["access_token"] == "[REDACTED:key]"
    assert report.redacted_keys == 2
    # value-level patterns fire even on non-sensitive keys at full capture
    assert "[MASKED:email]" not in out["prompt"]  # email only masked at metadata level
    assert out["safe.key"] == "harmless"


def test_metric_token_keys_are_not_redacted() -> None:
    """Regression: llm.token_count.* / *.usage.*tokens are counters, not secrets."""
    report = SanitizeReport()
    out = sanitize_attributes(
        {
            "llm.token_count.prompt": 512,
            "gen_ai.usage.input_tokens": 100,
            "forge_lens.token_usage": {"input_tokens": 10},
        },
        report,
        capture=CaptureLevel.FULL,
        max_chars=4096,
    )
    assert out["llm.token_count.prompt"] == 512
    assert out["gen_ai.usage.input_tokens"] == 100
    assert out["forge_lens.token_usage"]["input_tokens"] == 10
    assert report.redacted_keys == 0


def test_metadata_capture_drops_content_and_masks_email() -> None:
    report = SanitizeReport()
    out = sanitize_attributes(
        _attrs(), report, capture=CaptureLevel.METADATA, max_chars=4096
    )
    assert out["input.value"]["_forge_lens.dropped"] is True
    assert report.dropped_content == 1
    assert out["prompt"] == "call me at [MASKED:email] please"
    assert report.emails_masked == 1


def test_long_values_truncated_with_marker() -> None:
    report = SanitizeReport()
    out = sanitize_attributes(
        {"big": "x" * 10_000}, report, capture=CaptureLevel.FULL, max_chars=100
    )
    assert out["big"].endswith(TRUNCATION_MARKER)
    assert len(out["big"]) == 100 + len(TRUNCATION_MARKER)
    assert report.truncated_values == 1


def test_credential_values_redacted_inline() -> None:
    report = SanitizeReport()
    out = sanitize_attributes(
        {"note": "token is sk-liveABCDEFGHIJKLMNOPQRST done"},
        report,
        capture=CaptureLevel.FULL,
        max_chars=4096,
    )
    assert "sk-live" not in out["note"]
    assert "[REDACTED:" in out["note"]


def test_sanitize_trace_covers_events(synthetic_trace: Trace) -> None:
    synthetic_trace.spans[0].attributes["input.value"] = "prompt body"
    synthetic_trace.spans[0].events.append(
        # event with sensitive attr
        __import__("forge_lens.contracts.v1.trace", fromlist=["SpanEvent"]).SpanEvent(
            name="evt", time=datetime.now(UTC), attributes={"api_secret": "x"}
        )
    )
    report = sanitize_trace(synthetic_trace)
    assert report.dropped_content >= 1
    assert synthetic_trace.spans[0].events[0].attributes["api_secret"] == "[REDACTED:key]"
