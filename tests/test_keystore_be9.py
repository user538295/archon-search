"""Tests for BE-9: MCP tools create_key, list_keys, revoke_key, rotate_key.

Covers:
- test_mcp_create_key_returns_token_once — create_key response includes token; no other MCP response does
- test_mcp_list_keys_no_token — list_keys response has no token field
- test_mcp_create_key_no_auth_401 — MCP create_key call with no Bearer token → 401
- test_mcp_revoke_key_invalid_auth_401 — MCP revoke_key with bad token → 401
- test_mcp_create_key_invalid_namespace_error — MCP create_key with invalid namespace → error response (S13)
- test_mcp_revoke_key_then_401 — revoke_key tool call; subsequent Bearer request → 401 (integration)
- test_mcp_rotate_key_returns_new_token — rotate_key MCP tool call returns new token; old key rejected (S6, integration)
"""
from __future__ import annotations

import asyncio
import secrets
import sys
import types
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

pytestmark = pytest.mark.xdist_group("mcp")

# ---------------------------------------------------------------------------
# FastMCP stub — must support tool() decorator so create_app() can register
# tools and we can retrieve the inner functions via _tools dict.
# ---------------------------------------------------------------------------


class _StubFastMCP:
    def __init__(self, *args, **kwargs):
        self._tools: dict = {}

    def tool(self):
        def decorator(fn):
            self._tools[fn.__name__] = fn
            return fn
        return decorator

    def custom_route(self, *args, **kwargs):
        def decorator(fn):
            return fn
        return decorator


_MCP_MODULE = "archon_search.server.mcp"
_FASTMCP_MODULE = "fastmcp"

# Save the real FastMCP class (if available) before we stub it.
try:
    import mcp.server.fastmcp as _mcp_server_fastmcp  # type: ignore[import]
    _real_fastmcp_class = _mcp_server_fastmcp.FastMCP
    _real_fastmcp_context = getattr(_mcp_server_fastmcp, "Context", None)
except (ImportError, AttributeError):
    _real_fastmcp_class = None
    _real_fastmcp_context = None

# Install stub into sys.modules so archon_search.server.mcp can be imported.
if _FASTMCP_MODULE not in sys.modules:
    _fastmcp_mod = types.ModuleType(_FASTMCP_MODULE)
    _fastmcp_mod.FastMCP = _StubFastMCP  # type: ignore[attr-defined]
    _fastmcp_mod.Context = type("Context", (), {})  # type: ignore[attr-defined]
    sys.modules[_FASTMCP_MODULE] = _fastmcp_mod
else:
    sys.modules[_FASTMCP_MODULE].FastMCP = _StubFastMCP  # type: ignore[attr-defined]

# Force-reload the MCP module so it picks up the stub.
sys.modules.pop(_MCP_MODULE, None)

from archon_search.server.mcp import create_app as _mcp_create_app  # noqa: E402

# Restore the real FastMCP so other test modules are not broken.
if _real_fastmcp_class is not None:
    sys.modules[_FASTMCP_MODULE].FastMCP = _real_fastmcp_class  # type: ignore[attr-defined]
    if _real_fastmcp_context is not None:
        sys.modules[_FASTMCP_MODULE].Context = _real_fastmcp_context  # type: ignore[attr-defined]
sys.modules.pop(_MCP_MODULE, None)


@pytest.fixture(autouse=True, scope="module")
def _stub_fastmcp_for_module():
    """Reinstall the stub for the duration of this module's tests, then restore."""
    sys.modules[_FASTMCP_MODULE].FastMCP = _StubFastMCP  # type: ignore[attr-defined]
    sys.modules.pop(_MCP_MODULE, None)
    yield
    if _real_fastmcp_class is not None:
        sys.modules[_FASTMCP_MODULE].FastMCP = _real_fastmcp_class  # type: ignore[attr-defined]
        if _real_fastmcp_context is not None:
            sys.modules[_FASTMCP_MODULE].Context = _real_fastmcp_context  # type: ignore[attr-defined]
    sys.modules.pop(_MCP_MODULE, None)


# ---------------------------------------------------------------------------
# Helper — build a minimal MCP app and return its registered tool closures
# ---------------------------------------------------------------------------


