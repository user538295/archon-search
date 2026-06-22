"""Tests for BE-2: APIKeyMiddleware update with additive key_store parameter.

Covers:
- C2 (KeyStore ↔ APIKeyMiddleware seam)
- S2  (managed key accepted, namespace stamped)
- S11 (unknown token → 401)
- S12 (no auth header → 401 with WWW-Authenticate: Bearer)
- S16 (env-var key wins over a revoked managed-key record)
- S19 (hmac.compare_digest used for managed-key comparisons; early-exit on match)
- S25 (managed key beats TOML same token — dispatch order)
"""
from __future__ import annotations

import hashlib
import hmac
import secrets
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.requests import Request
from starlette.responses import Response

from archon_search.key_manager import KeyRecord, KeyStore
from archon_search.server.middleware_auth import APIKeyMiddleware

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_VALID_LEGACY_KEY = "a" * 64

# A valid raw token for managed key tests — 64 hex chars
_MANAGED_RAW_TOKEN = "b" * 64
_MANAGED_TOKEN_HASH = hashlib.sha256(_MANAGED_RAW_TOKEN.encode()).hexdigest()

_TOML_RAW_TOKEN = "c" * 64  # used for TOML namespace entries


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_active_record(
    raw_token: str,
    namespace: str = "managed-ns",
    *,
    label: str | None = None,
    status: str = "active",
    expires_at: datetime | None = None,
) -> KeyRecord:
    token_hash = hashlib.sha256(raw_token.encode()).hexdigest()
    return KeyRecord(
        id="test-id-1234",
        token_hash=token_hash,
        namespace=namespace,
        label=label,
        created_at=datetime.now(UTC),
        expires_at=expires_at,
        status=status,  # type: ignore[arg-type]
    )


def _mock_key_store(records: list[KeyRecord]) -> KeyStore:
    """Build a KeyStore mock whose active_keys() and load() return the given records."""
    store = MagicMock(spec=KeyStore)
    # active_keys filters to status==active and not expired — simulate the real filter
    now = datetime.now(UTC)
    active = [
        r
        for r in records
        if r.status == "active"
        and (r.expires_at is None or r.expires_at > now)
    ]
    store.active_keys = AsyncMock(return_value=active)
    store.load = AsyncMock(return_value=records)
    return store


def _mini_app_with_keystore(
    key_store: KeyStore | None,
    api_key: str = _VALID_LEGACY_KEY,
    namespaces: dict[str, str] | None = None,
) -> FastAPI:
    app = FastAPI()
    app.add_middleware(
        APIKeyMiddleware,
        api_key=api_key,
        namespaces=namespaces,
        key_store=key_store,
    )

    @app.get("/protected")
    async def protected(request: Request) -> dict:
        return {"namespace": request.state.namespace}

    @app.get("/health")
    async def health() -> dict:
        return {"ok": True}

    return app


# ---------------------------------------------------------------------------
# Unit: managed key accepted → namespace stamped (S2)
# ---------------------------------------------------------------------------


class TestMiddlewareManagedKeyAccepted:
    def test_middleware_managed_key_accepted(self) -> None:
        """Valid managed key → 200; request.state.namespace set to key's namespace (S2)."""
        record = _make_active_record(_MANAGED_RAW_TOKEN, namespace="managed-ns")
        key_store = _mock_key_store([record])
        app = _mini_app_with_keystore(key_store)
        client = TestClient(app, raise_server_exceptions=False)

        resp = client.get("/protected", headers={"Authorization": f"Bearer {_MANAGED_RAW_TOKEN}"})

        assert resp.status_code == 200
        assert resp.json()["namespace"] == "managed-ns"


# ---------------------------------------------------------------------------
# Unit: unknown token → 401 (S11)
# ---------------------------------------------------------------------------


