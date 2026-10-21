"""Runtime configuration for Forge Lens.

Resolution order (highest wins):
1. explicit argument
2. environment variable (``FORGE_LENS_*``)
3. ``.forge-lens/config.toml`` in the current project
4. defaults
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path

DEFAULT_DIR_NAME = ".forge-lens"
DEFAULT_DB_NAME = "lens.db"


class CaptureLevel(StrEnum):
    """How much payload content may be captured.

    ``metadata`` (the safe default) keeps names, counts, hashes and sizes
    but drops bodies. ``full`` captures content — opt-in only.
    """

    METADATA = "metadata"
    FULL = "full"


@dataclass(frozen=True)
class LensConfig:
    project_id: str = "default"
    data_dir: Path = field(default_factory=lambda: Path.cwd() / DEFAULT_DIR_NAME)
    db_path: Path | None = None
    capture: CaptureLevel = CaptureLevel.METADATA
    api_key: str | None = None
    """When set, ingestion endpoints require ``X-Forge-Lens-Key``."""

    endpoint: str = "http://127.0.0.1:7777"
    max_payload_bytes: int = 8 * 1024 * 1024
    max_traces_per_request: int = 10_000
    max_spans_per_request: int = 20_000
    max_events_per_span: int = 128
    max_attr_value_chars: int = 4_096
    rate_limit_per_minute: int = 0
    """0 disables the in-process rate limiter (local-first default)."""

    @property
    def resolved_db_path(self) -> Path:
        return self.db_path or (self.data_dir / DEFAULT_DB_NAME)


def _toml_to_config(data: dict[str, object], base: LensConfig) -> LensConfig:
    section = data.get("lens", data)
    if not isinstance(section, dict):
        return base
    kwargs: dict[str, object] = {}
    if isinstance(v := section.get("project_id"), str):
        kwargs["project_id"] = v
    if isinstance(v := section.get("db_path"), str):
        kwargs["db_path"] = Path(v).expanduser()
    if isinstance(v := section.get("capture"), str):
        kwargs["capture"] = CaptureLevel(v)
    if isinstance(v := section.get("api_key"), str):
        kwargs["api_key"] = v
    if isinstance(v := section.get("endpoint"), str):
        kwargs["endpoint"] = v
    for key, field_name in (
        ("max_payload_bytes", "max_payload_bytes"),
        ("max_traces_per_request", "max_traces_per_request"),
        ("max_spans_per_request", "max_spans_per_request"),
        ("max_events_per_span", "max_events_per_span"),
        ("max_attr_value_chars", "max_attr_value_chars"),
        ("rate_limit_per_minute", "rate_limit_per_minute"),
    ):
        if isinstance(v := section.get(key), int):
            kwargs[field_name] = v
    return LensConfig(**{**base.__dict__, **kwargs})


def load_config(project_dir: Path | None = None) -> LensConfig:
    base = LensConfig()
    if project_dir is not None:
        base = LensConfig(**{**base.__dict__, "data_dir": project_dir / DEFAULT_DIR_NAME})

    cfg_file = base.data_dir / "config.toml"
    if cfg_file.exists():
        with cfg_file.open("rb") as fh:
            base = _toml_to_config(tomllib.load(fh), base)

    env_map: dict[str, str] = {
        "FORGE_LENS_PROJECT": "project_id",
        "FORGE_LENS_DB": "db_path",
        "FORGE_LENS_CAPTURE": "capture",
        "FORGE_LENS_API_KEY": "api_key",
        "FORGE_LENS_ENDPOINT": "endpoint",
        "FORGE_LENS_MAX_TRACES_PER_REQUEST": "max_traces_per_request",
        "FORGE_LENS_RATE_LIMIT_PER_MINUTE": "rate_limit_per_minute",
    }
    overrides: dict[str, object] = {}
    for env, field_name in env_map.items():
        raw = os.environ.get(env)
        if raw is None:
            continue
        if field_name == "db_path":
            overrides[field_name] = Path(raw).expanduser()
        elif field_name == "capture":
            overrides[field_name] = CaptureLevel(raw)
        elif field_name in {"rate_limit_per_minute", "max_traces_per_request"}:
            overrides[field_name] = int(raw)
        else:
            overrides[field_name] = raw
    if overrides:
        base = LensConfig(**{**base.__dict__, **overrides})
    return base


def init_project(project_dir: Path, project_id: str) -> Path:
    """Create ``.forge-lens/`` with a starter config. Returns config path."""
    data_dir = project_dir / DEFAULT_DIR_NAME
    data_dir.mkdir(parents=True, exist_ok=True)
    (data_dir / "artifacts").mkdir(exist_ok=True)
    cfg = data_dir / "config.toml"
    if not cfg.exists():
        cfg.write_text(
            "[lens]\n"
            f'project_id = "{project_id}"\n'
            'capture = "metadata"\n'
            'endpoint = "http://127.0.0.1:7777"\n'
            "# api_key = \"change-me\"  # set to require X-Forge-Lens-Key on ingest\n"
            "# rate_limit_per_minute = 600\n",
            encoding="utf-8",
        )
    return cfg
