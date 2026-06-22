"""Tests for BE-3: Wire KeyStore into app.py and mcp.py.

Covers:
- app.state.key_store is a KeyStore instance after create_app()
- TOML [namespaces] tokens loaded as synthetic KeyRecord objects at startup
- TOML namespace tokens accepted on a real TestClient (S7)
- Both TOML and managed key tokens accepted simultaneously (S8)
"""
from __future__ import annotations

import hashlib
import secrets

import pytest
from fastapi.testclient import TestClient

from archon_search.key_manager import KeyRecord, KeyStore


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_minimal_app(tmp_path, monkeypatch, *, namespaces: dict[str, str] | None = None):
    """Return a FastAPI TestClient using the real create_app with a minimal config."""
    import secrets as _secrets

    from archon_search.config import SearchConfig
    from archon_search.jobs.scheduler import JobScheduler
    from archon_search.jobs.store import JobStore
    from archon_search.server.app import create_app

    api_key = _secrets.token_hex(32)
    monkeypatch.setenv("ARCHON_SEARCH_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("ARCHON_SEARCH_API_KEY", api_key)

    cfg = SearchConfig()
    cfg.db_path = str(tmp_path / "db")

    if namespaces is not None:
        cfg.namespaces = namespaces

    job_store = JobStore(path=tmp_path / "jobs.json")
    scheduler = JobScheduler(
        store=job_store,
        max_concurrent=cfg.jobs.max_concurrent_bulk,
        dispatch_fn=lambda job: None,
    )
    app = create_app(cfg, job_store, scheduler=scheduler)
    return app, api_key


# ---------------------------------------------------------------------------
# Unit tests
# ---------------------------------------------------------------------------


def test_create_app_exposes_key_store(tmp_path, monkeypatch):
    """app.state.key_store is a KeyStore instance after create_app()."""
    app, api_key = _make_minimal_app(tmp_path, monkeypatch)
    with TestClient(app) as client:
        assert hasattr(app.state, "key_store"), "app.state.key_store not set by create_app()"
        assert isinstance(app.state.key_store, KeyStore), (
            f"app.state.key_store is {type(app.state.key_store)}, expected KeyStore"
        )


def test_toml_synthetic_key_records_loaded_at_startup(tmp_path, monkeypatch):
    """After create_app(), TOML namespace tokens are present as synthetic KeyRecord objects."""
    toml_token = secrets.token_hex(32)
    namespaces = {toml_token: "toml-ns"}

    app, api_key = _make_minimal_app(tmp_path, monkeypatch, namespaces=namespaces)
    with TestClient(app) as client:
        key_store = app.state.key_store
        import asyncio
        records = asyncio.run(key_store.load())

        # Must have at least one synthetic record matching the TOML token
        token_hash = hashlib.sha256(toml_token.encode()).hexdigest()
        synthetic = [r for r in records if r.token_hash == token_hash]
        assert len(synthetic) == 1, (
            f"Expected exactly 1 synthetic record for TOML token, got {len(synthetic)}"
        )
        rec = synthetic[0]
        assert rec.namespace == "toml-ns"
        assert rec.label == "toml-ns", (
            f"Synthetic record label should mirror namespace name, got {rec.label!r}"
        )
        assert rec.status == "active"
        assert rec.id is None, (
            f"TOML synthetic records should have id=None, got {rec.id!r}"
        )


# ---------------------------------------------------------------------------
# Integration tests
# ---------------------------------------------------------------------------


def test_toml_namespaces_still_work(tmp_path, monkeypatch):
    """TOML [namespaces] token accepted on a real TestClient (S7).

    Existing TOML-based namespace auth continues to work after BE-3 wiring.
    No restart required — token resolved at startup.
    """
    toml_token = secrets.token_hex(32)
    namespaces = {toml_token: "toml-ns"}

    app, api_key = _make_minimal_app(tmp_path, monkeypatch, namespaces=namespaces)
    with TestClient(app) as client:
        resp = client.get(
            "/collections",
            headers={"Authorization": f"Bearer {toml_token}"},
        )
        assert resp.status_code == 200, (
            f"TOML namespace token rejected: {resp.status_code} {resp.text}"
        )


def test_toml_and_managed_key_coexist(tmp_path, monkeypatch):
    """Both TOML and managed key tokens accepted simultaneously (S8).

    After creating a managed key alongside a TOML namespace config,
    both tokens must authenticate successfully.
    """
    toml_token = secrets.token_hex(32)
    namespaces = {toml_token: "toml-ns"}

    app, api_key = _make_minimal_app(tmp_path, monkeypatch, namespaces=namespaces)
    with TestClient(app) as client:
        # First: TOML token works
        resp_toml = client.get(
            "/collections",
            headers={"Authorization": f"Bearer {toml_token}"},
        )
        assert resp_toml.status_code == 200, (
            f"TOML token rejected: {resp_toml.status_code} {resp_toml.text}"
        )

        # Second: create a managed key (the POST /keys endpoint is added in BE-4;
        # use KeyStore directly to create a managed key without the route)
        import asyncio

        key_store = app.state.key_store
        result = asyncio.run(key_store.create(ns="managed-ns", label=None, expires_at=None))
        managed_token = result["token"]

        # Managed key must also be accepted
        resp_managed = client.get(
            "/collections",
            headers={"Authorization": f"Bearer {managed_token}"},
        )
        assert resp_managed.status_code == 200, (
            f"Managed key rejected: {resp_managed.status_code} {resp_managed.text}"
        )

        # TOML token must still work after managed key creation
        resp_toml2 = client.get(
            "/collections",
            headers={"Authorization": f"Bearer {toml_token}"},
        )
        assert resp_toml2.status_code == 200, (
            f"TOML token rejected after managed key creation: {resp_toml2.status_code} {resp_toml2.text}"
        )


@pytest.mark.xdist_group("mcp")
def test_create_mcp_http_app_accepts_managed_key(tmp_path, monkeypatch):
    """create_mcp_http_app() key_store param wires managed-key auth correctly.

    A key created in the KeyStore and passed to create_mcp_http_app() must be
    accepted by the MCP auth middleware. This verifies the BE-3 wiring in mcp.py.
    """
    import asyncio
    import sys
    from unittest.mock import AsyncMock, MagicMock, patch

    from archon_search.key_manager import KeyStore
    from archon_search.paths import get_data_dir

    monkeypatch.setenv("ARCHON_SEARCH_DATA_DIR", str(tmp_path))

    # Ensure fastmcp is available in sys.modules (matches test_mcp_auth.py pattern)
    if "fastmcp" not in sys.modules:
        import mcp.server.fastmcp as _real_fastmcp
        sys.modules["fastmcp"] = _real_fastmcp  # type: ignore[assignment]

    # Create a KeyStore and populate it with a managed key
    key_store = KeyStore(get_data_dir() / "keys.json")
    result = asyncio.run(key_store.create(ns="managed-ns", label=None, expires_at=None))
    managed_token = result["token"]

    # Build a minimal pipeline mock
    from archon_search.pipeline import SearchPipelineResult
    from archon_search.embedder_cache import EmbedderCache

    pipeline = MagicMock()
    pipeline.search = AsyncMock(return_value=SearchPipelineResult(results=[], acl_filtered=False))

    # Build the MCP Starlette app with key_store wired in
    legacy_key = "a" * 64  # 64-char hex legacy key
    with patch("archon_search.server.mcp.load_or_generate_key", return_value=(legacy_key, "test")):
        from archon_search.server import mcp as mcp_module
        starlette_app = mcp_module.create_mcp_http_app(
            pipeline,
            "default",
            embedder_cache=EmbedderCache(max_size=3),
            key_store=key_store,
        )

    from starlette.testclient import TestClient as StarletteTestClient
    client = StarletteTestClient(starlette_app, raise_server_exceptions=False)

    # Managed key must be accepted (non-401)
    resp = client.post(
        "/mcp",
        headers={"Authorization": f"Bearer {managed_token}"},
        json={},
    )
    assert resp.status_code != 401, (
        f"Managed key rejected by MCP middleware: {resp.status_code} {resp.text}"
    )

    # Legacy key must still work (backward compat)
    resp_legacy = client.post(
        "/mcp",
        headers={"Authorization": f"Bearer {legacy_key}"},
        json={},
    )
    assert resp_legacy.status_code != 401, (
        f"Legacy key rejected by MCP middleware: {resp_legacy.status_code} {resp_legacy.text}"
    )