class TestMiddlewareUnknownToken:
    def test_middleware_unknown_token_401(self) -> None:
        """Token that matches no managed key and no legacy key → 401 (S11)."""
        record = _make_active_record(_MANAGED_RAW_TOKEN, namespace="managed-ns")
        key_store = _mock_key_store([record])
        # Legacy key is different
        app = _mini_app_with_keystore(key_store, api_key="d" * 64)
        client = TestClient(app, raise_server_exceptions=False)

        unknown_token = "e" * 64
        resp = client.get("/protected", headers={"Authorization": f"Bearer {unknown_token}"})

        assert resp.status_code == 401


# ---------------------------------------------------------------------------
# Unit: no auth header → 401 with WWW-Authenticate: Bearer (S12)
# ---------------------------------------------------------------------------


class TestMiddlewareNoAuthHeader:
    def test_middleware_no_auth_header_401(self) -> None:
        """Missing Authorization header → 401 with WWW-Authenticate: Bearer (S12)."""
        key_store = _mock_key_store([])
        app = _mini_app_with_keystore(key_store)
        client = TestClient(app, raise_server_exceptions=False)

        resp = client.get("/protected")

        assert resp.status_code == 401
        assert resp.headers.get("WWW-Authenticate") == "Bearer"


# ---------------------------------------------------------------------------
# Unit: timing-safe hmac.compare_digest for managed keys (S19)
# ---------------------------------------------------------------------------


