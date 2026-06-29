"""E1a / T-2 — e2e: graph_mode=naive search recall + multi-collection fanout.

Scenarios covered:
- S4: POST /search with graph_mode=naive expands query; graph_expansion_applied=True;
  results contain doc2-unique text ("RS256") surfaced only through graph expansion.
- S7: Fanout search with graph_mode=naive applies expansion per-collection
  independently; both col1 (TokenValidator/RS256) and col2 (UserStore) results appear
  in the merged response.

Stub NLP design:
  - "AuthService" in text AND "UserStore" NOT in text → [AuthService(ORG), TokenValidator(ORG)]
    (simulates code-graph analysis discovering AuthService depends on TokenValidator)
  - "AuthService" in text AND "UserStore" IS in text → [AuthService(ORG), UserStore(ORG)]
    (simulates code-graph analysis discovering AuthService depends on UserStore)
  - only "TokenValidator" in text → [TokenValidator(ORG)]
  - only "UserStore" in text → [UserStore(ORG)]

The stub returns entities that may NOT be present in the chunk text; this simulates
static dependency analysis that surfaces relationships from external knowledge (e.g.,
import graphs) rather than co-occurrence within a single text window.
"""
from __future__ import annotations

import sys
import types
from pathlib import Path

import pytest

from tests.integration.conftest import ingest_file_via_path, make_real_app

pytestmark = pytest.mark.integration


