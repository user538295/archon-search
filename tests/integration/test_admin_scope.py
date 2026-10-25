"""Admin scope (SecReview #1): key management and server-wide control endpoints need an admin key.

Drives the real ASGI app (REST + the mounted MCP app) with three kinds of credential:

* the default key — admin;
* a managed key created through ``POST /keys`` without ``admin`` — non-admin;
* a TOML ``[namespaces]`` token — non-admin.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import secrets
from datetime import UTC, datetime
from pathlib import Path

import pytest

from archon_search.key_manager import ENV_VAR
from archon_search.server.authz import (
    ADMIN_KEY_GRANT_DETAIL,
    ADMIN_REQUIRED_DETAIL,
    FOREIGN_NAMESPACE_DETAIL,
)
from tests.integration.conftest import (
    REAL_APP_DATA_SUBDIR,
    make_real_app,
    mcp_initialize,
    mcp_tool_call,
)

pytestmark = pytest.mark.integration

_FORBIDDEN = 403
_NOT_FOUND = 404
_OK = 200
_CREATED = 201
_ACCEPTED = 202
_UNAUTHORIZED = 401
_TOML_NAMESPACE = "ns-toml"


def _h(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _mint(client, admin_key: str, namespace: str, *, admin: bool = False) -> dict:
    resp = client.post("/keys", json={"namespace": namespace, "admin": admin}, headers=_h(admin_key))
    assert resp.status_code == _CREATED, resp.text
    return resp.json()


def _keys_path(tmp_path: Path) -> Path:
    """``keys.json`` of an app ``make_real_app(tmp_path, ...)`` built: it lives in the app's private data dir."""
    return tmp_path / REAL_APP_DATA_SUBDIR / "keys.json"


def _stored(tmp_path: Path) -> list[dict]:
    return json.loads(_keys_path(tmp_path).read_text())


# ---------------------------------------------------------------------------
# Key issuance
# ---------------------------------------------------------------------------


def test_default_key_is_admin_and_creates_keys_for_any_namespace(tmp_path, monkeypatch) -> None:
    with make_real_app(tmp_path, monkeypatch) as (client, _cfg, admin_key):
        body = _mint(client, admin_key, "team-a")
        assert body["admin"] is False
        assert body["namespace"] == "team-a"
        # The default namespace too: an admin may issue for it.
        assert _mint(client, admin_key, "default")["namespace"] == "default"


def test_non_admin_key_cannot_mint_a_key_for_another_namespace(tmp_path, monkeypatch) -> None:
    with make_real_app(tmp_path, monkeypatch) as (client, _cfg, admin_key):
        team_b = _mint(client, admin_key, "team-b")["token"]
        before = _stored(tmp_path)

        resp = client.post("/keys", json={"namespace": "default"}, headers=_h(team_b))

        assert resp.status_code == _FORBIDDEN
        assert resp.json() == {"detail": FOREIGN_NAMESPACE_DETAIL}
        assert _stored(tmp_path) == before, "a refused request must not write keys.json"


def test_non_admin_key_cannot_mint_an_admin_key_even_for_its_own_namespace(tmp_path, monkeypatch) -> None:
    with make_real_app(tmp_path, monkeypatch) as (client, _cfg, admin_key):
        team_b = _mint(client, admin_key, "team-b")["token"]

        resp = client.post("/keys", json={"namespace": "team-b", "admin": True}, headers=_h(team_b))

        assert resp.status_code == _FORBIDDEN
        assert resp.json() == {"detail": ADMIN_KEY_GRANT_DETAIL}


def test_non_admin_key_can_mint_a_non_admin_key_for_its_own_namespace(tmp_path, monkeypatch) -> None:
    with make_real_app(tmp_path, monkeypatch) as (client, _cfg, admin_key):
        team_b = _mint(client, admin_key, "team-b")["token"]

        resp = client.post("/keys", json={"namespace": "team-b", "label": "ci"}, headers=_h(team_b))

        assert resp.status_code == _CREATED
        assert resp.json()["admin"] is False
        assert client.get("/keys", headers=_h(resp.json()["token"])).status_code == _OK


def test_an_admin_key_can_mint_an_admin_key_which_then_acts_across_namespaces(tmp_path, monkeypatch) -> None:
    with make_real_app(tmp_path, monkeypatch) as (client, _cfg, admin_key):
        second_admin = _mint(client, admin_key, "ops", admin=True)
        assert second_admin["admin"] is True

        resp = client.post("/keys", json={"namespace": "team-z"}, headers=_h(second_admin["token"]))

        assert resp.status_code == _CREATED