def _get_tools(tmp_path: Path, monkeypatch):
    """Build a minimal create_app() and return (tools_dict, key_store, config)."""
    from archon_search.config import SearchConfig
    from archon_search.key_manager import KeyStore, ENV_VAR
    from archon_search.paths import get_data_dir
    from archon_search.pipeline import SearchPipeline
    from archon_search.server.mcp import create_app

    monkeypatch.setenv("ARCHON_SEARCH_DATA_DIR", str(tmp_path))
    monkeypatch.delenv(ENV_VAR, raising=False)

    cfg = SearchConfig()
    cfg.db_path = str(tmp_path / "db")

    pipeline = MagicMock(spec=SearchPipeline)
    pipeline.store = MagicMock()
    pipeline.store.get_collection_meta = AsyncMock(return_value=None)

    key_store = KeyStore(path=get_data_dir() / "keys.json")

    app = create_app(
        pipeline=pipeline,
        default_collection="default",
        config=cfg,
        key_store=key_store,
    )
    return app._tools, key_store, cfg


# ---------------------------------------------------------------------------
# Helper — build a real HTTP app for auth integration tests
# ---------------------------------------------------------------------------


def _make_http_app(tmp_path: Path, monkeypatch):
    """Build a minimal FastAPI HTTP app for managed key auth checks."""
    from archon_search.config import SearchConfig
    from archon_search.jobs.scheduler import JobScheduler
    from archon_search.jobs.store import JobStore
    from archon_search.key_manager import ENV_VAR
    from archon_search.server.app import create_app as create_http_app

    monkeypatch.setenv("ARCHON_SEARCH_DATA_DIR", str(tmp_path))
    monkeypatch.delenv(ENV_VAR, raising=False)

    cfg = SearchConfig()
    cfg.db_path = str(tmp_path / "db")
    job_store = JobStore(path=tmp_path / "jobs.json")
    scheduler = JobScheduler(
        store=job_store,
        max_concurrent=cfg.jobs.max_concurrent_bulk,
        dispatch_fn=lambda job: None,
    )
    http_app = create_http_app(cfg, job_store, scheduler=scheduler)
    return http_app, cfg


# ---------------------------------------------------------------------------
# Unit: test_mcp_create_key_returns_token_once
# ---------------------------------------------------------------------------


def test_mcp_create_key_returns_token_once(tmp_path, monkeypatch):
    """create_key tool response includes token; no other MCP key response does (S1)."""
    tools, key_store, _ = _get_tools(tmp_path, monkeypatch)
    assert "create_key" in tools

    result = asyncio.run(tools["create_key"](namespace="default", label=None, expires_at=None))
    assert "token" in result
    assert "id" in result
    assert "namespace" in result
    assert result["namespace"] == "default"

    # list_keys and revoke_key responses must NOT have a token field.
    keys_result = asyncio.run(tools["list_keys"](status="active", namespace=None))
    assert "token" not in keys_result
    assert "keys" in keys_result


# ---------------------------------------------------------------------------
# Unit: test_mcp_list_keys_no_token
# ---------------------------------------------------------------------------


def test_mcp_list_keys_no_token(tmp_path, monkeypatch):
    """list_keys response has no token field."""
    tools, key_store, _ = _get_tools(tmp_path, monkeypatch)
    assert "list_keys" in tools

    # Create a key first so there is something to list.
    asyncio.run(key_store.create(ns="default", label=None, expires_at=None))

    result = asyncio.run(tools["list_keys"](status="active", namespace=None))
    assert "token" not in result
    assert "keys" in result
    # Every individual key dict must also lack a token.
    for k in result["keys"]:
        assert "token" not in k


# ---------------------------------------------------------------------------
# Unit: test_mcp_create_key_invalid_namespace_error
# ---------------------------------------------------------------------------


def test_mcp_create_key_invalid_namespace_error(tmp_path, monkeypatch):
    """create_key with invalid namespace returns error response (S13)."""
    tools, _, _ = _get_tools(tmp_path, monkeypatch)

    result = asyncio.run(tools["create_key"](namespace="invalid ns!", label=None, expires_at=None))
    assert "error" in result


