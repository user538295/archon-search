"""E1c / T-3 — e2e tests: naive mode traversal provenance and mixed results.

Covers:
- (a) ``test_explain_naive_provenance_e2e`` — real deployed app post-E1a;
      ingest docs; POST /explain graph_mode="naive"; assert at least one result
      has non-null graph_provenance with a valid TraversalStep structure  (S2)
- (b) ``test_explain_naive_mixed_results_e2e`` — query that yields both graph
      and hybrid results; assert graph-retrieved items have graph_provenance,
      hybrid-only items have graph_provenance=null  (S7, S8)
- (c) ``test_mcp_explain_naive_provenance_e2e`` — MCP explain tool with
      graph_mode="naive"; result dict carries graph_mode_applied and provenance
      in result items with valid TraversalStep structure  (S12)

Corpus design for S2 and S12 (chunk_size=128, ~7 total chunks):
  doc1 — "AuthService" text repeated 20 times → ~6 chunks.
    Stub NLP: "AuthService" in text → [AuthService(ORG), TokenValidator(ORG)].
    Graph builder creates AuthService ↔ TokenValidator edge.

  doc2 — "TokenValidator validates RS256 tokens" (1 chunk).
    Stub NLP: "TokenValidator" in text → [TokenValidator(ORG)].

  With candidate_depth = max(top_k_retrieve × 3, 20) = 45 and only ~7 chunks,
  the expanded search "AuthService TokenValidator" retrieves ALL chunks.  All
  candidates get graph provenance.  Both tests only need ≥1 non-null result.

Corpus design for S7 (chunk_size=16, top_k_retrieve=3, ~60 total chunks):
  Same doc1 (30 reps → ~60 chunks) and doc2 (1 chunk) files, but with
  top_k_retrieve=3 → candidate_depth = max(9, 20) = 20.

  Expanded graph search "AuthService TokenValidator" (union of vec+FTS, ≤40):
    - doc2 (fts_rank=0: TokenValidator rare IDF; vec_rank=0: ingested first).
    - Top ~19 doc1 chunks (by FTS "AuthService" rank + vec rank).
    - All 20 candidates get graph provenance.

  Standard hybrid "AuthService" (union of vec+FTS, ≤40):
    - FTS top-20: doc1[0..19] (doc2 has no "AuthService" → BM25=0, absent).
    - Vec top-20: doc2(vec_rank=0) + doc1[0..18].
    - Union: doc2 + doc1[0..19] = 21 candidates.
    - Of these 21: doc2 + doc1[0..18] are in graph_by_chunk → dedup removes them.
    - doc1[19] is NOT in graph_by_chunk → added with graph_provenance=None.

  Merged total: ~20 graph (provenance) + several hybrid-only (null provenance) ≈ 30.
  top_k=80 ensures all merged candidates land in results[] (30 < 80);
  near_misses is empty.  The S7 test asserts `not near_misses` to enforce this.
"""
from __future__ import annotations

import json
import sys
import types
from pathlib import Path

import pytest

from tests.integration.conftest import ingest_file_via_path, make_real_app

pytestmark = pytest.mark.integration


# ---------------------------------------------------------------------------
# spaCy stub helpers
# ---------------------------------------------------------------------------