def _auth(api_key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {api_key}"}


def _install_content_aware_spacy_stub(monkeypatch: pytest.MonkeyPatch) -> None:
    """Install a fake spaCy package that simulates code-graph entity extraction.

    Routing logic (order matters — AuthService check comes first):
    - "AuthService" in text AND "UserStore" NOT in text:
        → [AuthService(ORG), TokenValidator(ORG)]
        Simulates: col1 dependency graph (AuthService → TokenValidator)
    - "AuthService" in text AND "UserStore" IS in text:
        → [AuthService(ORG), UserStore(ORG)]
        Simulates: col2 dependency graph (AuthService → UserStore)
    - only "TokenValidator" in text:
        → [TokenValidator(ORG)]
    - only "UserStore" in text:
        → [UserStore(ORG)]

    Returning TokenValidator even when it is absent from the raw text is intentional:
    it simulates a static-analysis extractor that consults an import graph rather than
    NLP co-occurrence. The graph builder sees both entities in the same chunk → creates
    the AuthService ↔ TokenValidator edge.
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
                if "UserStore" in text:
                    # col2 scenario: AuthService depends on UserStore
                    ents.append(_FakeEnt("AuthService", "ORG"))
                    ents.append(_FakeEnt("UserStore", "ORG"))
                else:
                    # col1 / single-collection scenario: AuthService depends on TokenValidator
                    ents.append(_FakeEnt("AuthService", "ORG"))
                    ents.append(_FakeEnt("TokenValidator", "ORG"))
            elif "TokenValidator" in text:
                ents.append(_FakeEnt("TokenValidator", "ORG"))
            elif "UserStore" in text:
                ents.append(_FakeEnt("UserStore", "ORG"))
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


# ---------------------------------------------------------------------------
# T-2 e2e tests
# ---------------------------------------------------------------------------


def test_e2e_graph_naive_single_collection_recall(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Graph expansion surfaces a doc2-unique "RS256" chunk when searching "AuthService".

    Setup:
    - doc1: ONLY "AuthService" text (20 repetitions, no TokenValidator anywhere).
      Stub NLP: "AuthService" in text, "UserStore" not in text
      → returns [AuthService(ORG), TokenValidator(ORG)].
      Graph builder sees both entities in each chunk → creates AuthService↔TokenValidator edge.
    - doc2: ONLY "TokenValidator" + "RS256" text (20 repetitions, no AuthService).
      Stub NLP: "TokenValidator" in text → [TokenValidator(ORG)].
      Without graph expansion, a plain "AuthService" query would NOT rank doc2 highly
      because doc2 contains no "AuthService" text.

    With graph_mode=naive:
    - GraphExpander finds "AuthService" node, fetches its neighbour "TokenValidator".
    - Expanded query "AuthService TokenValidator" boosts FTS score for doc2.
    - doc2 chunks (containing "RS256") surface in results.
    - graph_expansion_applied=True in response.

    NOTE: primary assertion is graph_expansion_applied=True (proves the expansion
    code path ran). The "RS256" assertion is a secondary recall check driven by RRF.
    doc2 is a single short chunk ingested first (lower LanceDB row IDs → better
    vector rank with zero-vector stubs). doc1 is a 20-rep document producing ~10
    chunks ingested second. Without expansion, FTS for "AuthService" gives doc1
    chunks a high BM25 advantage (IDF is low because AuthService is common) and the
    vector tie-breaker favours doc1 via FTS rank; doc2 with no "AuthService" falls
    below top_k_return=5. With expansion to "AuthService TokenValidator", doc2's
    single chunk rises to RRF rank 0 (vec_rank=0 + fts_rank=0 for rare
    "TokenValidator") and appears in results. For true recall measurement, use the
    eval suite (BE-9 / tests/eval/).

    Covers S4: naive expansion improves recall on relationship-dense corpora.
    """
    _install_content_aware_spacy_stub(monkeypatch)

    col = "e1a-t2-single-recall"

    # doc2: short (single chunk) — ingested FIRST so it gets lower vector ranks.
    # ONLY "TokenValidator" + "RS256" — no AuthService text.
    # "TokenValidator" is rare (1 of ~11 chunks) → high BM25 IDF with expansion.
    # Without expansion, FTS for "AuthService" doesn't match → doc2 ranks low.
    doc2 = tmp_path / "token_validator_doc.txt"
    doc2.write_text(
        "TokenValidator validates JWT tokens using the RS256 asymmetric signing algorithm.\n",
        encoding="utf-8",
    )

    # doc1: long (20 reps, ~10 chunks) — ingested SECOND so it gets higher vector ranks.
    # ONLY "AuthService" — no TokenValidator text anywhere.
    # Stub extracts [AuthService, TokenValidator] per chunk, creating the edge.
    doc1 = tmp_path / "auth_service_doc.txt"
    doc1.write_text(
        "AuthService handles authentication in the system. "
        "AuthService is the primary security gateway for all requests. "
        "AuthService validates credentials and issues session tokens. "
        "AuthService is responsible for the full login and logout lifecycle.\n" * 20,
        encoding="utf-8",
    )

    with make_real_app(
        tmp_path,
        monkeypatch,
        graph_enabled=True,
        toml_content="[database]\nchunk_size = 128\n",
    ) as (client, _cfg, api_key):
        auth = _auth(api_key)

        # doc2 ingested first → lower LanceDB row IDs → better vector rank
        ingest_file_via_path(client, col, str(doc2), api_key=api_key)
        ingest_file_via_path(client, col, str(doc1), api_key=api_key)

        # Negative baseline: without graph_mode, "RS256" (doc2-unique) should NOT appear.
        # doc2 (1 chunk) has no "AuthService" so FTS for "AuthService" gives it 0 BM25.
        # doc1 chunks (10 of them, ingested after doc2) have "AuthService" → high FTS rank.
        # RRF: doc1[0] (vec=1, fts=0) ≈ 0.033 > doc2 (vec=0, no fts) ≈ 0.017 → doc2 not in top 5.
        resp_baseline = client.post(
            "/search",
            json={"collection": col, "query": "AuthService", "top_k": 5},
            headers=auth,
        )
        assert resp_baseline.status_code == 200, (
            f"Baseline /search failed: {resp_baseline.status_code} {resp_baseline.text}"
        )
        baseline_texts = [r["text"] for r in resp_baseline.json()["results"]]
        assert len(baseline_texts) > 0, (
            f"Baseline search returned no results — collection '{col}' may not have been ingested. "
            f"Full response: {resp_baseline.json()}"
        )
        assert not any("RS256" in t for t in baseline_texts), (
            "Negative baseline failed: 'RS256' appeared in plain search results (without "
            "graph_mode). This means chunk_size is too large — reduce it so doc2 chunks "
            f"stay out of top_k=5. Baseline texts (first 3): {baseline_texts[:3]}"
        )

        resp = client.post(
            "/search",
            json={
                "collection": col,
                "query": "AuthService",
                "graph_mode": "naive",
                "top_k": 10,
            },
            headers=auth,
        )
        assert resp.status_code == 200, (
            f"POST /search with graph_mode=naive failed: {resp.status_code} {resp.text}"
        )
        data = resp.json()

        # Primary assertion: expansion code path ran
        assert data["graph_expansion_applied"] is True, (
            f"Expected graph_expansion_applied=True; got {data.get('graph_expansion_applied')}. "
            f"Full response: {data}"
        )
        assert data["expansion_used"] is True, (
            f"Expected expansion_used=True; got {data.get('expansion_used')}. "
            f"Full response: {data}"
        )

        # Secondary recall assertion: "RS256" is doc2-unique.
        # It can only appear in results if graph expansion retrieved doc2 chunks
        # via the TokenValidator neighbour added to the expanded query.
        result_texts = [r["text"] for r in data["results"]]
        assert any("RS256" in t for t in result_texts), (
            "Expected at least one result containing doc2-unique 'RS256' after graph expansion; "
            f"got {len(result_texts)} results. "
            "This indicates graph expansion did not surface TokenValidator-tagged doc2 chunks. "
            f"Result texts (first 3): {result_texts[:3]}"
        )
        assert any("TokenValidator" in t for t in result_texts), (
            "Expected at least one result containing 'TokenValidator' after graph expansion; "
            f"got {len(result_texts)} results. Result texts (first 3): {result_texts[:3]}"
        )


def test_e2e_graph_naive_fanout_per_collection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Fanout search applies graph expansion per-collection independently.

    Setup:
    - col1:
        doc_auth1: ONLY "AuthService" text (20 reps).
          Stub: AuthService in text, UserStore NOT in text → [AuthService(ORG), TokenValidator(ORG)].
          Graph: AuthService↔TokenValidator edge.
        doc_tv: ONLY "TokenValidator" + "RS256" text (20 reps).
          Without col1 expansion, "AuthService" query would not retrieve this doc.
    - col2:
        doc_auth2: "AuthService" AND "UserStore" text (20 reps).
          Stub: AuthService in text, UserStore IS in text → [AuthService(ORG), UserStore(ORG)].
          Graph: AuthService↔UserStore edge.
        doc_us: ONLY "UserStore" + "cache" text (20 reps).
          Represents an expansion-surfaced UserStore document in col2.

    With fanout graph_mode=naive:
    - col1 expander: AuthService node → TokenValidator neighbour →
      expanded query retrieves doc_tv → "RS256" appears in merged results.
    - col2 expander: AuthService node → UserStore neighbour →
      expanded query boosts UserStore content in col2.
    - graph_expansion_applied=True in final merged response.

    NOTE: primary assertion is graph_expansion_applied=True. The content assertions
    verify that BOTH collection legs contributed expansion results:
    - "RS256" can only come from col1's doc_tv (TokenValidator expansion in col1).
    - "LRU" can only come from col2's doc_us (UserStore expansion in col2).

    RRF interleaving strategy: each collection has one SHORT doc (single chunk,
    ingested FIRST → low vector ranks) and one LONG doc (20 reps, ingested SECOND).
    With reranker disabled, the pipeline sorts all candidates globally by RRF score.
    The short docs (vec_rank=0, fts_rank=0 via rare-term IDF) reach RRF=1/60+1/60≈0.033,
    outranking the long docs (vec_rank=1, fts_rank=1 → 0.0328). Stable sort interleaves
    col1 and col2 by score:  doc_tv(col1,0.033), doc_auth2(col2,0.033),
    doc_auth1[0](col1,0.0328), doc_us[0](col2,0.0328), … → both RS256 and LRU land
    in the top 5. For true per-collection recall, use the eval suite (BE-9).

    Covers S7: multi-collection fanout applies expansion per-collection independently.
    """
    _install_content_aware_spacy_stub(monkeypatch)

    col1 = "e1a-t2-fanout-col1"
    col2 = "e1a-t2-fanout-col2"

    # col1 — TokenValidator document: SHORT (single chunk), ingested FIRST.
    # ONLY "TokenValidator" + "RS256" — no AuthService text.
    # "TokenValidator" is rare in col1 (1 of ~11 chunks) → high IDF → top FTS rank
    # for expanded query "AuthService TokenValidator". "RS256" is col1-unique.
    doc_tv = tmp_path / "col1_token_validator.txt"
    doc_tv.write_text(
        "TokenValidator validates JWT tokens using the RS256 asymmetric signing algorithm.\n",
        encoding="utf-8",
    )

    # col1 — AuthService document: LONG (20 reps, ~10 chunks), ingested SECOND.
    # ONLY "AuthService", no TokenValidator, no UserStore.
    # Stub: "AuthService" in text, "UserStore" NOT in text → [AuthService(ORG), TokenValidator(ORG)]
    # Graph wires AuthService↔TokenValidator edge for col1.
    doc_auth1 = tmp_path / "col1_auth_service.txt"
    doc_auth1.write_text(
        "AuthService handles authentication in the system. "
        "AuthService is the primary security gateway for all requests. "
        "AuthService validates credentials and issues session tokens. "
        "AuthService is responsible for the full login and logout lifecycle.\n" * 20,
        encoding="utf-8",
    )

    # col2 — AuthService+UserStore document: SHORT (single chunk), ingested FIRST.
    # Contains BOTH "AuthService" AND "UserStore".
    # Stub: "AuthService" in text, "UserStore" IS in text → [AuthService(ORG), UserStore(ORG)]
    # Graph wires AuthService↔UserStore edge for col2.
    # "AuthService" is rare in col2 (1 of ~11 chunks) → high IDF → top FTS rank.
    doc_auth2 = tmp_path / "col2_auth_service.txt"
    doc_auth2.write_text(
        "AuthService delegates session management to UserStore.\n",
        encoding="utf-8",
    )

    # col2 — UserStore document: LONG (20 reps, ~10 chunks), ingested SECOND.
    # ONLY "UserStore" + "LRU cache" — no AuthService text.
    # "LRU" is col2-unique (absent from col1 and doc_auth2). Retrieved when col2
    # expansion adds "UserStore" to the query and doc_us ranks via RRF.
    doc_us = tmp_path / "col2_user_store.txt"
    doc_us.write_text(
        "UserStore is the session persistence layer. "
        "It uses an LRU cache for high-throughput session lookups. "
        "UserStore cache entries expire after a configurable TTL. "
        "UserStore ensures low-latency session retrieval via in-memory cache.\n" * 20,
        encoding="utf-8",
    )

    with make_real_app(
        tmp_path,
        monkeypatch,
        graph_enabled=True,
        toml_content=(
            "[database]\nchunk_size = 128\nreranker_model = \"\"\n"
            "[search]\nmax_fanout = 5\n"
        ),
    ) as (client, _cfg, api_key):
        auth = _auth(api_key)

        # Per-collection ingest order: short "target" doc first (lower vector ranks),
        # long "source" doc second.  This gives the short docs vec_rank=0 within their
        # collection, which is decisive for RRF when the stub reranker is disabled.
        ingest_file_via_path(client, col1, str(doc_tv), api_key=api_key)
        ingest_file_via_path(client, col1, str(doc_auth1), api_key=api_key)
        ingest_file_via_path(client, col2, str(doc_auth2), api_key=api_key)
        ingest_file_via_path(client, col2, str(doc_us), api_key=api_key)

        # Negative baseline: without graph_mode, neither "RS256" nor "LRU" should appear.
        # doc_tv (1 chunk, no "AuthService") and doc_us chunks (no "AuthService") get 0 FTS
        # for the plain "AuthService" query. The RRF advantage of doc_auth2 (vec=0, fts=0
        # via rare "AuthService" IDF in col2) and doc_auth1 chunks puts them in top 5;
        # doc_tv and doc_us rank below top_k_return=5.
        resp_baseline = client.post(
            "/search",
            json={"collections": [col1, col2], "query": "AuthService", "top_k": 5},
            headers=auth,
        )
        assert resp_baseline.status_code == 200, (
            f"Baseline fanout /search failed: {resp_baseline.status_code} {resp_baseline.text}"
        )
        baseline_texts = [r["text"] for r in resp_baseline.json()["results"]]
        assert len(baseline_texts) > 0, (
            f"Baseline fanout search returned no results — collections may not have been ingested. "
            f"Full response: {resp_baseline.json()}"
        )
        assert not any("RS256" in t for t in baseline_texts), (
            "Negative baseline failed: 'RS256' appeared in plain fanout results (without "
            "graph_mode). Reduce chunk_size so doc_tv chunks stay out of top_k=5. "
            f"Baseline texts (first 3): {baseline_texts[:3]}"
        )
        assert not any("LRU" in t for t in baseline_texts), (
            "Negative baseline failed: 'LRU' appeared in plain fanout results (without "
            "graph_mode). Reduce chunk_size so doc_us chunks stay out of top_k=5. "
            f"Baseline texts (first 3): {baseline_texts[:3]}"
        )

        resp = client.post(
            "/search",
            json={
                "collections": [col1, col2],
                "query": "AuthService",
                "graph_mode": "naive",
                "top_k": 10,
            },
            headers=auth,
        )
        assert resp.status_code == 200, (
            f"Fanout POST /search with graph_mode=naive failed: {resp.status_code} {resp.text}"
        )
        data = resp.json()

        # Primary assertion: at least one collection leg applied expansion
        assert data["graph_expansion_applied"] is True, (
            f"Expected graph_expansion_applied=True in fanout response; "
            f"got {data.get('graph_expansion_applied')}. Full response: {data}"
        )
        assert data["expansion_used"] is True, (
            f"Expected expansion_used=True; got {data.get('expansion_used')}. "
            f"Full response: {data}"
        )

        # Results must be non-empty
        assert len(data["results"]) > 0, (
            f"Expected non-empty results from fanout search; got empty. Full response: {data}"
        )

        result_texts = [r["text"] for r in data["results"]]

        # Verify per-collection provenance: results from BOTH collections must be present
        result_collections = {r["collection"] for r in data["results"]}
        assert col1 in result_collections, (
            f"Expected results from col1 ({col1}) in merged fanout results; "
            f"got collections: {result_collections}"
        )
        assert col2 in result_collections, (
            f"Expected results from col2 ({col2}) in merged fanout results; "
            f"got collections: {result_collections}"
        )

        # col1 expansion check: "RS256" is col1-unique (only in doc_tv).
        # Its presence proves col1's TokenValidator expansion retrieved doc_tv.
        assert any("RS256" in t for t in result_texts), (
            "Expected 'RS256' from col1's TokenValidator expansion in merged results; "
            f"got {len(result_texts)} results. "
            f"Result texts (first 3): {result_texts[:3]}"
        )

        # col2 expansion check: "LRU" is doc_us-unique (only in doc_us, not in doc_auth2).
        # col2's graph expands "AuthService" → "UserStore", boosting doc_us chunks.
        # "LRU" proves doc_us was retrieved via UserStore expansion (not already in top_k).
        assert any("LRU" in t for t in result_texts), (
            "Expected 'LRU' from col2's UserStore expansion (doc_us) in merged fanout response; "
            f"got {len(result_texts)} results. "
            f"Result texts (first 3): {result_texts[:3]}"
        )