class TestMiddlewareTimingSafeCompareDigest:
    def test_middleware_timing_safe_compare_digest(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """hmac.compare_digest (not secrets.compare_digest or ==) used for managed-key comparisons.

        Both arguments must be 64-char hex strings (SHA-256 hex digests).
        The managed-key loop exits on first match (early exit is intentional for performance).
        (S19)
        """
        record = _make_active_record(_MANAGED_RAW_TOKEN, namespace="managed-ns")
        key_store = _mock_key_store([record])
        app = _mini_app_with_keystore(key_store)

        hmac_calls: list[tuple[str, str]] = []
        real_compare = hmac.compare_digest

        def tracking_compare(a: str, b: str) -> bool:
            hmac_calls.append((a, b))
            return real_compare(a, b)

        with patch("archon_search.server.middleware_auth.hmac.compare_digest", side_effect=tracking_compare):
            client = TestClient(app, raise_server_exceptions=False)
            resp = client.get("/protected", headers={"Authorization": f"Bearer {_MANAGED_RAW_TOKEN}"})

        assert resp.status_code == 200

        # At least one call must have been made for the managed key
        assert len(hmac_calls) >= 1, "hmac.compare_digest was not called"

        # Both arguments must be 64-char hex strings (SHA-256 digests)
        for a_arg, b_arg in hmac_calls:
            assert len(a_arg) == 64 and all(c in "0123456789abcdef" for c in a_arg), (
                f"First arg is not a 64-char hex string: {a_arg!r}"
            )
            assert len(b_arg) == 64 and all(c in "0123456789abcdef" for c in b_arg), (
                f"Second arg is not a 64-char hex string: {b_arg!r}"
            )

        # The managed-key loop exits on first match — with one record, exactly one hmac call
        assert len(hmac_calls) == 1, (
            f"Expected exactly 1 hmac.compare_digest call (early exit on match), got {len(hmac_calls)}"
        )


# ---------------------------------------------------------------------------
# Unit: TOML namespace loop has no early exit (timing-safe for namespace tokens)
# ---------------------------------------------------------------------------


class TestMiddlewareTomlLoopNoEarlyExit:
    def test_middleware_toml_loop_no_early_exit(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """TOML namespace dict is iterated fully even after a match (no break).

        This preserves the existing timing-safe behavior for TOML namespace tokens.
        """
        # No managed keys — only TOML namespaces
        key_store = _mock_key_store([])

        KEY_A = "f" * 64
        KEY_B = "g" * 64
        namespaces = {KEY_A: "tenantA", KEY_B: "tenantB"}
        app = _mini_app_with_keystore(key_store, api_key="z" * 64, namespaces=namespaces)

        secrets_calls: list[tuple[str, str]] = []
        real_compare = secrets.compare_digest

        def tracking_compare(a: str, b: str) -> bool:
            secrets_calls.append((a, b))
            return real_compare(a, b)

        with patch("archon_search.server.middleware_auth.secrets.compare_digest", side_effect=tracking_compare):
            client = TestClient(app, raise_server_exceptions=False)
            # KEY_A is the first namespace key; the loop must continue to KEY_B
            resp = client.get("/protected", headers={"Authorization": f"Bearer {KEY_A}"})

        assert resp.status_code == 200
        assert resp.json()["namespace"] == "tenantA"

        # Both namespace entries must have been compared (no early exit)
        assert len(secrets_calls) >= 2, (
            f"Expected >= 2 secrets.compare_digest calls (all namespace entries), got {len(secrets_calls)}"
        )


# ---------------------------------------------------------------------------
# Unit: managed key beats TOML same token (S25)
# ---------------------------------------------------------------------------


class TestMiddlewareManagedBeatsTomlSameToken:
    def test_middleware_managed_beats_toml_same_token(self) -> None:
        """Same raw token in both managed keys (namespace=A) and TOML (namespace=B).

        Managed key wins per dispatch order — request.state.namespace = A (S25).
        """
        # Use the same raw token for both managed and TOML
        shared_token = "h" * 64
        record = _make_active_record(shared_token, namespace="managed-ns-A")
        key_store = _mock_key_store([record])

        # TOML also maps this token to a different namespace
        namespaces = {shared_token: "toml-ns-B"}
        app = _mini_app_with_keystore(key_store, api_key="z" * 64, namespaces=namespaces)
        client = TestClient(app, raise_server_exceptions=False)

        resp = client.get("/protected", headers={"Authorization": f"Bearer {shared_token}"})

        assert resp.status_code == 200
        # Managed key's namespace must win
        assert resp.json()["namespace"] == "managed-ns-A"


# ---------------------------------------------------------------------------
# Unit: revoked managed key blocks legacy fallback (rotation-revocation guard)
# ---------------------------------------------------------------------------


class TestMiddlewareRevokedManagedKeyBlocksLegacyFallback:
    def test_middleware_revoked_managed_key_blocks_legacy_fallback(self) -> None:
        """After rotation: a token revoked in keys.json is rejected with 401 even if it
        matches _api_key on the legacy path (rotation-revocation guard)."""
        # Token that is BOTH the legacy api_key AND revoked in key_store
        rotated_out_token = "i" * 64
        revoked_record = _make_active_record(
            rotated_out_token, namespace="default", status="revoked"
        )
        # key_store.active_keys() returns no active keys for this token
        # but key_store.load() returns the revoked record
        key_store = _mock_key_store([revoked_record])
        # Legacy api_key IS the same token
        app = _mini_app_with_keystore(key_store, api_key=rotated_out_token)
        client = TestClient(app, raise_server_exceptions=False)

        resp = client.get("/protected", headers={"Authorization": f"Bearer {rotated_out_token}"})

        # Must be rejected even though the token matches the legacy api_key
        assert resp.status_code == 401


# ---------------------------------------------------------------------------
# Unit: expired-but-active-status key blocks legacy fallback (datetime guard)
# ---------------------------------------------------------------------------


class TestMiddlewareExpiredKeyBlocksLegacyFallback:
    def test_middleware_expired_active_key_blocks_legacy_fallback(self) -> None:
        """A key with status='active' but expires_at in the past is blocked by the
        rotation-revocation guard even though it matches _api_key on the legacy path.

        This exercises the datetime-based branch of the revocation guard:
            r.expires_at is not None and r.expires_at <= now
        (the Cycle-1 fix that replaced the dead 'expired' status literal check).
        """
        from datetime import UTC, datetime, timedelta

        expired_token = "n" * 64
        # status="active" but expires_at is 1 hour in the past
        expired_record = _make_active_record(
            expired_token,
            namespace="default",
            status="active",
            expires_at=datetime.now(UTC) - timedelta(hours=1),
        )
        # active_keys() filters this record out (expired); load() returns it
        key_store = _mock_key_store([expired_record])
        # Legacy api_key IS the same token
        app = _mini_app_with_keystore(key_store, api_key=expired_token)
        client = TestClient(app, raise_server_exceptions=False)

        resp = client.get("/protected", headers={"Authorization": f"Bearer {expired_token}"})

        # Must be rejected — expired key blocks the legacy fallback
        assert resp.status_code == 401


# ---------------------------------------------------------------------------
# Unit: env-var key wins over a revoked record (S16)
# ---------------------------------------------------------------------------


class TestMiddlewareEnvKeyWinsOverRevoked:
    def test_middleware_env_key_wins_over_revoked(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """ARCHON_SEARCH_API_KEY wins over a revoked managed-key record for the same token (S16).

        S16 scenario: env var is set AND the token exists in keys.json as revoked →
        the request is ACCEPTED (env var wins, not blocked by revoked record).

        Implementation note: S16 is satisfied by running the middleware in a context where
        the api_key parameter equals the env-var value. This test proves the middleware's
        behavior when key_store=None (the env-var case). The INTENDED BE-3 wiring is that
        when ARCHON_SEARCH_API_KEY is set, app.py will pass key_store=None to the middleware
        to avoid the negative check — that wiring does not yet exist in app.py and is planned
        for BE-3. The env-var token being revoked in keys.json does NOT block it on the legacy
        path when key_store is None (legacy-only mode).
        """
        env_token = "j" * 64
        # The token IS revoked in keys.json
        revoked_record = _make_active_record(
            env_token, namespace="default", status="revoked"
        )
        # Critically: the key_store is None here (S16 is for env-var-only mode)
        # When ARCHON_SEARCH_API_KEY is set, app.py passes key_store=None to the middleware
        # to avoid the negative check. S16 requires 200.
        app = _mini_app_with_keystore(key_store=None, api_key=env_token)
        client = TestClient(app, raise_server_exceptions=False)

        resp = client.get("/protected", headers={"Authorization": f"Bearer {env_token}"})

        assert resp.status_code == 200


# ---------------------------------------------------------------------------
# Unit: legacy path unchanged when key_store=None
# ---------------------------------------------------------------------------


class TestMiddlewareLegacyPathUnchanged:
    def test_middleware_legacy_path_unchanged(self) -> None:
        """Existing api_key + namespaces path works when key_store=None."""
        KEY_TOML = "k" * 64
        LEGACY_KEY = "l" * 64
        app = _mini_app_with_keystore(
            key_store=None,
            api_key=LEGACY_KEY,
            namespaces={KEY_TOML: "toml-ns"},
        )
        client = TestClient(app, raise_server_exceptions=False)

        # TOML namespace key works
        resp_toml = client.get("/protected", headers={"Authorization": f"Bearer {KEY_TOML}"})
        assert resp_toml.status_code == 200
        assert resp_toml.json()["namespace"] == "toml-ns"

        # Legacy api_key works
        resp_legacy = client.get("/protected", headers={"Authorization": f"Bearer {LEGACY_KEY}"})
        assert resp_legacy.status_code == 200

        # Unknown token fails
        resp_unknown = client.get("/protected", headers={"Authorization": f"Bearer {'m' * 64}"})
        assert resp_unknown.status_code == 401


# ---------------------------------------------------------------------------
# Integration: managed key accepted on a real TestClient request
# ---------------------------------------------------------------------------


class TestMiddlewareManagedKeyFullRequest:
    def test_middleware_managed_key_full_request(self, tmp_path: Path) -> None:
        """Managed key created via real KeyStore is accepted on a real TestClient request (S2, integration)."""
        import asyncio

        key_store = KeyStore(path=tmp_path / "keys.json")
        result = asyncio.run(key_store.create(ns="int-ns", label=None, expires_at=None))
        raw_token = result["token"]

        app = _mini_app_with_keystore(key_store, api_key="z" * 64)
        client = TestClient(app, raise_server_exceptions=False)

        resp = client.get("/protected", headers={"Authorization": f"Bearer {raw_token}"})

        assert resp.status_code == 200
        assert resp.json()["namespace"] == "int-ns"