def _install_content_aware_spacy_stub(monkeypatch: pytest.MonkeyPatch) -> None:
    """Install a fake spaCy that extracts graph entities from ingested text.

    Routing logic:
    - "AuthService" in text AND "UserStore" NOT in text:
        → [AuthService(ORG), TokenValidator(ORG)]
        Simulates: AuthService depends on TokenValidator
    - "TokenValidator" in text (AuthService NOT in text):
        → [TokenValidator(ORG)]
    - otherwise: no entities

    Must be called BEFORE make_real_app because create_app calls
    ``_check_graph_deps`` which imports spacy synchronously.
    """

    class _FakeEnt:
        def __init__(self, text: str, label: str) -> None:
            self.text = text
            self.label_ = label

    class _FakeDoc:
        def __init__(self, ents: list[_FakeEnt]) -> None:
            self.ents = ents

    class _FakeNLP:
        def __call__(self, text: str) -> _FakeDoc:
            ents: list[_FakeEnt] = []
            if "AuthService" in text:
                if "UserStore" not in text:
                    ents.append(_FakeEnt("AuthService", "ORG"))
                    ents.append(_FakeEnt("TokenValidator", "ORG"))
            elif "TokenValidator" in text:
                ents.append(_FakeEnt("TokenValidator", "ORG"))
            return _FakeDoc(ents)

    nlp_instance = _FakeNLP()

    fake_util = types.ModuleType("spacy.util")
    fake_util.get_installed_models = lambda: ["en_core_web_sm"]  # type: ignore[attr-defined]
    fake_cli = types.ModuleType("spacy.cli")
    fake_cli.download = lambda model: None  # type: ignore[attr-defined]
    fake_spacy = types.ModuleType("spacy")
    fake_spacy.load = lambda model: nlp_instance  # type: ignore[attr-defined]
    fake_spacy.util = fake_util  # type: ignore[attr-defined]
    fake_spacy.cli = fake_cli  # type: ignore[attr-defined]

    monkeypatch.setitem(sys.modules, "spacy", fake_spacy)
    monkeypatch.setitem(sys.modules, "spacy.util", fake_util)
    monkeypatch.setitem(sys.modules, "spacy.cli", fake_cli)


def _install_deterministic_embedding(monkeypatch: pytest.MonkeyPatch) -> None:
    """Replace the zero-vector fastembed stub with a deterministic, content-aware one.

    The shared stub (tests/_search_stubs.py) yields all-zero vectors, so every
    chunk's vector score ties. The S7 assertion depends on a knife-edge boundary
    (one chunk in the plain-query top-k but not the expanded-query top-k), and
    which tied chunks fill each top-k is decided by LanceDB's tied-row order —
    which lancedb 0.36 no longer keeps stable, making this test ~40% flaky.

    This stub keeps determinism WITHOUT masking anything: cosine similarity
    tracks two concept axes (AuthService, TokenValidator) exactly as a real
    embedder would separate these documents, and a tiny per-text hash jitter
    (<= 0.01, far below the concept axes) breaks exact ties so retrieval order is
    stable across runs. monkeypatch auto-reverts, so no other test is affected.
    """
    import hashlib

    import numpy as np

    def _embed(self: object, texts: list[str]):  # type: ignore[no-untyped-def]
        for t in texts:
            v = np.zeros(384, dtype=np.float32)
            if "AuthService" in t:
                v[0] = 1.0
            if "TokenValidator" in t:
                v[1] = 1.0
            h = int.from_bytes(hashlib.sha256(t.encode()).digest()[:4], "big")
            v[2] = (h % 10_000) / 1_000_000.0  # deterministic tie-break, <= 0.01
            yield v

    monkeypatch.setattr(sys.modules["fastembed"].TextEmbedding, "embed", _embed)


def _auth(api_key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {api_key}"}


def _assert_valid_first_traversal_step(prov: dict, expected_entity: str = "AuthService") -> None:
    """Assert that a graph_provenance dict contains a valid first TraversalStep.

    Checks: prov is dict with non-empty 'steps' list; first step has
    non-empty 'entity' matching expected_entity, non-empty 'entity_id', and at
    least one of relationship/community_id/chunk_id set.
    """
    assert isinstance(prov, dict), (
        f"Expected graph_provenance to be a dict; got {type(prov).__name__!r}: {prov!r}"
    )
    assert "steps" in prov, (
        f"Expected 'steps' key in graph_provenance; got: {list(prov.keys())}"
    )
    steps = prov["steps"]
    assert isinstance(steps, list), (
        f"Expected steps to be a list; got {type(steps).__name__!r}"
    )
    assert steps, (
        "Expected non-empty steps list in graph_provenance"
    )
    step = steps[0]
    assert "entity" in step, (
        f"TraversalStep missing 'entity' field; got: {list(step.keys())}"
    )
    assert step["entity"], (
        f"TraversalStep.entity must be non-empty; got: {step['entity']!r}"
    )
    assert "entity_id" in step, (
        f"TraversalStep missing 'entity_id' field; got: {list(step.keys())}"
    )
    assert step["entity_id"], (
        f"TraversalStep.entity_id must be non-empty; got: {step['entity_id']!r}"
    )
    assert step["entity"] == expected_entity, (
        f"Expected TraversalStep.entity={expected_entity!r} (the query-matched entity); "
        f"got {step['entity']!r}"
    )
    assert (
        step.get("relationship") is not None
        or step.get("community_id") is not None
        or step.get("chunk_id") is not None
    ), (
        f"TraversalStep must have at least one of relationship/community_id/chunk_id set; "
        f"got: {step!r}"
    )


# ---------------------------------------------------------------------------
# MCP helpers (same pattern as test_e1b_t3_e2e_status_mcp.py)
# ---------------------------------------------------------------------------


def _mcp_headers(token: str, session_id: str | None = None) -> dict:
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    }
    if session_id is not None:
        headers["mcp-session-id"] = session_id
    return headers


