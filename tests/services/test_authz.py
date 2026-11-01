"""Unit tests for archon_search.services.authz (C3): the transport-free authorization policy."""
from __future__ import annotations

import subprocess
import sys

import pytest

from archon_search.key_manager import KeyStore
from archon_search.services.authz import (
    ADMIN_KEY_GRANT_DETAIL,
    ADMIN_REQUIRED_DETAIL,
    FORBIDDEN_CODE,
    FOREIGN_NAMESPACE_DETAIL,
    AuthIdentity,
    ForbiddenError,
    authorize_key_create,
    authorize_key_listing_namespace,
    caller_may_revoke,
    can_manage_key,
    require_admin,
)

_ADMIN = AuthIdentity(namespace="ops", admin=True)
_USER = AuthIdentity(namespace="team", admin=False)


def test_require_admin_rejects_non_admin_with_fixed_detail():
    assert FORBIDDEN_CODE == "forbidden"
    assert ADMIN_REQUIRED_DETAIL == "this operation requires an admin API key"
    require_admin(_ADMIN)
    for caller in (_USER, None):
        with pytest.raises(ForbiddenError) as exc_info:
            require_admin(caller)
        assert str(exc_info.value) == ADMIN_REQUIRED_DETAIL


def test_authorize_key_create_and_listing_namespace_rules():
    authorize_key_create(_ADMIN, namespace="other", admin=True)
    authorize_key_create(_USER, namespace="team", admin=False)
    with pytest.raises(ForbiddenError) as grant:
        authorize_key_create(_USER, namespace="team", admin=True)
    assert str(grant.value) == ADMIN_KEY_GRANT_DETAIL == "only an admin API key can create an admin key"
    with pytest.raises(ForbiddenError) as foreign:
        authorize_key_create(_USER, namespace="other", admin=False)
    assert (
        str(foreign.value)
        == FOREIGN_NAMESPACE_DETAIL
        == "a non-admin API key can only manage keys in its own namespace"
    )

    assert authorize_key_listing_namespace(_ADMIN, None) is None
    assert authorize_key_listing_namespace(_ADMIN, "other") == "other"
    assert authorize_key_listing_namespace(_USER, None) == "team"
    assert authorize_key_listing_namespace(_USER, "team") == "team"
    with pytest.raises(ForbiddenError) as listing:
        authorize_key_listing_namespace(_USER, "other")
    assert str(listing.value) == FOREIGN_NAMESPACE_DETAIL


async def test_caller_may_revoke_reads_key_store(tmp_path):
    store = KeyStore(tmp_path / "keys.json")
    own = await store.create("team", None, None)
    foreign = await store.create("other", None, None)
    admin_key = await store.create("team", None, None, admin=True)
    records = {r.id: r for r in await store.list_keys()}

    assert await caller_may_revoke(store, _USER, own.id)
    assert not await caller_may_revoke(store, _USER, foreign.id)
    assert not await caller_may_revoke(store, _USER, admin_key.id)
    assert not await caller_may_revoke(store, _USER, "unknown-id")
    assert await caller_may_revoke(store, _ADMIN, "unknown-id")

    assert can_manage_key(_USER, records[own.id])
    assert not can_manage_key(_USER, records[foreign.id])
    assert not can_manage_key(_USER, records[admin_key.id])
    assert can_manage_key(_ADMIN, records[admin_key.id])


def test_services_authz_loads_without_fastapi():
    script = (
        "import sys\n"
        "import archon_search.services.authz\n"
        "print(','.join(sorted(sys.modules)))\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True, timeout=30, check=False
    )
    assert result.returncode == 0, result.stderr
    loaded = set(result.stdout.strip().splitlines()[-1].split(","))
    assert "archon_search.services.authz" in loaded
    banned = {"fastapi", "starlette", "fastmcp", "uvicorn"}
    assert not {m for m in loaded if m.split(".")[0] in banned}
    assert not {m for m in loaded if m == "archon_search.server" or m.startswith("archon_search.server.")}