# ---------------------------------------------------------------------------
# Listing and revoking
# ---------------------------------------------------------------------------


def test_non_admin_lists_only_non_admin_keys_of_its_own_namespace(tmp_path, monkeypatch) -> None:
    with make_real_app(tmp_path, monkeypatch) as (client, _cfg, admin_key):
        team_b = _mint(client, admin_key, "team-b")
        _mint(client, admin_key, "team-b", admin=True)
        _mint(client, admin_key, "team-c")

        resp = client.get("/keys", params={"status": "all"}, headers=_h(team_b["token"]))

        assert resp.status_code == _OK
        assert [k["id"] for k in resp.json()["keys"]] == [team_b["id"]]
        everything = client.get("/keys", params={"status": "all"}, headers=_h(admin_key)).json()["keys"]
        assert {"team-b", "team-c"} <= {k["namespace"] for k in everything}


def test_non_admin_cannot_list_another_namespace(tmp_path, monkeypatch) -> None:
    with make_real_app(tmp_path, monkeypatch) as (client, _cfg, admin_key):
        team_b = _mint(client, admin_key, "team-b")["token"]

        resp = client.get("/keys", params={"namespace": "team-c"}, headers=_h(team_b))

        assert resp.status_code == _FORBIDDEN
        assert resp.json() == {"detail": FOREIGN_NAMESPACE_DETAIL}


def test_non_admin_cannot_revoke_another_namespaces_key(tmp_path, monkeypatch) -> None:
    with make_real_app(tmp_path, monkeypatch) as (client, _cfg, admin_key):
        team_b = _mint(client, admin_key, "team-b")["token"]
        victim = _mint(client, admin_key, "team-c")

        resp = client.delete(f"/keys/{victim['id']}", headers=_h(team_b))

        assert resp.status_code == _NOT_FOUND, "a foreign key must look unknown, not forbidden"
        assert client.get("/keys", headers=_h(victim["token"])).status_code == _OK, "the victim key must still work"


def test_non_admin_cannot_revoke_an_admin_key_of_its_own_namespace(tmp_path, monkeypatch) -> None:
    with make_real_app(tmp_path, monkeypatch) as (client, _cfg, admin_key):
        team_b = _mint(client, admin_key, "team-b")["token"]
        b_admin = _mint(client, admin_key, "team-b", admin=True)

        resp = client.delete(f"/keys/{b_admin['id']}", headers=_h(team_b))

        assert resp.status_code == _NOT_FOUND
        assert client.get("/keys", headers=_h(b_admin["token"])).status_code == _OK


def test_non_admin_can_revoke_a_non_admin_key_of_its_own_namespace(tmp_path, monkeypatch) -> None:
    with make_real_app(tmp_path, monkeypatch) as (client, _cfg, admin_key):
        team_b = _mint(client, admin_key, "team-b")["token"]
        sibling = _mint(client, admin_key, "team-b")

        assert client.delete(f"/keys/{sibling['id']}", headers=_h(team_b)).status_code == _OK
        assert client.get("/keys", headers=_h(sibling["token"])).status_code == _UNAUTHORIZED


def test_admin_revokes_any_key(tmp_path, monkeypatch) -> None:
    with make_real_app(tmp_path, monkeypatch) as (client, _cfg, admin_key):
        victim = _mint(client, admin_key, "team-c")

        assert client.delete(f"/keys/{victim['id']}", headers=_h(admin_key)).status_code == _OK
        assert client.get("/keys", headers=_h(victim["token"])).status_code == _UNAUTHORIZED


# ---------------------------------------------------------------------------
# Rotation of the default key
# ---------------------------------------------------------------------------


def test_non_admin_cannot_rotate_the_default_key(tmp_path, monkeypatch) -> None:
    with make_real_app(tmp_path, monkeypatch) as (client, _cfg, admin_key):
        team_b = _mint(client, admin_key, "team-b")["token"]
        before = _stored(tmp_path)

        resp = client.post("/keys/rotate", json={"grace_seconds": 0}, headers=_h(team_b))

        assert resp.status_code == _FORBIDDEN
        assert resp.json() == {"detail": ADMIN_REQUIRED_DETAIL}
        assert _stored(tmp_path) == before
        assert client.app.state.api_key == admin_key