# ---------------------------------------------------------------------------
# Unit: test_mcp_create_key_no_auth_401
# These tests exercise the Starlette auth middleware wrapping the FastMCP app.
# They use the real create_mcp_http_app() which needs the real FastMCP — so we
# restore the real FastMCP before creating the app.
# ---------------------------------------------------------------------------


def test_mcp_create_key_no_auth_401(tmp_path, monkeypatch):
    """MCP create_key endpoint with no Bearer token → 401."""
    from archon_search.key_manager import ENV_VAR, get_data_dir
    from archon_search.config import SearchConfig
    from archon_search.key_manager import KeyStore
    from archon_search.pipeline import SearchPipeline

    monkeypatch.setenv("ARCHON_SEARCH_DATA_DIR", str(tmp_path))
    api_key = secrets.token_hex(32)
    # Write api_key to .search.env so load_or_generate_key() returns it.
    monkeypatch.delenv(ENV_VAR, raising=False)
    key_file = get_data_dir() / ".search.env"
    key_file.parent.mkdir(parents=True, exist_ok=True)
    key_file.write_text(f"{ENV_VAR}={api_key}\n")

    cfg = SearchConfig()
    cfg.db_path = str(tmp_path / "db")

    pipeline = MagicMock(spec=SearchPipeline)
    pipeline.store = MagicMock()
    pipeline.store.get_collection_meta = AsyncMock(return_value=None)

    key_store = KeyStore(path=get_data_dir() / "keys.json")

    # Temporarily restore real FastMCP for Starlette HTTP layer.
    if _real_fastmcp_class is not None:
        sys.modules[_FASTMCP_MODULE].FastMCP = _real_fastmcp_class  # type: ignore[attr-defined]
    sys.modules.pop(_MCP_MODULE, None)

    try:
        from archon_search.server.mcp import create_mcp_http_app
        from starlette.testclient import TestClient

        app = create_mcp_http_app(
            pipeline=pipeline,
            default_collection="default",
            config=cfg,
            key_store=key_store,
        )
        client = TestClient(app, raise_server_exceptions=False)
        # No Authorization header → 401 from auth middleware.
        resp = client.get("/mcp")
        assert resp.status_code == 401
    finally:
        # Re-stub for remaining unit tests.
        sys.modules[_FASTMCP_MODULE].FastMCP = _StubFastMCP  # type: ignore[attr-defined]
        sys.modules.pop(_MCP_MODULE, None)


def test_mcp_revoke_key_invalid_auth_401(tmp_path, monkeypatch):
    """MCP revoke_key with bad token → 401 from the Starlette auth middleware."""
    from archon_search.key_manager import ENV_VAR, get_data_dir
    from archon_search.config import SearchConfig
    from archon_search.key_manager import KeyStore
    from archon_search.pipeline import SearchPipeline

    monkeypatch.setenv("ARCHON_SEARCH_DATA_DIR", str(tmp_path))
    api_key = secrets.token_hex(32)
    monkeypatch.delenv(ENV_VAR, raising=False)
    key_file = get_data_dir() / ".search.env"
    key_file.parent.mkdir(parents=True, exist_ok=True)
    key_file.write_text(f"{ENV_VAR}={api_key}\n")

    cfg = SearchConfig()
    cfg.db_path = str(tmp_path / "db")

    pipeline = MagicMock(spec=SearchPipeline)
    pipeline.store = MagicMock()
    pipeline.store.get_collection_meta = AsyncMock(return_value=None)

    key_store = KeyStore(path=get_data_dir() / "keys.json")

    if _real_fastmcp_class is not None:
        sys.modules[_FASTMCP_MODULE].FastMCP = _real_fastmcp_class  # type: ignore[attr-defined]
    sys.modules.pop(_MCP_MODULE, None)

    try:
        from archon_search.server.mcp import create_mcp_http_app
        from starlette.testclient import TestClient

        app = create_mcp_http_app(
            pipeline=pipeline,
            default_collection="default",
            config=cfg,
            key_store=key_store,
        )
        client = TestClient(app, raise_server_exceptions=False)
        # Wrong Bearer token → 401.
        resp = client.get("/mcp", headers={"Authorization": "Bearer wrongtoken"})
        assert resp.status_code == 401
    finally:
        sys.modules[_FASTMCP_MODULE].FastMCP = _StubFastMCP  # type: ignore[attr-defined]
        sys.modules.pop(_MCP_MODULE, None)


