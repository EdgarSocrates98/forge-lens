from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import patch

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