def test_admin_rotates_the_default_key(tmp_path, monkeypatch) -> None:
    with make_real_app(tmp_path, monkeypatch) as (client, _cfg, admin_key):
        monkeypatch.delenv(ENV_VAR)  # rotation answers 409 while the env var is set

        resp = client.post("/keys/rotate", json={"grace_seconds": 0}, headers=_h(admin_key))

        assert resp.status_code == _OK, resp.text
        # The new default key is admin too.
        assert client.post("/keys", json={"namespace": "x"}, headers=_h(resp.json()["token"])).status_code == _CREATED


# ---------------------------------------------------------------------------
# Server-wide control endpoints
# ---------------------------------------------------------------------------

_ADMIN_ONLY_REQUESTS = [
    ("POST", "/backup/trigger", None),
    ("POST", "/maintenance/trigger", None),
    ("POST", "/sync", None),
    ("GET", "/telemetry/stats", None),
    ("GET", "/telemetry/entries", None),
    ("POST", "/collections/", {"path": "/tmp/../x"}),
]


@pytest.mark.parametrize(("method", "path", "body"), _ADMIN_ONLY_REQUESTS)
def test_control_endpoints_refuse_a_managed_non_admin_key(tmp_path, monkeypatch, method, path, body) -> None:
    with make_real_app(tmp_path, monkeypatch, maintenance_enabled=True) as (client, _cfg, admin_key):
        team_b = _mint(client, admin_key, "team-b")["token"]

        resp = client.request(method, path, json=body, headers=_h(team_b))

        assert resp.status_code == _FORBIDDEN, resp.text
        assert resp.json() == {"detail": ADMIN_REQUIRED_DETAIL}


@pytest.mark.parametrize(("method", "path", "body"), _ADMIN_ONLY_REQUESTS)
def test_control_endpoints_refuse_a_toml_namespace_token(tmp_path, monkeypatch, method, path, body) -> None:
    toml_token = secrets.token_hex(32)
    with make_real_app(tmp_path, monkeypatch, namespaces={toml_token: _TOML_NAMESPACE}) as (client, _cfg, _key):
        resp = client.request(method, path, json=body, headers=_h(toml_token))

        assert resp.status_code == _FORBIDDEN, resp.text
        assert resp.json() == {"detail": ADMIN_REQUIRED_DETAIL}


@pytest.mark.parametrize(("method", "path", "body"), _ADMIN_ONLY_REQUESTS)
def test_control_endpoints_accept_the_admin_key(tmp_path, monkeypatch, method, path, body) -> None:
    with make_real_app(tmp_path, monkeypatch, maintenance_enabled=True, telemetry_enabled=True) as (
        client,
        _cfg,
        admin_key,
    ):
        resp = client.request(method, path, json=body, headers=_h(admin_key))

        assert resp.status_code != _FORBIDDEN, resp.text
        assert resp.status_code < 500, resp.text


def test_a_toml_namespace_token_still_reaches_its_own_namespace_data_routes(tmp_path, monkeypatch) -> None:
    toml_token = secrets.token_hex(32)
    with make_real_app(tmp_path, monkeypatch, namespaces={toml_token: _TOML_NAMESPACE}) as (client, _cfg, _key):
        assert client.get("/collections/", headers=_h(toml_token)).status_code == _OK
        assert client.get("/status", headers=_h(toml_token)).status_code == _OK


# ---------------------------------------------------------------------------
# keys.json written before the ``admin`` field existed
# ---------------------------------------------------------------------------


def _legacy_record(token: str, namespace: str, *, record_id: str | None) -> dict:
    """A keys.json entry as written before the ``admin`` capability: no ``admin`` field."""
    return {
        "id": record_id,
        "token_hash": hashlib.sha256(token.encode()).hexdigest(),
        "namespace": namespace,
        "label": None,
        "created_at": datetime.now(UTC).isoformat(),
        "expires_at": None,
        "status": "active",
    }


def test_legacy_records_without_the_admin_field_are_admin_only_in_the_default_namespace(tmp_path, monkeypatch) -> None:
    legacy_default, legacy_other = secrets.token_hex(32), secrets.token_hex(32)
    _keys_path(tmp_path).parent.mkdir(exist_ok=True)
    _keys_path(tmp_path).write_text(
        json.dumps(
            [
                _legacy_record(legacy_default, "default", record_id="legacy-default"),
                _legacy_record(legacy_other, "team-x", record_id="legacy-other"),
            ]
        )
    )
    with make_real_app(tmp_path, monkeypatch) as (client, _cfg, _key):
        assert client.post("/keys", json={"namespace": "any"}, headers=_h(legacy_default)).status_code == _CREATED
        denied = client.post("/keys", json={"namespace": "any"}, headers=_h(legacy_other))
        assert denied.status_code == _FORBIDDEN
        assert denied.json() == {"detail": FOREIGN_NAMESPACE_DETAIL}


