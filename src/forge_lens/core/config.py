"""Runtime configuration for Forge Lens.

Resolution order (highest wins):
1. explicit argument
2. environment variable (``FORGE_LENS_*``)
3. ``.forge-lens/config.toml`` in the current project
4. defaults
"""

from __future__ import annotations

import json
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

    auth_reads: bool = False
    """When true (and ``api_key`` is set), query/read endpoints require
    the key too. Default off: local-first reads stay open by design."""

    auth_mode: str = "none"
    """``none`` (local single-user) | ``key`` (single shared key — implied
    by ``api_key``) | ``keys`` (table-managed keys with roles and
    per-project tenancy). In ``keys`` mode every request, reads
    included, must present a valid key — that is what makes tenant
    isolation enforceable."""

    retention_days: int = 0
    """0 disables automatic retention; >0 makes ``lens prune`` without an
    explicit ``--before`` delete traces older than this many days."""

    endpoint: str = "http://127.0.0.1:7777"
    max_payload_bytes: int = 8 * 1024 * 1024
    max_traces_per_request: int = 10_000
    max_spans_per_request: int = 20_000
    max_events_per_span: int = 128
    max_attr_value_chars: int = 4_096
    rate_limit_per_minute: int = 0
    """0 disables the in-process rate limiter (local-first default)."""

    pricing_file: Path | None = None
    """Optional JSON pricing table overriding the bundled registry."""

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
    if isinstance(v := section.get("auth_reads"), bool):
        kwargs["auth_reads"] = v
    if isinstance(v := section.get("auth_mode"), str) and v in {
        "none", "key", "keys",
    }:
        kwargs["auth_mode"] = v
    if isinstance(v := section.get("retention_days"), int) and not isinstance(
        v, bool
    ):
        kwargs["retention_days"] = v
    if isinstance(v := section.get("endpoint"), str):
        kwargs["endpoint"] = v
    if isinstance(v := section.get("pricing_file"), str):
        kwargs["pricing_file"] = Path(v).expanduser()
    for key, field_name in (
        ("max_payload_bytes", "max_payload_bytes"),
        ("max_traces_per_request", "max_traces_per_request"),
        ("max_spans_per_request", "max_spans_per_request"),
        ("max_events_per_span", "max_events_per_span"),
        ("max_attr_value_chars", "max_attr_value_chars"),
        ("rate_limit_per_minute", "rate_limit_per_minute"),
    ):
        # bool is an int subclass — `rate_limit_per_minute = true` would
        # silently mean 1 req/min; only a real int configures a bound
        if isinstance(v := section.get(key), int) and not isinstance(v, bool):
            kwargs[field_name] = v
    return LensConfig(**{**base.__dict__, **kwargs})


def load_config(project_dir: Path | None = None) -> LensConfig:
    base = LensConfig()
    if project_dir is not None:
        base = LensConfig(**{**base.__dict__, "data_dir": project_dir / DEFAULT_DIR_NAME})

    cfg_file = base.data_dir / "config.toml"
    if cfg_file.exists():
        try:
            with cfg_file.open("rb") as fh:
                base = _toml_to_config(tomllib.load(fh), base)
        except Exception as exc:
            # bare ValueError/KeyError from a config file is operator
            # hostile — name the file in the error
            raise ValueError(f"invalid config file {cfg_file}: {exc}") from exc

    env_map: dict[str, str] = {
        "FORGE_LENS_PROJECT": "project_id",
        "FORGE_LENS_DB": "db_path",
        "FORGE_LENS_CAPTURE": "capture",
        "FORGE_LENS_API_KEY": "api_key",
        "FORGE_LENS_AUTH_READS": "auth_reads",
        "FORGE_LENS_AUTH_MODE": "auth_mode",
        "FORGE_LENS_RETENTION_DAYS": "retention_days",
        "FORGE_LENS_ENDPOINT": "endpoint",
        "FORGE_LENS_PRICING_FILE": "pricing_file",
        "FORGE_LENS_MAX_PAYLOAD_BYTES": "max_payload_bytes",
        "FORGE_LENS_MAX_TRACES_PER_REQUEST": "max_traces_per_request",
        "FORGE_LENS_MAX_SPANS_PER_REQUEST": "max_spans_per_request",
        "FORGE_LENS_MAX_EVENTS_PER_SPAN": "max_events_per_span",
        "FORGE_LENS_MAX_ATTR_VALUE_CHARS": "max_attr_value_chars",
        "FORGE_LENS_RATE_LIMIT_PER_MINUTE": "rate_limit_per_minute",
    }
    overrides: dict[str, object] = {}
    for env, field_name in env_map.items():
        raw = os.environ.get(env)
        if raw is None:
            continue
        try:
            if field_name in {"db_path", "pricing_file"}:
                overrides[field_name] = Path(raw).expanduser()
            elif field_name == "capture":
                overrides[field_name] = CaptureLevel(raw)
            elif field_name == "auth_reads":
                overrides[field_name] = raw.lower() in {"1", "true", "yes", "on"}
            elif field_name == "auth_mode":
                if raw not in {"none", "key", "keys"}:
                    raise ValueError(f"invalid auth_mode {raw!r}")
                overrides[field_name] = raw
            elif field_name in {"endpoint", "project_id", "api_key"}:
                overrides[field_name] = raw
            else:  # every remaining mapped field is int-valued
                overrides[field_name] = int(raw)
        except ValueError as exc:
            raise ValueError(f"invalid env {env}={raw!r}: {exc}") from exc
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
        # TOML basic strings share JSON's escape set — json.dumps
        # produces a valid literal for any project_id (quotes, control
        # chars), where raw interpolation would corrupt the file
        cfg.write_text(
            "[lens]\n"
            f"project_id = {json.dumps(project_id)}\n"
            'capture = "metadata"\n'
            'endpoint = "http://127.0.0.1:7777"\n'
            "# api_key = \"change-me\"  # set to require X-Forge-Lens-Key on ingest\n"
            "# auth_reads = true  # also require the key on GET query endpoints\n"
            "# auth_mode = \"keys\"  # managed per-key RBAC + project scope\n"
            "# retention_days = 30  # auto-prune horizon for `archive`/`prune`\n"
            "# rate_limit_per_minute = 600\n",
            encoding="utf-8",
        )
    return cfg