# ---------------------------------------------------------------------------
# Integration: test_mcp_revoke_key_then_401
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_mcp_revoke_key_then_401(tmp_path, monkeypatch):
    """revoke_key tool call; subsequent Bearer request with that token → 401 (S4, S9)."""
    http_app, cfg = _make_http_app(tmp_path, monkeypatch)
    admin_key = http_app.state.api_key
    key_store = http_app.state.key_store

    from fastapi.testclient import TestClient
    from archon_search.pipeline import SearchPipeline
    from archon_search.server.mcp import create_app as create_mcp_app

    pipeline = MagicMock(spec=SearchPipeline)
    pipeline.store = MagicMock()
    pipeline.store.get_collection_meta = AsyncMock(return_value=None)

    mcp = create_mcp_app(
        pipeline=pipeline,
        default_collection="default",
        config=cfg,
        key_store=key_store,
    )
    tools = mcp._tools
    assert "revoke_key" in tools

    with TestClient(http_app) as client:
        # Create a managed key via POST /keys.
        resp = client.post(
            "/keys",
            json={"namespace": "default"},
            headers={"Authorization": f"Bearer {admin_key}"},
        )
        assert resp.status_code == 201
        managed_key_id = resp.json()["id"]
        managed_token = resp.json()["token"]

        # Confirm managed key works.
        auth_resp = client.get("/keys", headers={"Authorization": f"Bearer {managed_token}"})
        assert auth_resp.status_code == 200

        # Revoke via MCP tool.
        result = asyncio.run(tools["revoke_key"](key_id=managed_key_id))
        assert result.get("status") == "revoked"

        # After revocation, the managed token must be rejected.
        auth_resp_after = client.get(
            "/keys", headers={"Authorization": f"Bearer {managed_token}"}
        )
        assert auth_resp_after.status_code == 401


# ---------------------------------------------------------------------------
# Integration: test_mcp_rotate_key_returns_new_token
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_mcp_rotate_key_returns_new_token(tmp_path, monkeypatch):
    """rotate_key MCP tool call returns new token; old key revoked on next rotation (S6)."""
    http_app, cfg = _make_http_app(tmp_path, monkeypatch)
    admin_key = http_app.state.api_key
    key_store = http_app.state.key_store

    from fastapi.testclient import TestClient
    from archon_search.key_manager import ENV_VAR, get_key_file
    from archon_search.pipeline import SearchPipeline
    from archon_search.server.mcp import create_app as create_mcp_app

    pipeline = MagicMock(spec=SearchPipeline)
    pipeline.store = MagicMock()
    pipeline.store.get_collection_meta = AsyncMock(return_value=None)

    mcp = create_mcp_app(
        pipeline=pipeline,
        default_collection="default",
        config=cfg,
        key_store=key_store,
    )
    tools = mcp._tools
    assert "rotate_key" in tools

    with TestClient(http_app) as client:
        # First rotation: auto-generated key → creates a managed record.
        result1 = asyncio.run(tools["rotate_key"](grace_seconds=None))
        assert "token" in result1
        assert "new_key_id" in result1
        assert result1.get("status") == "active"
        first_new_token = result1["token"]
        assert first_new_token != admin_key

        # .search.env must be updated.
        key_file = get_key_file()
        content = key_file.read_text()
        assert ENV_VAR in content
        assert first_new_token in content

        # Second rotation with grace_seconds=0 — first managed key is revoked.
        result2 = asyncio.run(tools["rotate_key"](grace_seconds=0))
        assert "token" in result2
        second_new_token = result2["token"]
        assert second_new_token != first_new_token

        # first_new_token should now be revoked in keys.json.
        keys_records = asyncio.run(key_store.list_keys())
        first_record = next(
            (r for r in keys_records if r.id == result1["new_key_id"]), None
        )
        assert first_record is not None
        assert first_record.status == "revoked"


