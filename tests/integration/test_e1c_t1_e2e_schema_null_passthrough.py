"""E1c / T-1 — e2e smoke tests: /explain schema extension + null pass-through.

Covers:
- (a) POST /explain with ``graph_mode`` omitted entirely → response unchanged from
      pre-E1c; ``graph_mode_applied`` is null; all ``result.graph_provenance`` are
      null; no existing response field is absent  (S1)
- (b) POST /explain with ``graph_mode: null`` supplied explicitly → identical
      assertions to (a)  (S13)

Both tests use a real app (``make_real_app``) with two real ingested files and
``top_k=1`` so that ``results`` is non-empty and ``near_misses`` is also non-empty
(each file produces at least one chunk; with top_k=1 the second file's chunk lands
in near_misses, making the S9 assertion — near_misses must NOT carry
``graph_provenance`` — exercised on real items rather than vacuously on an empty list).

No graph feature enabled — these tests verify backward compatibility for non-graph
callers: the new ``graph_mode_applied`` response field is present and null, the new
``graph_provenance`` field on each result is present and null, and all pre-E1c fields
are still present at the top level.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from tests.integration.conftest import ingest_file_via_path, make_real_app

pytestmark = pytest.mark.integration

# ---------------------------------------------------------------------------
# Pre-E1c required top-level response fields — every always-present field in
# ExplainResponse plus the two new E1c additive fields.
# Intentionally excludes:
#   - stage_timings_ms: conditionally popped from the dict when None
#     (routes_explain.py lines 540 and 664-665)
# ---------------------------------------------------------------------------

_REQUIRED_TOP_LEVEL_FIELDS = {
    "rerank",
    "routing",
    "collection",
    "acl_filtered",
    "results",
    "near_misses",
    "excluded_collections",
    "embedding_model",
    "hyde_applied",
    "rag_fusion_applied",
    "rag_fusion_queries_used",
    "rag_fusion_attempted",
    "rag_fusion_failure_reason",
    "rag_fusion_sub_queries",
    # E1c additive fields — present but null for non-graph callers:
    "graph_mode_applied",
}


def _auth(api_key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {api_key}"}


# ---------------------------------------------------------------------------
# (a) test_explain_graph_mode_omitted_response_unchanged
# ---------------------------------------------------------------------------


def test_explain_graph_mode_omitted_response_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """POST /explain without ``graph_mode`` → ``graph_mode_applied`` is null;
    all result ``graph_provenance`` are null; pre-E1c fields all present  (S1).

    The request body deliberately omits ``graph_mode`` entirely — the field must
    default to null server-side and the response must look identical to what a
    pre-E1c caller would receive, with the two new additive fields set to null.

    Two files are ingested so the collection contains at least 2 chunks. ``top_k=1``
    ensures ``results`` has 1 item and ``near_misses`` is non-empty, making the S9
    assertion (near_misses must NOT carry ``graph_provenance``) non-vacuous.
    """
    col = "t1-omit-graph-mode"
    # Two distinct files so the collection has at least 2 chunks.
    # With top_k=1, the second file's chunk lands in near_misses, making the
    # S9 assertion (near_misses must NOT carry graph_provenance) non-vacuous.
    doc1 = tmp_path / "doc1.txt"
    doc1.write_text(
        "Archon search explain provenance graph mode null passthrough test.\n",
        encoding="utf-8",
    )
    doc2 = tmp_path / "doc2.txt"
    doc2.write_text(
        "Pipeline retrieval reranker scoring breakdown diagnostics result.\n",
        encoding="utf-8",
    )

    with make_real_app(tmp_path, monkeypatch) as (client, _cfg, api_key):
        ingest_file_via_path(client, col, str(doc1), api_key=api_key)
        ingest_file_via_path(client, col, str(doc2), api_key=api_key)

        # graph_mode is intentionally absent from the payload.
        # top_k=1 ensures results has exactly 1 item and near_misses is non-empty.
        resp = client.post(
            "/explain",
            json={"query": "archon explain provenance", "collection": col, "top_k": 1},
            headers=_auth(api_key),
        )

        assert resp.status_code == 200, (
            f"Expected 200 for explain without graph_mode; "
            f"got {resp.status_code}: {resp.text}"
        )
        body = resp.json()

        # All pre-E1c + new additive fields must be present at the top level
        for field in _REQUIRED_TOP_LEVEL_FIELDS:
            assert field in body, (
                f"Required response field {field!r} missing from /explain response. "
                f"Present keys: {sorted(body.keys())}"
            )

        # New E1c field: must be present and null (graph_mode omitted → no graph path)
        assert body["graph_mode_applied"] is None, (
            f"Expected graph_mode_applied=null when graph_mode is omitted; "
            f"got {body['graph_mode_applied']!r}"
        )

        # All result items must carry graph_provenance=null (non-graph path)
        results = body.get("results", [])
        assert results, (
            "Expected non-empty results after ingest; "
            "cannot verify graph_provenance=null on an empty list"
        )
        for i, result in enumerate(results):
            assert "graph_provenance" in result, (
                f"results[{i}] missing 'graph_provenance' field. "
                f"Present keys: {sorted(result.keys())}"
            )
            assert result["graph_provenance"] is None, (
                f"results[{i}].graph_provenance expected null for non-graph explain; "
                f"got {result['graph_provenance']!r}"
            )

        # Near-miss items must NOT carry graph_provenance (schema omission by design, S9).
        # top_k=1 guarantees near_misses is non-empty with a multi-chunk document, so
        # this assertion is exercised on real items rather than vacuously on an empty list.
        near_misses = body.get("near_misses", [])
        assert near_misses, (
            "Expected non-empty near_misses with top_k=1 and multi-chunk document; "
            "cannot verify graph_provenance absence on an empty list (S9)"
        )
        for i, nm in enumerate(near_misses):
            assert "graph_provenance" not in nm, (
                f"near_misses[{i}] must not have 'graph_provenance' field (S9); "
                f"present keys: {sorted(nm.keys())}"
            )


# ---------------------------------------------------------------------------
# (b) test_explain_graph_mode_null_explicit_response_unchanged
# ---------------------------------------------------------------------------


def test_explain_graph_mode_null_explicit_response_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """POST /explain with ``graph_mode: null`` explicit → same as omitted (S13).

    A caller who explicitly sets ``graph_mode`` to null must receive the same
    response as a caller who omits it entirely.  ``graph_mode_applied`` must be
    null, all result ``graph_provenance`` must be null, and no pre-E1c field
    must be absent.

    Two files are ingested so the collection contains at least 2 chunks. ``top_k=1``
    ensures ``results`` has 1 item and ``near_misses`` is non-empty, making the S9
    assertion (near_misses must NOT carry ``graph_provenance``) non-vacuous.
    """
    col = "t1-null-graph-mode"
    # Two distinct files so the collection has at least 2 chunks.
    # With top_k=1, the second file's chunk lands in near_misses, making the
    # S9 assertion (near_misses must NOT carry graph_provenance) non-vacuous.
    doc1 = tmp_path / "doc1.txt"
    doc1.write_text(
        "Archon search explain graph mode null explicit passthrough test.\n",
        encoding="utf-8",
    )
    doc2 = tmp_path / "doc2.txt"
    doc2.write_text(
        "Pipeline retrieval reranker scoring breakdown diagnostics result.\n",
        encoding="utf-8",
    )

    with make_real_app(tmp_path, monkeypatch) as (client, _cfg, api_key):
        ingest_file_via_path(client, col, str(doc1), api_key=api_key)
        ingest_file_via_path(client, col, str(doc2), api_key=api_key)

        # graph_mode is explicitly supplied as null (JSON null).
        # top_k=1 ensures results has exactly 1 item and near_misses is non-empty.
        resp = client.post(
            "/explain",
            json={
                "query": "archon explain graph mode",
                "collection": col,
                "graph_mode": None,
                "top_k": 1,
            },
            headers=_auth(api_key),
        )

        assert resp.status_code == 200, (
            f"Expected 200 for explain with graph_mode=null; "
            f"got {resp.status_code}: {resp.text}"
        )
        body = resp.json()

        # All pre-E1c + new additive fields must be present at the top level
        for field in _REQUIRED_TOP_LEVEL_FIELDS:
            assert field in body, (
                f"Required response field {field!r} missing from /explain response. "
                f"Present keys: {sorted(body.keys())}"
            )

        # New E1c field: null because graph_mode=null was requested
        assert body["graph_mode_applied"] is None, (
            f"Expected graph_mode_applied=null when graph_mode=null; "
            f"got {body['graph_mode_applied']!r}"
        )

        # All result items must carry graph_provenance=null
        results = body.get("results", [])
        assert results, (
            "Expected non-empty results after ingest; "
            "cannot verify graph_provenance=null on an empty list"
        )
        for i, result in enumerate(results):
            assert "graph_provenance" in result, (
                f"results[{i}] missing 'graph_provenance' field. "
                f"Present keys: {sorted(result.keys())}"
            )
            assert result["graph_provenance"] is None, (
                f"results[{i}].graph_provenance expected null for graph_mode=null; "
                f"got {result['graph_provenance']!r}"
            )

        # Near-miss items must NOT carry graph_provenance (schema omission by design, S9).
        # top_k=1 guarantees near_misses is non-empty with a multi-chunk document, so
        # this assertion is exercised on real items rather than vacuously on an empty list.
        near_misses = body.get("near_misses", [])
        assert near_misses, (
            "Expected non-empty near_misses with top_k=1 and multi-chunk document; "
            "cannot verify graph_provenance absence on an empty list (S9)"
        )
        for i, nm in enumerate(near_misses):
            assert "graph_provenance" not in nm, (
                f"near_misses[{i}] must not have 'graph_provenance' field (S9); "
                f"present keys: {sorted(nm.keys())}"
            )
