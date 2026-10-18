"""REST ``POST /keys/rotate`` and MCP ``rotate_key`` agree on the server's current default key.

Both transports read the current key from, and publish the new key to, ``app.state.api_key``
under the shared ``KeyStore.rotation_lock``; both auth middlewares accept that same key on
the legacy path. So whichever transport rotates next retires the key the other just issued.
"""
from __future__ import annotations

import asyncio
import hashlib
import secrets
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from starlette.applications import Starlette
from starlette.routing import Mount
from starlette.testclient import TestClient

from archon_search.key_manager import ENV_VAR, ROTATE_WRITE_FAILED_DETAIL, KeyStore, get_key_file
from archon_search.paths import get_data_dir
from tests.integration.conftest import make_real_app, mcp_headers, mcp_initialize, mcp_tool_call

pytestmark = [pytest.mark.integration, pytest.mark.xdist_group("mcp")]

_UNAUTHORIZED = 401
_OK = 200
_GRACE_SECONDS = 3600


def _sha256(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _rest_rotate(client, token: str) -> str:
    resp = client.post("/keys/rotate", json={"grace_seconds": 0}, headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == _OK, resp.text
    return resp.json()["token"]


def _mcp_rotate(client, token: str) -> str:
    session_id = mcp_initialize(client, token)
    return mcp_tool_call(client, token, session_id, "rotate_key", {"grace_seconds": 0})["token"]


def _mcp_initialize_status(client, token: str) -> int:
    resp = client.post(
        "/mcp",
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {"protocolVersion": "2024-11-05", "capabilities": {}, "clientInfo": {"name": "t", "version": "1"}},
        },
        headers=mcp_headers(token),
    )
    return resp.status_code


def _assert_only_accepted(client, current: str, retired: list[str]) -> None:
    assert client.get("/keys", headers={"Authorization": f"Bearer {current}"}).status_code == _OK
    assert _mcp_initialize_status(client, current) == _OK
    for index, token in enumerate(retired):
        rest = client.get("/keys", headers={"Authorization": f"Bearer {token}"}).status_code
        assert rest == _UNAUTHORIZED, f"REST still accepts retired key #{index}"
        assert _mcp_initialize_status(client, token) == _UNAUTHORIZED, f"MCP still accepts retired key #{index}"


def test_rest_rotation_after_an_mcp_rotation_retires_the_mcp_issued_key(tmp_path, monkeypatch) -> None:
    with make_real_app(tmp_path, monkeypatch, mcp_enabled=True) as (client, _cfg, startup_key):
        monkeypatch.delenv(ENV_VAR)  # rotation answers 409 while the env var is set
        mcp_key = _mcp_rotate(client, startup_key)
        assert client.app.state.api_key == mcp_key, "MCP must publish its new key like REST does"

        rest_key = _rest_rotate(client, mcp_key)

        assert client.app.state.api_key == rest_key
        _assert_only_accepted(client, rest_key, retired=[startup_key, mcp_key])


def test_mcp_rotation_after_a_rest_rotation_retires_the_rest_issued_key(tmp_path, monkeypatch) -> None:
    with make_real_app(tmp_path, monkeypatch, mcp_enabled=True) as (client, _cfg, startup_key):
        monkeypatch.delenv(ENV_VAR)
        rest_key = _rest_rotate(client, startup_key)

        mcp_key = _mcp_rotate(client, rest_key)

        assert client.app.state.api_key == mcp_key
        _assert_only_accepted(client, mcp_key, retired=[startup_key, rest_key])


def test_the_mounted_mcp_app_shares_the_rest_key_store_and_key_state(tmp_path, monkeypatch) -> None:
    """One KeyStore object (so one rotation_lock) and one current-key holder span both transports."""
    from archon_search.server.mcp import create_mcp_http_app

    # String target: resolved from sys.modules when the patch starts, so it hits the module
    # the lifespan imports even after another test dropped archon_search.server.mcp.
    with patch("archon_search.server.mcp.create_mcp_http_app", wraps=create_mcp_http_app) as factory:
        with make_real_app(tmp_path, monkeypatch, mcp_enabled=True) as (client, _cfg, _key):
            app_state = client.app.state

    factory.assert_called_once()
    kwargs = factory.call_args.kwargs
    assert kwargs["key_store"] is app_state.key_store
    assert kwargs["api_key_state"] is app_state


def test_a_mounted_mcp_rotation_retires_the_server_key_not_the_key_file_content(tmp_path, monkeypatch) -> None:
    """The key MCP retires is app.state.api_key: a different token in .search.env is ignored."""
    decoy = secrets.token_hex(32)
    with make_real_app(tmp_path, monkeypatch, mcp_enabled=True) as (client, _cfg, startup_key):
        monkeypatch.delenv(ENV_VAR)
        get_key_file().write_text(f"{ENV_VAR}={decoy}\n")
        session_id = mcp_initialize(client, startup_key)

        result = mcp_tool_call(client, startup_key, session_id, "rotate_key", {"grace_seconds": _GRACE_SECONDS})

        records = asyncio.run(client.app.state.key_store.load())
        hashes = {record.token_hash for record in records}
        grace = [r for r in records if r.token_hash == _sha256(startup_key)]
        assert len(grace) == 1 and grace[0].status == "active" and grace[0].expires_at is not None
        assert _sha256(decoy) not in hashes, "the rotation must not read the current key from .search.env"
        assert client.app.state.api_key == result["token"]


def test_a_failed_mounted_mcp_rotation_leaves_the_server_key_unchanged(tmp_path, monkeypatch) -> None:
    with make_real_app(tmp_path, monkeypatch, mcp_enabled=True) as (client, _cfg, startup_key):
        monkeypatch.delenv(ENV_VAR)
        session_id = mcp_initialize(client, startup_key)

        with patch("archon_search.key_manager.atomic_write_bytes", side_effect=PermissionError(13, "Permission denied")):
            result = mcp_tool_call(client, startup_key, session_id, "rotate_key", {"grace_seconds": 0})

        assert result == {"error": ROTATE_WRITE_FAILED_DETAIL, "code": "internal_error"}
        assert client.app.state.api_key == startup_key
        _assert_only_accepted(client, startup_key, retired=[])


def test_a_standalone_mcp_rotation_retires_the_old_key_and_accepts_the_new_one(tmp_path, monkeypatch) -> None:
    """Unmounted create_mcp_http_app: its private key state is what its auth and rotate_key share."""
    from archon_search.server.mcp import create_mcp_http_app

    old_key = secrets.token_hex(32)
    monkeypatch.delenv(ENV_VAR, raising=False)
    # Own data dir: .search.env and keys.json must not land in the shared per-worker one.
    monkeypatch.setenv("ARCHON_SEARCH_DATA_DIR", str(tmp_path))
    get_key_file().write_text(f"{ENV_VAR}={old_key}\n")
    pipeline = MagicMock()
    pipeline.store.get_collection_meta = AsyncMock(return_value=None)
    mcp_app = create_mcp_http_app(
        pipeline=pipeline,
        default_collection="default",
        key_store=KeyStore(get_data_dir() / "keys.json"),
    )
    assert get_key_file().parent == tmp_path
    # Serve it at /mcp, as when mounted, so the shared MCP helpers apply; run its lifespan.
    outer = Starlette(
        routes=[Mount("/mcp", app=mcp_app)],
        lifespan=lambda _app: mcp_app.router.lifespan_context(mcp_app),
    )

    with TestClient(outer) as client:
        new_key = _mcp_rotate(client, old_key)

        assert new_key != old_key
        assert _mcp_initialize_status(client, new_key) == _OK
        assert _mcp_initialize_status(client, old_key) == _UNAUTHORIZED