# ---------------------------------------------------------------------------
# Unit: test_mcp_revoke_key_nonexistent_returns_not_found (C1-I-TEST-1)
# ---------------------------------------------------------------------------


def test_mcp_revoke_key_nonexistent_returns_not_found(tmp_path, monkeypatch):
    """revoke_key on a nonexistent key_id returns error with code='not_found'."""
    tools, _, _ = _get_tools(tmp_path, monkeypatch)
    result = asyncio.run(tools["revoke_key"](key_id="does-not-exist"))
    assert "error" in result
    assert result.get("code") == "not_found"


# ---------------------------------------------------------------------------
# Unit: test_mcp_revoke_key_idempotent (C1-I-TEST-2)
# ---------------------------------------------------------------------------


def test_mcp_revoke_key_idempotent(tmp_path, monkeypatch):
    """revoke_key on an already-revoked key returns success (idempotent)."""
    tools, key_store, _ = _get_tools(tmp_path, monkeypatch)
    result_create = asyncio.run(key_store.create(ns="default", label=None, expires_at=None))
    key_id = result_create["id"]
    # First revoke.
    r1 = asyncio.run(tools["revoke_key"](key_id=key_id))
    assert r1.get("status") == "revoked"
    # Second revoke — must be idempotent (no error).
    r2 = asyncio.run(tools["revoke_key"](key_id=key_id))
    assert r2.get("status") == "revoked"
    assert "error" not in r2


# ---------------------------------------------------------------------------
# Integration: test_mcp_rotate_key_grace_sets_expires_at (C1-I-TEST-3)
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_mcp_rotate_key_grace_sets_expires_at(tmp_path, monkeypatch):
    """rotate_key with grace_seconds>0: old key gets expires_at set, not revoked (S15)."""
    http_app, cfg = _make_http_app(tmp_path, monkeypatch)
    key_store = http_app.state.key_store

    from archon_search.pipeline import SearchPipeline
    from archon_search.server.mcp import create_app as create_mcp_app

    pipeline = MagicMock(spec=SearchPipeline)
    pipeline.store = MagicMock()
    pipeline.store.get_collection_meta = AsyncMock(return_value=None)

    mcp = create_mcp_app(
        pipeline=pipeline,
        default_collection="default",
        config=cfg,
        key_store=key_store,
    )
    tools = mcp._tools

    from fastapi.testclient import TestClient

    with TestClient(http_app):
        # First rotation: creates a managed key record from the auto-generated key.
        result1 = asyncio.run(tools["rotate_key"](grace_seconds=None))
        assert "token" in result1
        first_new_id = result1["new_key_id"]

        # Second rotation with grace_seconds=60: old managed key gets expires_at, not revoked.
        result2 = asyncio.run(tools["rotate_key"](grace_seconds=60))
        assert "token" in result2
        assert result2.get("status") == "active"

        # The old managed key (first_new_id) should have expires_at set, status="active".
        keys_records = asyncio.run(key_store.list_keys())
        old_record = next((r for r in keys_records if r.id == first_new_id), None)
        assert old_record is not None
        assert old_record.expires_at is not None, "grace window key must have expires_at set"
        assert old_record.status == "active", "grace window key must remain active until expiry"


# ---------------------------------------------------------------------------
# Unit: test_mcp_rotate_key_write_failure_aborts (C1-I-TEST-4)
# ---------------------------------------------------------------------------


def test_mcp_rotate_key_write_failure_aborts(tmp_path, monkeypatch):
    """rotate_key returns internal_error when .search.env write fails; keys.json unchanged."""
    from unittest.mock import patch

    tools, key_store, _ = _get_tools(tmp_path, monkeypatch)

    # Create an initial key so keys.json has known content.
    asyncio.run(key_store.create(ns="default", label=None, expires_at=None))
    initial_records = asyncio.run(key_store.list_keys())
    initial_count = len(initial_records)

    # Patch asyncio.to_thread to raise OSError on the first call (the .search.env write).
    # The rotate_key tool calls _asyncio.to_thread where _asyncio is the asyncio module
    # bound at block-execution time — patching asyncio.to_thread globally affects it.
    call_count = [0]

    async def _mock_to_thread(func, *args, **kwargs):
        call_count[0] += 1
        if call_count[0] == 1:  # first call is the .search.env write
            raise OSError("disk full")
        import asyncio as _std_asyncio
        return await _std_asyncio.to_thread(func, *args, **kwargs)

    with patch("asyncio.to_thread", _mock_to_thread):
        result = asyncio.run(tools["rotate_key"](grace_seconds=None))

    assert "error" in result
    assert result.get("code") == "internal_error"
    assert "rotation aborted" in result["error"]

    # keys.json must be unchanged (rotate_default_key was never called).
    after_records = asyncio.run(key_store.list_keys())
    assert len(after_records) == initial_count


