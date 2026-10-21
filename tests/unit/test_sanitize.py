from __future__ import annotations

from datetime import UTC, datetime

from forge_lens.contracts.v1.trace import SpanEvent, SpanLink, Trace
from forge_lens.core.config import CaptureLevel
from forge_lens.sanitize.redact import (
    TRUNCATION_MARKER,
    SanitizeReport,
    _is_sensitive_key,
    sanitize_attributes,
    sanitize_trace,
)


def _attrs() -> dict:
    return {
        "openai.api_key": "sk-ABCDEFGHIJKLMNOPQRSTUVWXYZ1234",
        "prompt": "call me at john.doe@example.com please",
        "auth": {"access_token": "bearer abcdefghijklmnopqrstuvwxyz0123456789"},
        "input.value": "the user prompt body",
        "safe.setting": "harmless",
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
    assert out["safe.setting"] == "harmless"


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
        SpanEvent(name="evt", time=datetime.now(UTC), attributes={"api_secret": "x"})
    )
    report = sanitize_trace(synthetic_trace)
    assert report.dropped_content >= 1
    assert synthetic_trace.spans[0].events[0].attributes["api_secret"] == "[REDACTED:key]"


def test_sanitize_trace_covers_link_attributes(synthetic_trace: Trace) -> None:
    """Regression: secrets in span.links[*].attributes must not persist."""
    synthetic_trace.spans[0].links.append(
        SpanLink(
            trace_id="c" * 32,
            span_id="f" * 16,
            attributes={"api_key": "secret123", "safe": "ok"},
        )
    )
    report = sanitize_trace(synthetic_trace)
    link = synthetic_trace.spans[0].links[0]
    assert link.attributes["api_key"] == "[REDACTED:key]"
    assert link.attributes["safe"] == "ok"
    assert report.redacted_keys >= 1


def test_generic_token_keys_redacted() -> None:
    """github.token / npm_token / *_token are credential keys, not metrics."""
    report = SanitizeReport()
    out = sanitize_attributes(
        {
            "github.token": "ghp_abc123",
            "npm_token": "npm_abc",
            "gitlab_token": "glpat-x",
            "bot_token": "slack-ish",
            "deploy_token": "deploy-ish",
            "auth.token": "auth-ish",
        },
        report,
        capture=CaptureLevel.FULL,
        max_chars=4096,
    )
    assert all(v == "[REDACTED:key]" for v in out.values())
    assert report.redacted_keys == 6


def test_credential_value_patterns() -> None:
    report = SanitizeReport()
    out = sanitize_attributes(
        {
            "gl": "use glpat-aBcD1234567890xYz_aB here",
            "npm": "npm_aBcD1234567890xYzAbcD1234567890",
            "pypi": "pypi-aBcD1234567890xYzAbcD1234567890aB",
            "hf": "hf_aBcD1234567890xYzAbcD1234567890aB",
            "gho": "gho_1234567890abcdefghij",
        },
        report,
        capture=CaptureLevel.FULL,
        max_chars=4096,
    )
    assert "[REDACTED:gitlab-pat]" in out["gl"]
    assert "[REDACTED:npm-token]" in out["npm"]
    assert "[REDACTED:pypi-token]" in out["pypi"]
    assert "[REDACTED:huggingface-token]" in out["hf"]
    assert "[REDACTED:github-token]" in out["gho"]


def test_list_truncation_leaves_marker() -> None:
    """Lists > MAX_LIST_ITEMS keep an explicit truncation marker — no silent drop."""
    report = SanitizeReport()
    out = sanitize_attributes(
        {"big": list(range(10_001))},
        report,
        capture=CaptureLevel.FULL,
        max_chars=4096,
    )
    assert len(out["big"]) == 10_001  # 10_000 items + marker
    assert out["big"][-1] == "[TRUNCATED: 1 items dropped]"
    assert report.truncated_values == 1


def test_mime_type_survives_metadata_capture() -> None:
    """input.mime_type/output.mime_type are metadata — must not be content-dropped."""
    report = SanitizeReport()
    out = sanitize_attributes(
        {
            "input.value": "the prompt body",
            "input.mime_type": "text/plain",
            "output.value": "the answer body",
            "output.mime_type": "application/json",
        },
        report,
        capture=CaptureLevel.METADATA,
        max_chars=4096,
    )
    assert out["input.value"]["_forge_lens.dropped"] is True
    assert out["output.value"]["_forge_lens.dropped"] is True
    assert out["input.mime_type"] == "text/plain"
    assert out["output.mime_type"] == "application/json"


def test_camelcase_token_keys_redacted() -> None:
    """accessToken/refreshToken/idToken & friends — camelCase credential
    keys must not bypass key redaction (metric keys stay untouched)."""
    report = SanitizeReport()
    out = sanitize_attributes(
        {
            "accessToken": "eyJhbGciOi.supersecret",
            "refreshToken": "rt_abcdef123456",
            "idToken": "idtok",
            "sessionToken": "st",
            "privateToken": "pt",
            # metric keys must survive — they end in tokens/count/usage
            "llm.token_count.input": 100,
            "gen_ai.usage.output_tokens": 42,
            "forge_lens.token_usage": {"input_tokens": 1},
        },
        report,
        capture=CaptureLevel.METADATA,
        max_chars=4096,
    )
    for key in ("accessToken", "refreshToken", "idToken", "sessionToken", "privateToken"):
        assert out[key] == "[REDACTED:key]", key
    assert out["llm.token_count.input"] == 100
    assert out["gen_ai.usage.output_tokens"] == 42
    assert out["forge_lens.token_usage"] == {"input_tokens": 1}
    assert report.redacted_keys == 5


