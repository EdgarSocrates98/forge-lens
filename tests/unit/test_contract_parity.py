"""Contract & surface-parity checks — mechanical drift guards.

Keeps docs honest without hand-audits:

- every OpenAPI path emitted by ``create_app().openapi()`` is named in
  ``docs/reference/api.md``;
- every ``@app.<method>`` route literal in ``api/app.py`` is documented
  there too, and the writer-tier summary covers every
  ``Depends(check_auth)`` route;
- every MCP tool in ``mcp_server._tools()`` is listed in
  ``docs/reference/mcp.md``;
- every public ``LensClient`` method (Python and TS) is named in its
  SDK reference doc;
- the error-envelope contract holds end-to-end: authored rejections
  carry ``{"detail": str}``, framework validation carries
  ``{"detail": […]}``, and ``413`` fires on an oversized body.

Reading doc files is the point — these tests fail when a route, tool
or client method ships without documentation.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
APP_PY = REPO / "src" / "forge_lens" / "api" / "app.py"
MCP_PY = REPO / "src" / "forge_lens" / "mcp_server.py"
QUERY_PY = REPO / "src" / "forge_lens" / "sdk" / "query.py"
QUERY_TS = REPO / "packages" / "forge-lens-sdk-ts" / "src" / "query.ts"
API_MD = REPO / "docs" / "reference" / "api.md"
MCP_MD = REPO / "docs" / "reference" / "mcp.md"
SDK_PY_MD = REPO / "docs" / "reference" / "sdk-python.md"
SDK_TS_MD = REPO / "docs" / "reference" / "sdk-typescript.md"

pytestmark = pytest.mark.skipif(
    not API_MD.exists(), reason="docs/reference/api.md ausente"
)


def _route_literals() -> list[tuple[str, str]]:
    """``(method, path)`` for every ``@app.<verb>`` decorator in app.py —
    f-string escapes resolved (``{API_VERSION}`` → ``v1``, ``{{`` → ``{``)."""
    src = APP_PY.read_text(encoding="utf-8")
    out: list[tuple[str, str]] = []
    for m in re.finditer(
        r'@app\.(get|post|put|delete|patch)\(\s*f?["\']([^"\']+)["\']',
        src,
    ):
        path = (
            m.group(2)
            .replace("{API_VERSION}", "v1")
            .replace("{{", "{")
            .replace("}}", "}")
        )
        out.append((m.group(1).upper(), path))
    return out


def _norm(value: str) -> str:
    """``{param}`` segments → ``{}`` so doc placeholders normalize."""
    return re.sub(r"\{[^}]*\}", "{}", value)


def _documented(method: str, route: str) -> bool:
    """A route counts as documented when ``<METHOD> <path>`` appears in
    api.md with any ``{param}`` spelling."""
    text = _norm(API_MD.read_text(encoding="utf-8"))
    return f"{method} {_norm(route)}" in text


def test_every_route_literal_is_documented() -> None:
    routes = _route_literals()
    assert len(routes) >= 50, f"sanity: expected ≥50 routes, parsed {len(routes)}"
    missing = [f"{v} {p}" for v, p in routes if not _documented(v, p)]
    assert not missing, f"routes missing from api.md: {missing}"


def test_openapi_paths_match_route_literals() -> None:
    """The generated schema must expose exactly the decorated routes —
    no more (undocumented surface), no less (dead docs)."""
    pytest.importorskip("fastapi")
    from forge_lens.api.app import create_app
    from forge_lens.core.config import LensConfig

    app = create_app(
        config=LensConfig(
            data_dir=Path("/nonexistent"), db_path=Path("/nonexistent/x.db")
        )
    )
    spec_paths = {
        f"{verb.upper()} {path}"
        for path, ops in app.openapi()["paths"].items()
        for verb in ops
        if verb in ("get", "post", "put", "delete", "patch")
    }
    literal_paths = {
        f"{v} {p}" for v, p in _route_literals() if p != "/favicon.ico"
    }
    assert spec_paths == literal_paths


def test_openapi_paths_are_documented() -> None:
    pytest.importorskip("fastapi")
    from forge_lens.api.app import create_app
    from forge_lens.core.config import LensConfig

    app = create_app(
        config=LensConfig(
            data_dir=Path("/nonexistent"), db_path=Path("/nonexistent/x.db")
        )
    )
    missing = [
        f"{verb.upper()} {path}"
        for path, ops in app.openapi()["paths"].items()
        for verb in ops
        if verb in ("get", "post", "put", "delete", "patch")
        and not _documented(verb.upper(), path)
    ]
    assert not missing, f"openapi paths missing from api.md: {missing}"


def test_writer_tier_doc_covers_check_auth_routes() -> None:
    """Every route guarded by ``Depends(check_auth)`` must appear in the
    api.md writer bullet — the auth table is the contract operators read."""
    src = APP_PY.read_text(encoding="utf-8")
    text = API_MD.read_text(encoding="utf-8")
    writer_bullet = re.search(
        r"\*\*writer\*\*.*?(?=\n- \*\*reader\*\*)", text, re.DOTALL
    )
    assert writer_bullet is not None
    bullet = writer_bullet.group(0)
    # find each decorated block that binds check_auth
    for m in re.finditer(
        r'@app\.(post|put|delete)\((.*?)\)\s*\n\s*async def (\w+)',
        src,
        re.DOTALL,
    ):
        args = m.group(2)
        if "check_auth" not in args:
            continue
        path = re.search(r'f"([^"]+)"', args)
        assert path is not None, m.group(3)
        route = path.group(1).replace("{API_VERSION}", "v1")
        # writer bullet documents globs: /v1/datasets/* covers the two PUTs
        stem = re.sub(r"/\{[^}]*\}.*", "", route)
        assert stem in bullet, f"{route} not covered by writer-tier doc"


def test_mcp_tools_are_documented() -> None:
    from forge_lens.mcp_server import _tools

    text = MCP_MD.read_text(encoding="utf-8")
    missing = [t["name"] for t in _tools() if t["name"] not in text]
    assert not missing, f"MCP tools missing from mcp.md: {missing}"


def test_python_sdk_methods_documented() -> None:
    src = QUERY_PY.read_text(encoding="utf-8")
    methods = re.findall(r"^    def ([a-z_]+)\(self", src, re.MULTILINE)
    public = [m for m in methods if not m.startswith("_") and m != "from_config"]
    text = SDK_PY_MD.read_text(encoding="utf-8")
    missing = [m for m in public if m not in text]
    assert not missing, f"LensClient methods missing from sdk-python.md: {missing}"


def test_ts_sdk_methods_documented() -> None:
    src = QUERY_TS.read_text(encoding="utf-8")
    methods = re.findall(r"^  ([a-zA-Z]+)\(", src, re.MULTILINE)
    private = {"request", "get", "post", "url", "pid", "constructor"}
    public = [m for m in methods if m not in private]
    text = SDK_TS_MD.read_text(encoding="utf-8")
    missing = [m for m in public if m not in text]
    assert not missing, f"LensClient methods missing from sdk-typescript.md: {missing}"


# -- error-envelope contract (end-to-end through the ASGI app) -------------


@pytest.fixture
def api_client(tmp_path: Path):
    pytest.importorskip("fastapi")
    pytest.importorskip("httpx")
    from fastapi.testclient import TestClient

    from forge_lens.api.app import create_app
    from forge_lens.core.config import LensConfig
    from forge_lens.storage.sqlite import SQLiteTraceStore

    store = SQLiteTraceStore(tmp_path / "parity.db")
    cfg = LensConfig(data_dir=tmp_path, db_path=tmp_path / "parity.db")
    with TestClient(create_app(store=store, config=cfg)) as client:
        yield client
    store.close()


def test_error_envelope_authored_vs_framework(api_client) -> None:
    """Route-authored rejections → ``{"detail": str}``; a query-param the
    framework can't parse → ``{"detail": [violation, …]}``."""
    r = api_client.get("/v1/traces", params={"status": "bogus"})
    assert r.status_code == 422
    assert isinstance(r.json()["detail"], str)

    r = api_client.get("/v1/traces", params={"limit": "abc"})
    assert r.status_code == 422
    assert isinstance(r.json()["detail"], list)
    assert r.json()["detail"][0]["loc"] == ["query", "limit"]

    r = api_client.get(f"/v1/traces/{'f' * 32}")
    assert r.status_code == 404
    assert isinstance(r.json()["detail"], str)


def test_body_bounds_envelope(api_client) -> None:
    """Malformed JSON → 400 (not 422); depth > 512 → 400; oversized → 413 —
    all through ``{"detail": str}``."""
    r = api_client.post(
        "/v1/traces",
        content=b"{not json",
        headers={"Content-Type": "application/json"},
    )
    assert r.status_code == 400
    assert isinstance(r.json()["detail"], str)

    deep = b"[" * 513 + b"]" * 513
    r = api_client.post("/v1/eval/run", content=deep)
    assert r.status_code == 400
    assert isinstance(r.json()["detail"], str)

    r = api_client.post(
        "/v1/traces",
        content=b"{}",
        headers={
            "Content-Type": "application/json",
            "Content-Length": str(9 * 1024 * 1024),
        },
    )
    assert r.status_code == 413
    assert isinstance(r.json()["detail"], str)