# ---------------------------------------------------------------------------
# Unit: test_mcp_list_keys_status_revoked_and_all_filters (C1-I-TEST-5)
# ---------------------------------------------------------------------------


def test_mcp_list_keys_status_revoked_and_all_filters(tmp_path, monkeypatch):
    """list_keys returns correct sets for status='revoked' and status='all'."""
    tools, key_store, _ = _get_tools(tmp_path, monkeypatch)

    r1 = asyncio.run(key_store.create(ns="default", label=None, expires_at=None))
    r2 = asyncio.run(key_store.create(ns="default", label=None, expires_at=None))
    asyncio.run(key_store.revoke(r2["id"]))

    # status=revoked → only revoked key, hidden_revoked_count=0.
    result_revoked = asyncio.run(tools["list_keys"](status="revoked", namespace=None))
    assert result_revoked["hidden_revoked_count"] == 0
    assert len(result_revoked["keys"]) == 1
    assert result_revoked["keys"][0]["status"] == "revoked"

    # status=all → both keys, hidden_revoked_count=0.
    result_all = asyncio.run(tools["list_keys"](status="all", namespace=None))
    assert result_all["hidden_revoked_count"] == 0
    assert len(result_all["keys"]) == 2


# ---------------------------------------------------------------------------
# Unit: test_mcp_list_keys_namespace_filter (C1-I-TEST-6)
# ---------------------------------------------------------------------------


def test_mcp_list_keys_namespace_filter(tmp_path, monkeypatch):
    """list_keys with namespace filter returns only that namespace's keys."""
    tools, key_store, _ = _get_tools(tmp_path, monkeypatch)

    asyncio.run(key_store.create(ns="ns-a", label=None, expires_at=None))
    asyncio.run(key_store.create(ns="ns-b", label=None, expires_at=None))

    result = asyncio.run(tools["list_keys"](status="active", namespace="ns-a"))
    assert len(result["keys"]) == 1
    assert result["keys"][0]["namespace"] == "ns-a"


# ---------------------------------------------------------------------------
# Unit: test_mcp_rotate_key_negative_grace_returns_error (C1-I-TEST-8)
# ---------------------------------------------------------------------------


def test_mcp_rotate_key_negative_grace_returns_error(tmp_path, monkeypatch):
    """rotate_key with grace_seconds < 0 returns a validation_error."""
    tools, _, _ = _get_tools(tmp_path, monkeypatch)
    result = asyncio.run(tools["rotate_key"](grace_seconds=-1))
    assert "error" in result
    assert result.get("code") == "validation_error"


# ---------------------------------------------------------------------------
# Unit: test_mcp_revoke_key_null_id_returns_toml_hint (C1-I-TEST-9)
# ---------------------------------------------------------------------------


def test_mcp_revoke_key_null_id_returns_toml_hint(tmp_path, monkeypatch):
    """revoke_key with key_id='null' returns helpful TOML hint error."""
    tools, _, _ = _get_tools(tmp_path, monkeypatch)
    result = asyncio.run(tools["revoke_key"](key_id="null"))
    assert "error" in result
    assert result.get("code") == "not_found"
    assert "archon-search.toml" in result["error"]


# ---------------------------------------------------------------------------
# Unit: test_mcp_key_tools_not_registered_when_no_key_store (C1-I-TEST-10)
# ---------------------------------------------------------------------------


