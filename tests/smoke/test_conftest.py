"""Smoke tests for the ``smoke_server`` session fixture itself (BE-2).

These tests validate the fixture's building blocks (API key format) and its
end-to-end behaviour (server starts, health/ready polling succeeds, the
corpus pre-seed produces a non-empty collection) and its failure path
(bind failure surfaces "server did not start" with captured stderr).

Module-level ``pytestmark`` serialises this file on one xdist worker so that
all smoke tests share the single session-scoped server subprocess (matches
the pattern in ``tests/smoke/test_cli.py``).
"""

from __future__ import annotations

import secrets
import socket
import time

import httpx
import pytest

from tests.smoke.conftest import _HEALTH_READY_TIMEOUT_S, _start_server, _free_port

pytestmark = pytest.mark.xdist_group("smoke_e2e")


# ---------------------------------------------------------------------------
# Unit test — API key format assumption
# ---------------------------------------------------------------------------


def test_fixture_api_key_format() -> None:
    """``secrets.token_hex(32)`` must produce a 64-char all-lowercase hex string.

    ``key_manager.py:497`` (``_validate_key``) only requires a non-empty
    lowercase-hex string (``_HEX_RE = re.compile(r"^[0-9a-f]+$")`` — no
    length constraint). The 64-char length asserted here is a property of
    ``secrets.token_hex(32)`` itself, asserted for stability, not something
    the validator demands. A malformed key would still be rejected by the
    validator and would break all authenticated smoke-test requests.
    """
    key = secrets.token_hex(32)
    assert len(key) == 64
    assert key == key.lower()
    assert all(c in "0123456789abcdef" for c in key)


# ---------------------------------------------------------------------------
# Integration test — fixture starts server and seeds corpus (S1)
# ---------------------------------------------------------------------------


def test_smoke_server_starts_and_seeds(smoke_server) -> None:
    """The session-scoped ``smoke_server`` fixture yields a live, seeded server."""
    health_resp = httpx.get(f"{smoke_server.base_url}/health", timeout=5)
    assert health_resp.status_code == 200

    collections_resp = httpx.get(
        f"{smoke_server.base_url}/collections/smoke",
        headers={"Authorization": f"Bearer {smoke_server.api_key}"},
        timeout=5,
    )
    assert collections_resp.status_code == 200
    assert collections_resp.json()["doc_count"] > 0


# ---------------------------------------------------------------------------
# Integration test — startup failure surfaces stderr (S15)
# ---------------------------------------------------------------------------


def test_startup_failure_error_includes_stderr(tmp_path_factory) -> None:
    """Pre-binding the target port forces a server bind failure.

    The fixture helper must raise ``RuntimeError`` with a "server did not
    start" message that includes the captured stderr.

    Bind-collision mechanism: the subprocess's serve mode binds to its
    default host, ``0.0.0.0`` (all interfaces). The blocker below binds the
    SAME address, ``0.0.0.0:{port}`` — not ``127.0.0.1`` — because on macOS
    (verified empirically on this dev machine) a listener on ``127.0.0.1``
    does NOT collide with a later bind to ``0.0.0.0`` on the same port; both
    listen independently and a request to ``127.0.0.1`` connects to whichever
    socket owns that specific address, producing a hang/timeout rather than
    a fast, deterministic "address already in use" failure. Binding the
    blocker to the identical ``0.0.0.0`` address the server itself uses
    reproduces a genuine ``OSError: [Errno 48] Address already in use`` that
    the server process surfaces and exits on immediately, regardless of
    platform.
    """
    port = _free_port()
    blocker = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    blocker.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    blocker.bind(("0.0.0.0", port))
    blocker.listen(1)
    try:
        data_dir = tmp_path_factory.mktemp("smoke_data_bindfail")
        api_key = secrets.token_hex(32)
        start = time.monotonic()
        with pytest.raises(RuntimeError) as exc_info:
            _start_server(port=port, data_dir=data_dir, api_key=api_key)
        elapsed = time.monotonic() - start
        # A real bind failure exits fast; if this were instead hitting the
        # health/ready poll timeout, elapsed would be >= _HEALTH_READY_TIMEOUT_S.
        assert elapsed < _HEALTH_READY_TIMEOUT_S
        message = str(exc_info.value)
        assert "server did not start" in message
        assert "--- captured stderr ---" in message
    finally:
        blocker.close()


# ---------------------------------------------------------------------------
# Integration test — graph-enabled fixture has graph data (BE-9, guards S3)
# ---------------------------------------------------------------------------


def test_smoke_server_graph_enabled_has_graph_data(smoke_server_graph_enabled) -> None:
    """The graph-enabled smoke server's seeded collection reports a non-empty graph.

    Guards the S3 e2e test (``graph build-communities --wait``): without this,
    a graph too small to cluster would make S3 hit the S8 failure path
    (``CommunityBuilder.build`` raising on an empty/single-node graph) instead
    of S3's happy path.

    The graph extras are guarded (``importorskip("spacy")``) inside the
    ``smoke_server_graph_enabled`` fixture itself — before the server is
    spawned — so on a machine without them the fixture (and hence this test)
    skips cleanly rather than erroring at setup. No further guard is needed
    here.

    Distinct from the fixture's own >=2-node/>=1-edge sanity check: this test
    asserts the consumer-visible contract end to end — the collection is
    populated (``doc_count > 0``) AND its graph is enabled (200, not the 422 a
    disabled server returns) and non-empty (``node_count > 0``).
    """
    headers = {"Authorization": f"Bearer {smoke_server_graph_enabled.api_key}"}

    detail_resp = httpx.get(
        f"{smoke_server_graph_enabled.base_url}/collections/smoke_graph",
        headers=headers,
        timeout=5,
    )
    assert detail_resp.status_code == 200
    assert detail_resp.json()["doc_count"] > 0

    graph_resp = httpx.get(
        f"{smoke_server_graph_enabled.base_url}/graph/smoke_graph",
        headers=headers,
        timeout=5,
    )
    assert graph_resp.status_code == 200, (
        f"expected 200 (graph enabled); a 422 would mean graph is disabled: "
        f"{graph_resp.status_code} {graph_resp.text}"
    )
    assert graph_resp.json()["node_count"] > 0
