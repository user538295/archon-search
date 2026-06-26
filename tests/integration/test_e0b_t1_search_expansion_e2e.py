"""E0b / T-1 — e2e: POST /search expansion warning in all cases.

Scenarios covered:
  S3: HyDE requested, resolve_hyde_vector returns (None, False) →
      expansion_used=false, expansion_warning contains 'HyDE expansion failed'
  S5: Neither HyDE nor RAG Fusion requested →
      expansion_used=false, expansion_warning=null

Note: TestClient-based tests are integration-level (in-process ASGI). Labeled
#e2e_test in the plan because they exercise the full application stack with a
real SearchPipeline, real LanceDB store, and real ASGI middleware chain.
True process-isolated e2e is not required for E0b.

Scenarios S1, S2, S4, S4b are covered at integration level in
tests/server/test_routes_search.py (BE-3 tasks). T-1 provides a complementary
full-stack verification using make_real_app (real pipeline) instead of the
mocked pipeline used by the BE-3 tests.
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from tests.integration.conftest import ingest_file_via_path, make_real_app

pytestmark = pytest.mark.integration


def _auth(api_key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {api_key}"}


# ---------------------------------------------------------------------------
# S3: HyDE requested → resolve_hyde_vector returns (None, False) →
#     expansion_used=false, expansion_warning contains 'HyDE expansion failed'
# ---------------------------------------------------------------------------


def test_e2e_search_expansion_failure_warning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Real app via TestClient; force resolve_hyde_vector() to return (None, False) with HyDE
    requested; assert response has expansion_used=false, expansion_warning is non-null and
    equals 'HyDE expansion failed'.

    Covers scenario S3: HyDE timeout/failure detection at the route level. resolve_hyde_vector
    is stubbed, so this verifies route-level warning mapping when HyDE resolution returns
    failure — not the HyDE resolution logic itself.
    """
    doc = tmp_path / "e0b_t1_hyde_test.md"
    doc.write_text(
        "# E0b T1 Test Document\n\nContent for search expansion e2e test.\n" * 8
    )
    col = "e0b-t1-expansion-warning"

    with make_real_app(tmp_path, monkeypatch) as (client, cfg, api_key):
        ingest_file_via_path(client, col, str(doc), api_key=api_key)

        # Force resolve_hyde_vector to return (None, False) — simulates all HyDE failure
        # modes (timeout, API error, missing key, empty response) which are indistinguishable
        # at the route level. config.hyde.enabled is irrelevant: the mock replaces the
        # function entirely, so the config parameter is never read.
        with patch(
            "archon_search.server.routes_search.resolve_hyde_vector",
            new=AsyncMock(return_value=(None, False)),
        ):
            resp = client.post(
                "/search",
                json={"collection": col, "query": "expansion test content", "hyde": True},
                headers=_auth(api_key),
            )

            assert resp.status_code == 200, (
                f"expected 200, got {resp.status_code}: {resp.text}"
            )
            data = resp.json()
            assert data["expansion_used"] is False, (
                f"expected expansion_used=false when HyDE fails, got: {data['expansion_used']}"
            )
            assert data["hyde_applied"] is False, (
                f"expected hyde_applied=false when HyDE fails, got: {data['hyde_applied']}"
            )
            assert data["rag_fusion_applied"] is False, (
                f"expected rag_fusion_applied=false when HyDE fails, got: {data['rag_fusion_applied']}"
            )
            assert data["expansion_warning"] == "HyDE expansion failed", (
                f"expected expansion_warning='HyDE expansion failed', got: {data['expansion_warning']!r}"
            )


# ---------------------------------------------------------------------------
# S5: Neither HyDE nor RAG Fusion requested → expansion_used=false, expansion_warning=null
# ---------------------------------------------------------------------------


def test_e2e_search_no_expansion_requested(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Real app via TestClient; POST /search without hyde or rag_fusion flags; assert
    expansion_used=false and expansion_warning=null.

    Covers scenario S5: the default search path with no expansion produces clean
    (false, null) fields in the response, verified through the full application stack.
    """
    doc = tmp_path / "e0b_t1_plain_test.md"
    doc.write_text(
        "# E0b T1 Plain Search Test\n\nContent for plain search without expansion.\n" * 8
    )
    col = "e0b-t1-no-expansion"

    with make_real_app(tmp_path, monkeypatch) as (client, cfg, api_key):
        ingest_file_via_path(client, col, str(doc), api_key=api_key)

        resp = client.post(
            "/search",
            json={"collection": col, "query": "plain search without expansion"},
            headers=_auth(api_key),
        )

        assert resp.status_code == 200, (
            f"expected 200, got {resp.status_code}: {resp.text}"
        )
        data = resp.json()
        assert data["expansion_used"] is False, (
            f"expected expansion_used=false when no expansion requested, got: {data['expansion_used']}"
        )
        assert data["hyde_applied"] is False, (
            f"expected hyde_applied=false when no expansion requested, got: {data['hyde_applied']}"
        )
        assert data["rag_fusion_applied"] is False, (
            f"expected rag_fusion_applied=false when no expansion requested, got: {data['rag_fusion_applied']}"
        )
        assert data["expansion_warning"] is None, (
            f"expected expansion_warning=null when no expansion requested, got: {data['expansion_warning']!r}"
        )