def _mcp_initialize(client, token: str) -> str:
    """Send MCP initialize + notifications/initialized; return session_id."""
    resp = client.post(
        "/mcp",
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "t3-explain-test", "version": "1.0"},
            },
        },
        headers=_mcp_headers(token),
    )
    assert resp.status_code == 200, (
        f"MCP initialize failed: {resp.status_code} {resp.text[:300]}"
    )
    session_id = resp.headers.get("mcp-session-id")
    assert session_id, "MCP initialize must return mcp-session-id header"
    client.post(
        "/mcp",
        json={"jsonrpc": "2.0", "method": "notifications/initialized"},
        headers=_mcp_headers(token, session_id),
    )
    return session_id


def _mcp_call_tool(client, token: str, session_id: str, tool_name: str, arguments: dict) -> dict:
    """Call an MCP tool and return the parsed SSE result payload."""
    resp = client.post(
        "/mcp",
        json={
            "jsonrpc": "2.0",
            "id": 99,
            "method": "tools/call",
            "params": {"name": tool_name, "arguments": arguments},
        },
        headers=_mcp_headers(token, session_id),
    )
    assert resp.status_code == 200, (
        f"MCP tools/call ({tool_name}) failed: {resp.status_code} {resp.text[:300]}"
    )
    data_lines = [
        line[5:].strip()
        for line in resp.text.split("\n")
        if line.startswith("data:")
    ]
    assert data_lines, (
        f"No data: line in SSE response for {tool_name}: {resp.text[:300]!r}"
    )
    return json.loads(data_lines[-1])


def _extract_tool_text(result: dict, tool_name: str) -> dict:
    """Extract and parse the JSON text from an MCP tool response."""
    assert result, f"Tool '{tool_name}' returned empty result dict"
    rpc_result = result.get("result")
    assert rpc_result is not None, (
        f"Tool '{tool_name}' RPC result missing 'result' key: {result!r}"
    )
    assert not rpc_result.get("isError"), (
        f"Tool '{tool_name}' returned isError=True (unhandled exception): {rpc_result!r}"
    )
    content = rpc_result.get("content", [])
    assert content, f"Tool '{tool_name}' returned empty content list: {rpc_result!r}"
    text = content[0].get("text", "")
    assert text, f"Tool '{tool_name}' returned empty text: {content!r}"
    return json.loads(text)


# ---------------------------------------------------------------------------
# Corpus builders
# ---------------------------------------------------------------------------


def _build_corpus(tmp_path: Path) -> tuple[Path, Path]:
    """Create doc1 (AuthService) and doc2 (TokenValidator+RS256) in tmp_path.

    Used for S2 (chunk_size=128) and S12 (chunk_size=128) tests.
    Total: ~7 chunks (6 doc1 + 1 doc2), well below candidate_depth=45.
    Both the expanded "AuthService TokenValidator" search and the standard
    "AuthService" hybrid search retrieve ALL ~7 chunks — so all candidates
    get graph provenance.  S2 and S12 only assert ≥1 non-null provenance result.
    """
    # doc1: ONLY "AuthService" text — 20 repetitions so ~6 chunks (chunk_size=128).
    # Stub NLP extracts [AuthService, TokenValidator] → graph builds A↔TV edge.
    doc1 = tmp_path / "auth_service_doc.txt"
    doc1.write_text(
        "AuthService handles authentication in the system. "
        "AuthService is the primary security gateway for all requests. "
        "AuthService validates credentials and issues session tokens. "
        "AuthService is responsible for the full login and logout lifecycle.\n" * 20,
        encoding="utf-8",
    )

    # doc2: ONLY "TokenValidator" + "RS256" — single short line (1 chunk).
    # Stub NLP extracts [TokenValidator] only.
    # Not matched by plain "AuthService" FTS query; reachable via graph expansion.
    doc2 = tmp_path / "token_validator_doc.txt"
    doc2.write_text(
        "TokenValidator validates JWT tokens using the RS256 asymmetric signing algorithm.\n",
        encoding="utf-8",
    )
    return doc1, doc2