def test_the_admin_flag_is_persisted_in_keys_json(tmp_path, monkeypatch) -> None:
    with make_real_app(tmp_path, monkeypatch) as (client, _cfg, admin_key):
        plain = _mint(client, admin_key, "team-b")
        boss = _mint(client, admin_key, "ops", admin=True)

        by_id = {r["id"]: r for r in _stored(tmp_path)}
        assert by_id[plain["id"]]["admin"] is False
        assert by_id[boss["id"]]["admin"] is True
        assert all(r["admin"] is False for r in _stored(tmp_path) if r["id"] is None)


# ---------------------------------------------------------------------------
# MCP key tools share the same policy
# ---------------------------------------------------------------------------


def _mcp(client, token: str, tool: str, arguments: dict) -> dict:
    return mcp_tool_call(client, token, mcp_initialize(client, token), tool, arguments)


def test_mcp_key_tools_refuse_cross_namespace_and_admin_grants_to_a_non_admin(tmp_path, monkeypatch) -> None:
    with make_real_app(tmp_path, monkeypatch, mcp_enabled=True) as (client, _cfg, admin_key):
        team_b = _mint(client, admin_key, "team-b")["token"]
        victim = _mint(client, admin_key, "team-c")
        before = _stored(tmp_path)

        cross = _mcp(client, team_b, "create_key", {"namespace": "default"})
        grant = _mcp(client, team_b, "create_key", {"namespace": "team-b", "admin": True})
        listing = _mcp(client, team_b, "list_keys", {"namespace": "team-c"})
        revoke = _mcp(client, team_b, "revoke_key", {"key_id": victim["id"]})
        rotate = _mcp(client, team_b, "rotate_key", {"grace_seconds": 0})

        assert cross == {"error": FOREIGN_NAMESPACE_DETAIL, "code": "forbidden"}
        assert grant == {"error": ADMIN_KEY_GRANT_DETAIL, "code": "forbidden"}
        assert listing == {"error": FOREIGN_NAMESPACE_DETAIL, "code": "forbidden"}
        assert revoke["code"] == "not_found"
        assert rotate == {"error": ADMIN_REQUIRED_DETAIL, "code": "forbidden"}
        assert _stored(tmp_path) == before
        assert client.get("/keys", headers=_h(victim["token"])).status_code == _OK


def test_mcp_key_tools_work_for_a_non_admin_inside_its_own_namespace(tmp_path, monkeypatch) -> None:
    with make_real_app(tmp_path, monkeypatch, mcp_enabled=True) as (client, _cfg, admin_key):
        team_b = _mint(client, admin_key, "team-b")

        created = _mcp(client, team_b["token"], "create_key", {"namespace": "team-b"})
        listing = _mcp(client, team_b["token"], "list_keys", {})

        assert created["admin"] is False
        assert {k["id"] for k in listing["keys"]} == {team_b["id"], created["id"]}
        assert _mcp(client, team_b["token"], "revoke_key", {"key_id": created["id"]}) == {
            "id": created["id"],
            "status": "revoked",
        }


def test_mcp_admin_key_manages_every_namespace(tmp_path, monkeypatch) -> None:
    with make_real_app(tmp_path, monkeypatch, mcp_enabled=True) as (client, _cfg, admin_key):
        created = _mcp(client, admin_key, "create_key", {"namespace": "team-q", "admin": True})

        assert created["admin"] is True and created["namespace"] == "team-q"
        listing = _mcp(client, admin_key, "list_keys", {"status": "all"})
        assert created["id"] in {k["id"] for k in listing["keys"]}


def test_the_admin_decision_comes_from_the_stored_record_after_a_restart(tmp_path, monkeypatch) -> None:
    """Admin is not remembered in process memory: a new app on the same data dir still honours it."""
    with make_real_app(tmp_path, monkeypatch) as (client, _cfg, admin_key):
        boss = _mint(client, admin_key, "ops", admin=True)["token"]
    with make_real_app(tmp_path, monkeypatch) as (client, _cfg, _new_default):
        assert client.post("/keys", json={"namespace": "anywhere"}, headers=_h(boss)).status_code == _CREATED
    assert asyncio.run(_load(tmp_path))


async def _load(tmp_path: Path) -> list:
    from archon_search.key_manager import KeyStore

    return await KeyStore(_keys_path(tmp_path)).load()
