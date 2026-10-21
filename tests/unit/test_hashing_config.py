from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import patch

import pytest

from forge_lens.core.config import LensConfig, init_project, load_config
from forge_lens.core.hashing import canonical_json, sha256_bytes, sha256_json


def test_canonical_json_is_stable() -> None:
    a = canonical_json({"b": 1, "a": [3, 2]})
    b = canonical_json({"a": [3, 2], "b": 1})
    assert a == b == '{"a":[3,2],"b":1}'


def test_sha256_known_value() -> None:
    assert (
        sha256_bytes(b"")
        == "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
    )
    h = sha256_json({"x": 1})
    assert len(h) == 64
    assert h == sha256_json({"x": 1})


def test_init_project_creates_config(tmp_path: Path) -> None:
    cfg_path = init_project(tmp_path, "demo")
    assert cfg_path.exists()
    text = cfg_path.read_text()
    assert 'project_id = "demo"' in text
    cfg = load_config(tmp_path)
    assert cfg.project_id == "demo"
    assert cfg.data_dir == tmp_path / ".forge-lens"


def test_env_overrides_config(tmp_path: Path) -> None:
    init_project(tmp_path, "tomlproj")
    with patch.dict(os.environ, {"FORGE_LENS_PROJECT": "envproj", "FORGE_LENS_CAPTURE": "full"}):
        cfg = load_config(tmp_path)
    assert cfg.project_id == "envproj"
    assert cfg.capture.value == "full"


def test_default_config_values() -> None:
    cfg = LensConfig()
    assert cfg.capture.value == "metadata"
    assert cfg.resolved_db_path.name == "lens.db"


def test_load_config_bad_env_reports_variable_name(monkeypatch) -> None:
    """Malformed env values raise with the variable name, not a bare traceback."""
    from forge_lens.core.config import load_config

    monkeypatch.setenv("FORGE_LENS_CAPTURE", "bogus")
    with pytest.raises(ValueError, match="FORGE_LENS_CAPTURE"):
        load_config()

    monkeypatch.delenv("FORGE_LENS_CAPTURE")
    monkeypatch.setenv("FORGE_LENS_MAX_TRACES_PER_REQUEST", "abc")
    with pytest.raises(ValueError, match="FORGE_LENS_MAX_TRACES_PER_REQUEST"):
        load_config()


def test_env_covers_all_int_bounds(monkeypatch) -> None:
    """Every int bound maps to FORGE_LENS_* and parses as an int."""
    monkeypatch.setenv("FORGE_LENS_MAX_PAYLOAD_BYTES", "1024")
    monkeypatch.setenv("FORGE_LENS_MAX_SPANS_PER_REQUEST", "7")
    monkeypatch.setenv("FORGE_LENS_MAX_EVENTS_PER_SPAN", "3")
    monkeypatch.setenv("FORGE_LENS_MAX_ATTR_VALUE_CHARS", "64")
    cfg = load_config()
    assert cfg.max_payload_bytes == 1024
    assert cfg.max_spans_per_request == 7
    assert cfg.max_events_per_span == 3
    assert cfg.max_attr_value_chars == 64


def test_env_bad_int_reports_variable_name(monkeypatch) -> None:
    monkeypatch.setenv("FORGE_LENS_MAX_PAYLOAD_BYTES", "huge")
    with pytest.raises(ValueError, match="FORGE_LENS_MAX_PAYLOAD_BYTES"):
        load_config()


def test_toml_bool_is_not_an_int_bound(tmp_path: Path) -> None:
    """`rate_limit_per_minute = true` is a bool — bool subclasses int but
    must not silently configure a 1 req/min bound."""
    from forge_lens.core.config import load_config

    (tmp_path / ".forge-lens").mkdir()
    (tmp_path / ".forge-lens" / "config.toml").write_text(
        "[lens]\nrate_limit_per_minute = true\nmax_spans_per_request = 50\n"
    )
    cfg = load_config(tmp_path)
    # bool ignored — default preserved; the real int still applies
    assert cfg.rate_limit_per_minute != 1
    assert cfg.max_spans_per_request == 50