def _build_large_corpus_for_s7(tmp_path: Path) -> tuple[Path, Path, Path]:
    """Build a corpus whose mixed graph/hybrid results are DETERMINISTIC.

    top_k_retrieve=10 (pinned in toml) → candidate_depth = max(10×3, 20) = 30.

    The S7 intent is a response containing BOTH graph-provenance results and a
    hybrid-only (null-provenance) result. That requires one document the plain
    query retrieves but the graph-EXPANDED query ranks below candidate_depth.
    With the deterministic content-aware embedder (_install_deterministic_embedding),
    similarity tracks two axes — AuthService (dim 0), TokenValidator (dim 1):

    - doc1: 80 DISTINCT 16-word chunks, each carrying BOTH "AuthService" and
      "TokenValidator" (+ a unique "segmentNNN" marker → distinct vectors) →
      vector ≈ [1, 1]. These are the graph-provenance results.
    - doc3: a single chunk with ONLY "AuthService" → vector ≈ [1, 0].
    - doc2: a single "TokenValidator"-only chunk → vector ≈ [0, 1] (the doc the
      AuthService↔TokenValidator graph edge points at).

    Plain query "AuthService" (vector [1, 0]):
      - doc3 [1,0] is the closest match; doc1 chunks [1,1] follow; doc2 [0,1] is
        far. Plain top-20 = doc3 + 19 doc1 chunks.
    Graph-expanded query "AuthService TokenValidator" (vector [1, 1]):
      - the 30 doc1 chunks [1,1] are the closest (they fill all 20 slots); doc3
        [1,0] and doc2 [0,1] are strictly farther → doc3 is NOT in the graph set.

    Merge: 20 doc1 chunks (graph provenance) + doc3 (hybrid-only, null
    provenance). doc3's exclusion from the graph set is a full concept-axis gap
    (not a tie-break), so the null-provenance leftover is stable across runs.
    top_k=80 ensures all merged candidates land in results[]; near_misses empty.
    """
    doc1 = tmp_path / "auth_service_large_doc.txt"
    # 80 distinct 16-word chunks, each with BOTH concept terms + a unique marker.
    doc1.write_text(
        " ".join(
            f"AuthService TokenValidator segment{i:03d} handles authentication for the "
            f"security gateway validating credentials issuing session tokens login"
            for i in range(80)
        ),
        encoding="utf-8",
    )

    # doc2: single TokenValidator-only chunk (graph edge target).
    doc2 = tmp_path / "token_validator_doc.txt"
    doc2.write_text(
        "TokenValidator validates JWT tokens using the RS256 asymmetric signing algorithm.\n",
        encoding="utf-8",
    )

    # doc3: single AuthService-only chunk — the deterministic hybrid-only leftover.
    # No "TokenValidator" → the expanded query ranks it below all 30 doc1 chunks.
    doc3 = tmp_path / "auth_service_only_doc.txt"
    doc3.write_text(
        "AuthService leftoveronly handles authentication credentials session tokens "
        "login logout lifecycle gateway primary.\n",
        encoding="utf-8",
    )
    return doc1, doc2, doc3


# ---------------------------------------------------------------------------
# (a) test_explain_naive_provenance_e2e  (S2)
# ---------------------------------------------------------------------------