def test_mcp_key_tools_not_registered_when_no_key_store(tmp_path, monkeypatch):
    """When key_store=None, none of the four key-management tools are registered."""
    from archon_search.config import SearchConfig
    from archon_search.pipeline import SearchPipeline
    from archon_search.key_manager import ENV_VAR
    from archon_search.server.mcp import create_app

    monkeypatch.setenv("ARCHON_SEARCH_DATA_DIR", str(tmp_path))
    monkeypatch.delenv(ENV_VAR, raising=False)

    cfg = SearchConfig()
    pipeline = MagicMock(spec=SearchPipeline)
    pipeline.store = MagicMock()
    pipeline.store.get_collection_meta = AsyncMock(return_value=None)

    # key_store=None → tools must not be registered.
    app = create_app(pipeline=pipeline, default_collection="default", config=cfg)
    tools = app._tools
    assert "create_key" not in tools
    assert "list_keys" not in tools
    assert "revoke_key" not in tools
    assert "rotate_key" not in tools


# ---------------------------------------------------------------------------
# Unit: test_mcp_rotate_key_env_var_set_returns_conflict (C1-I-ARCH-2)
# ---------------------------------------------------------------------------


def test_mcp_rotate_key_env_var_set_returns_conflict(tmp_path, monkeypatch):
    """rotate_key returns conflict error when ARCHON_SEARCH_API_KEY env var is set (S23 parity)."""
    from archon_search.key_manager import ENV_VAR

    # Set the env var — this is what triggers the guard.
    monkeypatch.setenv(ENV_VAR, "some-api-key-value")
    # Also set DATA_DIR for key_store path resolution.
    monkeypatch.setenv("ARCHON_SEARCH_DATA_DIR", str(tmp_path))

    from archon_search.config import SearchConfig
    from archon_search.pipeline import SearchPipeline
    from archon_search.key_manager import KeyStore
    from archon_search.paths import get_data_dir
    from archon_search.server.mcp import create_app

    cfg = SearchConfig()
    pipeline = MagicMock(spec=SearchPipeline)
    pipeline.store = MagicMock()
    pipeline.store.get_collection_meta = AsyncMock(return_value=None)

    key_store = KeyStore(path=get_data_dir() / "keys.json")
    app = create_app(pipeline=pipeline, default_collection="default", config=cfg, key_store=key_store)
    tools = app._tools

    result = asyncio.run(tools["rotate_key"](grace_seconds=None))
    assert "error" in result
    assert result.get("code") == "conflict"
    assert "ARCHON_SEARCH_API_KEY" in result["error"]


# ---------------------------------------------------------------------------
# Unit: T1 — create_key with valid ISO-8601 expires_at
# ---------------------------------------------------------------------------


def test_mcp_create_key_valid_expires_at(tmp_path, monkeypatch):
    """create_key with a valid timezone-aware ISO-8601 expires_at succeeds."""
    tools, _, _ = _get_tools(tmp_path, monkeypatch)
    result = asyncio.run(tools["create_key"](namespace="default", expires_at="2030-01-01T00:00:00Z"))
    assert "token" in result, result
    assert result["expires_at"] is not None


# ---------------------------------------------------------------------------
# Unit: T2 — create_key with invalid expires_at format
# ---------------------------------------------------------------------------


def test_mcp_create_key_invalid_expires_at_format(tmp_path, monkeypatch):
    """create_key with an unparseable expires_at returns validation_error."""
    tools, _, _ = _get_tools(tmp_path, monkeypatch)
    result = asyncio.run(tools["create_key"](namespace="default", expires_at="not-a-date"))
    assert result.get("code") == "validation_error"


# ---------------------------------------------------------------------------
# Unit: T3 — create_key with timezone-naive expires_at
# ---------------------------------------------------------------------------


def test_mcp_create_key_timezone_naive_expires_at(tmp_path, monkeypatch):
    """create_key with a timezone-naive expires_at returns validation_error."""
    tools, _, _ = _get_tools(tmp_path, monkeypatch)
    result = asyncio.run(tools["create_key"](namespace="default", expires_at="2030-01-01T00:00:00"))
    assert result.get("code") == "validation_error"
    assert "timezone" in result["error"]