def test_status_message_and_names_scrubbed(synthetic_trace: Trace) -> None:
    """status.message carries exception text — credentials there must
    not persist. Span/event names and source fields get the same scrub."""
    trace = synthetic_trace.model_copy(deep=True)
    key = "sk-ant-" + "a" * 30
    trace.spans[0].status.message = f"auth failed: key {key} rejected"
    trace.spans[0].name = f"call {key}"
    trace.spans[0].events = [
        SpanEvent(name=f"failed with {key}", time=datetime.now(tz=UTC))
    ]
    trace.status.message = f"top-level: {key}"
    trace.session_id = f"sess-{key}"
    trace.source.system = f"svc-{key}"
    trace.source.sdk_name = f"sdk {key}"

    report = sanitize_trace(trace)
    blob = repr(trace)
    assert key not in blob
    assert "[REDACTED:anthropic-key]" in trace.spans[0].status.message  # type: ignore[operator]
    assert "[REDACTED:anthropic-key]" in trace.spans[0].name
    assert "[REDACTED:anthropic-key]" in trace.spans[0].events[0].name
    assert "[REDACTED:anthropic-key]" in trace.status.message  # type: ignore[operator]
    assert "[REDACTED:anthropic-key]" in trace.session_id  # type: ignore[operator]
    assert "[REDACTED:anthropic-key]" in trace.source.system  # type: ignore[operator]
    assert report.redacted_values >= 6


def test_project_id_and_source_host_scrubbed(synthetic_trace: Trace) -> None:
    """project_id and source.host are producer-controlled free text —
    credential patterns apply (no e-mail mask: they are join keys)."""
    trace = synthetic_trace.model_copy(deep=True)
    key = "sk-ant-" + "b" * 30
    trace.project_id = f"proj-{key}"
    trace.source.host = f"host-{key}"

    sanitize_trace(trace)
    assert "[REDACTED:anthropic-key]" in trace.project_id
    assert "[REDACTED:anthropic-key]" in (trace.source.host or "")
    assert key not in repr(trace)


def test_depth_limit_drop_is_counted() -> None:
    """depth>8 returns a marker — and the report must count it."""
    report = SanitizeReport()
    deep: dict = {"a": 1}
    for _ in range(12):
        deep = {"n": deep}
    out = sanitize_attributes(
        {"deep": deep}, report, capture=CaptureLevel.FULL, max_chars=4096
    )
    assert "[DROPPED:depth-limit]" in repr(out)
    assert report.dropped_content > 0


def test_sensitive_key_denylist_covers_credential_families() -> None:
    """Credential attribute names must redact at the default metadata
    capture — anchored so lookalike words are never collateral."""
    sensitive = [
        "signing_key", "ssh_key", "session_key", "hmac_key",
        "encryption_key", "master_key", "db.signing.key", "license_key",
        "auth_key", "client_key", "service_account_key", "pgp.key",
        "passphrase", "my_passphrase", "wallet.pass-phrase",
        "pin", "user_pin", "card.pin", "pin_code", "pin.code",
        "otp", "login_otp", "otp.code", "otp_code", "otp.secret",
        "otp_number", "otp.number",
        "cvv", "cvv2", "card_cvc", "payment.cvc", "cvc3",
        "cvv22", "cvc123",
        "card_number", "cardNumber", "card.num", "card_numbers",
        "card_no",
        "security_code", "security.code",
        "social_security_number", "social_security",
        "aws_key", "aws_keys", "kms_key", "signing_keys",
        "access_tokens", "refresh.tokens", "id_tokens",
        "accessTokens", "refreshTokens", "idTokens",
    ]
    for key in sensitive:
        assert _is_sensitive_key(key), f"{key!r} must redact"


def test_sensitive_key_denylist_false_positives_stay_clear() -> None:
    harmless = [
        "monkey", "donkey", "turkey", "keyboard",
        "spinner", "shipping", "spin", "pinned.version", "keepin",
        "stop", "topic", "worktop",
        "service", "device", "invoice",
        "input_tokens", "output_tokens", "total_tokens",
        "llm.usage.prompt_tokens", "cardinality", "standard",
        # metric-counter lookalikes of the plural-token pair pattern
        "valid_tokens", "invalid_tokens", "paid_tokens", "rapid_tokens",
        "xaccess_tokens", "reaccess.tokens",
        # camelCase lookalikes — the left anchor still applies
        "validTokens", "paidTokens", "xaccessTokens",
        # legitimate telemetry `*key` names — OTel semconv included
        "messaging.kafka.message.key",
        "messaging.rabbitmq.message.routing_key",
        "partition_key", "primary_key", "sort_key", "hash_key",
        "lookup_key", "cache_key", "correlation_key", "message_key",
        "idempotency_key", "public_key", "wireguard.key",
        # pin/otp counters, not credentials
        "pin_count", "pin_retries", "login.pin_retries",
        "otp_count", "auth.otp_attempts", "cvv_count",
        # card/security lookalikes
        "discard_number", "wildcard.number", "card_number_count",
        "security_code_review", "mysecurity_code", "cardano",
        "social_security_reform",
        # `aws`/`kms` inside a longer word, or non-credential `*key`
        "xaws.key", "awaken", "monkeys",
    ]
    for key in harmless:
        assert not _is_sensitive_key(key), f"{key!r} must stay clear"


def test_non_str_attribute_keys_are_normalized_counted() -> None:
    """Programmatic SDK misuse (int/tuple keys) must not reach the
    serializer — str() coercion is counted as evidence."""
    report = SanitizeReport()
    out = sanitize_attributes(
        {1: "a", ("t", 1): "b", "nested": {9: "deep"}},
        report,
        capture=CaptureLevel.FULL,
        max_chars=4096,
    )
    assert out == {"1": "a", "('t', 1)": "b", "nested": {"9": "deep"}}
    assert report.normalized_keys == 3