def test_explain_naive_provenance_e2e(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """POST /explain graph_mode="naive" → at least one result carries non-null
    graph_provenance with a valid TraversalStep  (S2).

    Setup:
    - doc1: AuthService text (creates AuthService↔TokenValidator graph edge via stub NLP).
    - doc2: TokenValidator+RS256 text (single chunk, reachable via graph expansion).
    - Query: "AuthService" with graph_mode="naive".

    Expected: _explain_naive_graph_candidates finds AuthService node, follows the
    AuthService→TokenValidator edge, expands the query, retrieves doc2 chunk(s),
    attaches GraphProvenance(steps=[TraversalStep(entity="AuthService", ...)])
    to each graph-retrieved candidate.
    """
    _install_content_aware_spacy_stub(monkeypatch)

    col = "t3-s2-naive-provenance"
    doc1, doc2 = _build_corpus(tmp_path)

    with make_real_app(
        tmp_path,
        monkeypatch,
        graph_enabled=True,
        toml_content="[database]\nchunk_size = 128\n",
    ) as (client, _cfg, api_key):
        auth = _auth(api_key)

        # doc2 ingested first → lower LanceDB row IDs → better vector rank under stubs.
        ingest_file_via_path(client, col, str(doc2), api_key=api_key)
        ingest_file_via_path(client, col, str(doc1), api_key=api_key)

        resp = client.post(
            "/explain",
            json={
                "query": "AuthService",
                "collection": col,
                "graph_mode": "naive",
                "top_k": 10,
            },
            headers=auth,
        )

        assert resp.status_code == 200, (
            f"POST /explain graph_mode='naive' failed: {resp.status_code} {resp.text}"
        )
        body = resp.json()

        # Response-level assertion: graph_mode_applied must be "naive" (S2)
        assert body.get("graph_mode_applied") == "naive", (
            f"Expected graph_mode_applied='naive'; got {body.get('graph_mode_applied')!r}. "
            f"Full response keys: {list(body.keys())}"
        )

        results = body.get("results", [])
        assert results, (
            "Expected non-empty results after ingest; "
            "cannot verify graph_provenance on an empty list"
        )

        # Primary assertion (S2): at least one result must have non-null graph_provenance
        # with a valid TraversalStep (entity + entity_id + at least one of
        # relationship/community_id/chunk_id set).
        graph_results = [r for r in results if r.get("graph_provenance") is not None]
        assert graph_results, (
            "Expected at least one result with non-null graph_provenance after naive "
            "graph traversal; got zero. This means either the graph edge was not "
            "created during ingest or the entity matching in _explain_naive_graph_candidates "
            f"failed. Results: {[{k: v for k, v in r.items() if k != 'text'} for r in results]}"
        )

        # Validate TraversalStep structure on first graph result (S2)
        first_graph_result = graph_results[0]
        _assert_valid_first_traversal_step(first_graph_result["graph_provenance"])


# ---------------------------------------------------------------------------
# (b) test_explain_naive_mixed_results_e2e  (S7)
# ---------------------------------------------------------------------------


def test_explain_naive_mixed_results_e2e(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """POST /explain graph_mode="naive": mixed results — graph-retrieved chunks
    carry graph_provenance, hybrid-only chunks carry graph_provenance=null  (S7).
    Also verifies chunk-id uniqueness (dedup: graph provenance wins — S8).

    Design: top_k_retrieve=10 pins candidate_depth = max(30, 20) = 30.
    doc1 has ~80 chunks (80 reps, chunk_size=16) → 82 total chunks far
    exceed candidate_depth, so doc3 falls outside the expanded graph search.

    Expanded graph search "AuthService TokenValidator" (union of vec+FTS top-30):
      - ~30 doc1 chunks (both terms, closest vecs) with graph provenance.
      - doc3 ranks 31st+ in both vec and FTS (only "AuthService") → excluded.

    Standard hybrid "AuthService" (union of vec+FTS top-30):
      - doc3 (closest vec match to [1,0,0]) + doc1 chunks.
      - doc3 is NOT in graph_by_chunk → added with graph_provenance=None.

    Merged: ~30 graph-provenance + doc3 null-provenance + non-graph doc1
    chunks from standard search ≈ 60+ total.
    S489 pool truncation: merged[:top_k_retrieve] = 10 candidates.
    doc3's standard-search RRF (vec_rank=1) outranks the 10th graph
    candidate, so it survives truncation into the top 10.
    top_k=80 ensures all 10 fit in results[]; near_misses empty.

    S7: both provenance and null-provenance appear in results[].
    S8: no duplicate chunk_ids (graph provenance wins in dedup).
    """
    _install_content_aware_spacy_stub(monkeypatch)
    _install_deterministic_embedding(monkeypatch)

    col = "t3-s7-mixed-results"
    doc1, doc2, doc3 = _build_large_corpus_for_s7(tmp_path)

    with make_real_app(
        tmp_path,
        monkeypatch,
        graph_enabled=True,
        # chunk_size=16 → 80 doc1 chunks; top_k_retrieve=10 → candidate_depth=30
        # so doc3 falls outside graph search (82 total >> 30), and doc3's
        # standard-search RRF outranks the 10th graph candidate.
        toml_content="[database]\nchunk_size = 16\ntop_k_retrieve = 10\n",
    ) as (client, _cfg, api_key):
        auth = _auth(api_key)

        ingest_file_via_path(client, col, str(doc2), api_key=api_key)
        ingest_file_via_path(client, col, str(doc1), api_key=api_key)
        ingest_file_via_path(client, col, str(doc3), api_key=api_key)

        resp = client.post(
            "/explain",
            json={
                "query": "AuthService",
                "collection": col,
                "graph_mode": "naive",
                # top_k=80 (theoretical max: 2 searches × union-of-40 each) ensures all
                # merged candidates land in results[]; near_misses must be empty.
                "top_k": 80,
            },
            headers=auth,
        )

        assert resp.status_code == 200, (
            f"POST /explain graph_mode='naive' failed: {resp.status_code} {resp.text}"
        )
        body = resp.json()

        assert body.get("graph_mode_applied") == "naive", (
            f"Expected graph_mode_applied='naive'; got {body.get('graph_mode_applied')!r}"
        )

        results = body.get("results", [])
        near_misses = body.get("near_misses", [])

        # Guard: near_misses must be empty — all merged candidates should fit in results[]
        # (top_k=80 covers the theoretical worst-case merged count).  ExplainNearMiss has
        # no graph_provenance field, so mixing near_misses into the provenance check would
        # create vacuous assertions.
        assert not near_misses, (
            f"Expected near_misses to be empty (all merged candidates fit in top_k=80); "
            f"got {len(near_misses)} near-miss items. This means the merged candidate count "
            f"exceeds top_k=80 — increase top_k or reduce corpus size. "
            f"results count: {len(results)}"
        )

        assert results, (
            "Expected non-empty results after ingest"
        )

        # S7: graph-retrieved results must carry non-null graph_provenance.
        graph_results = [r for r in results if r.get("graph_provenance") is not None]
        assert graph_results, (
            "Expected at least one result with non-null graph_provenance (graph path); "
            f"got zero. Total results: {len(results)}. "
            f"Sample: {[{k: v for k, v in r.items() if k in ('chunk_id', 'graph_provenance')} for r in results[:3]]}"
        )

        # S7: at minimum 3 graph-provenance results must be present (catches partition collapse).
        # With ~20 graph candidates expected, a collapse to exactly 1 would indicate a
        # merge-path regression.
        assert len(graph_results) >= 3, (
            f"Expected at least 3 graph-provenance results in S7 mixed-results corpus; "
            f"got {len(graph_results)}. This may indicate a merge-path regression."
        )

        # S7: validate TraversalStep structure on first graph result (merge-path coverage).
        # S7 is the only test that exercises the full merge path; checking structure here
        # catches provenance corruption that S2/S12 (all-graph path) would miss.
        _assert_valid_first_traversal_step(graph_results[0]["graph_provenance"])

        # S7: hybrid-only results must carry null graph_provenance.
        # doc3 (AuthService-only) is retrieved by the plain query but ranks below all
        # 30 doc1 chunks in the expanded "AuthService TokenValidator" search, so it is
        # NOT in graph_by_chunk → appears with graph_provenance=None. This exclusion is
        # a full concept-axis gap, not a tie-break, so it is stable across runs.
        # Checking results[] only (not near_misses): ExplainResult always has the
        # graph_provenance field (null when the candidate came from the hybrid-only path).
        null_prov_results = [r for r in results if r.get("graph_provenance") is None]
        assert null_prov_results, (
            "Expected at least one result with graph_provenance=null (hybrid-only path); "
            f"got zero. Total results: {len(results)}. "
            "This means doc3 (AuthService-only) was covered by the expanded graph search — "
            "check the deterministic embedder ranks doc3 below all doc1 chunks for the "
            "expanded query. "
            f"Sample: {[{k: v for k, v in r.items() if k in ('chunk_id', 'graph_provenance')} for r in results[:3]]}"
        )

        # S8: no duplicate chunk_ids — graph provenance wins in dedup.
        # When a chunk appears in both graph and hybrid candidates, the merge logic
        # keeps the graph version (with provenance) and drops the hybrid duplicate.
        chunk_ids = [r["chunk_id"] for r in results]
        assert len(set(chunk_ids)) == len(chunk_ids), (
            "Duplicate chunk_ids in results — the S8 dedup invariant (graph provenance wins) "
            f"is violated. Duplicates: {[cid for cid in chunk_ids if chunk_ids.count(cid) > 1]}"
        )


# ---------------------------------------------------------------------------
# (c) test_mcp_explain_naive_provenance_e2e  (S12)
# ---------------------------------------------------------------------------


@pytest.mark.xdist_group("mcp")
def test_mcp_explain_naive_provenance_e2e(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """MCP explain tool with graph_mode="naive" → result dict carries
    graph_mode_applied and provenance in result items  (S12).

    Uses the same corpus as S2/S7: doc1 (AuthService, many chunks) + doc2
    (TokenValidator+RS256, one chunk).  The MCP explain tool is called with
    graph_mode="naive"; the response dict (ExplainResponse serialised) must
    contain graph_mode_applied="naive" and at least one result item with
    non-null graph_provenance.
    """
    _install_content_aware_spacy_stub(monkeypatch)

    col = "t3-s12-mcp-explain-naive"
    doc1, doc2 = _build_corpus(tmp_path)

    with make_real_app(
        tmp_path,
        monkeypatch,
        graph_enabled=True,
        mcp_enabled=True,
        toml_content="[database]\nchunk_size = 128\n",
    ) as (client, _cfg, api_key):
        # Ingest corpus.
        ingest_file_via_path(client, col, str(doc2), api_key=api_key)
        ingest_file_via_path(client, col, str(doc1), api_key=api_key)

        # MCP handshake.
        session_id = _mcp_initialize(client, api_key)

        # Call MCP explain tool with graph_mode="naive".
        raw = _mcp_call_tool(
            client,
            api_key,
            session_id,
            "explain",
            {
                "query": "AuthService",
                "collection": col,
                "graph_mode": "naive",
                "top_k": 10,
            },
        )
        parsed = _extract_tool_text(raw, "explain")

        # Result dict must be an ExplainResponse serialisation.
        assert isinstance(parsed, dict), (
            f"Expected dict from MCP explain; got {type(parsed).__name__!r}: {parsed!r}"
        )
        assert "error" not in parsed, (
            f"Unexpected error in MCP explain result: {parsed!r}"
        )

        # graph_mode_applied must be "naive" (S12)
        assert parsed.get("graph_mode_applied") == "naive", (
            f"Expected graph_mode_applied='naive' in MCP explain result; "
            f"got {parsed.get('graph_mode_applied')!r}. "
            f"Response keys: {list(parsed.keys())}"
        )

        # At least one result item must carry non-null graph_provenance (S12)
        results = parsed.get("results", [])
        assert results, (
            "Expected non-empty results in MCP explain response"
        )
        graph_results = [r for r in results if r.get("graph_provenance") is not None]
        assert graph_results, (
            "Expected at least one result item with non-null graph_provenance from "
            f"MCP explain with graph_mode='naive'; got zero. "
            f"Results: {[{k: v for k, v in r.items() if k in ('chunk_id', 'graph_provenance')} for r in results]}"
        )

        # Validate TraversalStep structure on first graph result (S12 parity with S2)
        first_graph_result = graph_results[0]
        _assert_valid_first_traversal_step(first_graph_result["graph_provenance"])