# ---------------------------------------------------------------------------
# Unit: T4 — list_keys with invalid status parameter
# ---------------------------------------------------------------------------


def test_mcp_list_keys_invalid_status(tmp_path, monkeypatch):
    """list_keys with an unrecognised status value returns validation_error."""
    tools, _, _ = _get_tools(tmp_path, monkeypatch)
    result = asyncio.run(tools["list_keys"](status="invalid"))
    assert result.get("code") == "validation_error"


# ---------------------------------------------------------------------------
# Unit: T5 — create_key exception path (internal_error, no message leakage)
# ---------------------------------------------------------------------------


def test_mcp_create_key_exception_path(tmp_path, monkeypatch):
    """create_key internal exception returns internal_error and does not leak exc message."""
    tools, key_store, _ = _get_tools(tmp_path, monkeypatch)

    async def _raise(*args, **kwargs):
        raise Exception("boom internal detail")

    key_store.create = _raise

    result = asyncio.run(tools["create_key"](namespace="default"))
    assert result.get("code") == "internal_error"
    assert "boom" not in result.get("error", ""), "exception detail must not leak"
    assert result["error"] == "Failed to create key"


# ---------------------------------------------------------------------------
# Unit: T6 — rotate_key ValueError from rotate_default_key
# ---------------------------------------------------------------------------


def test_mcp_rotate_key_valueerror_from_rotate_default(tmp_path, monkeypatch):
    """rotate_key returns validation_error when rotate_default_key raises ValueError."""
    tools, key_store, _ = _get_tools(tmp_path, monkeypatch)

    async def _raise(*args, **kwargs):
        raise ValueError("bad input from store")

    key_store.rotate_default_key = _raise

    result = asyncio.run(tools["rotate_key"](grace_seconds=None))
    assert result.get("code") == "validation_error"
    assert result["error"] == "bad input from store"


# ---------------------------------------------------------------------------
# Unit: T7 — rotate_key grace period: expires_at is approximately correct
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_mcp_rotate_key_grace_expires_at_timestamp(tmp_path, monkeypatch):
    """rotate_key with grace_seconds=60: old key's expires_at is within 10s of now+60s."""
    from datetime import datetime, timedelta, timezone

    http_app, cfg = _make_http_app(tmp_path, monkeypatch)
    key_store = http_app.state.key_store

    from archon_search.pipeline import SearchPipeline
    from archon_search.server.mcp import create_app as create_mcp_app

    pipeline = MagicMock(spec=SearchPipeline)
    pipeline.store = MagicMock()
    pipeline.store.get_collection_meta = AsyncMock(return_value=None)

    mcp = create_mcp_app(
        pipeline=pipeline,
        default_collection="default",
        config=cfg,
        key_store=key_store,
    )
    tools = mcp._tools

    from fastapi.testclient import TestClient

    with TestClient(http_app):
        # First rotation — create a managed key from the auto-generated default.
        result1 = asyncio.run(tools["rotate_key"](grace_seconds=None))
        first_new_id = result1["new_key_id"]

        before = datetime.now(timezone.utc)
        # Second rotation with grace — first managed key gets expires_at.
        asyncio.run(tools["rotate_key"](grace_seconds=60))
        after = datetime.now(timezone.utc)

        keys_records = asyncio.run(key_store.list_keys())
        old_record = next((r for r in keys_records if r.id == first_new_id), None)
        assert old_record is not None
        assert old_record.expires_at is not None

        expected_min = before + timedelta(seconds=60)
        expected_max = after + timedelta(seconds=60)
        assert expected_min <= old_record.expires_at <= expected_max, (
            f"expires_at {old_record.expires_at} not in [{expected_min}, {expected_max}]"
        )


# ---------------------------------------------------------------------------
# Unit: T8 — create_key label passes through to response
# ---------------------------------------------------------------------------


def test_mcp_create_key_label_passthrough(tmp_path, monkeypatch):
    """create_key with label='my-label' returns that label in the response."""
    tools, _, _ = _get_tools(tmp_path, monkeypatch)
    result = asyncio.run(tools["create_key"](namespace="default", label="my-label"))
    assert "token" in result, result
    assert result.get("label") == "my-label"
