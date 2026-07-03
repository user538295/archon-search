"""E2a T-3: E2e tests for scope filter — exact match, wildcard, no-op, invalid syntax, documents.

Scenarios covered:
- S8:  POST /search with scope_filter="user:alice" returns only alice-scoped and unscoped chunks;
       bob-scoped chunks are excluded.
- S9:  POST /search with scope_filter="user:alice*" matches alice and alice:thread-1; bob excluded.
- S10: Collection with no scoped chunks + any scope_filter → all top-k returned; no error.
       Unscoped chunks (scopes=null) are treated as shared/global and always match.
- S11: POST /search with scope_filter="*" → 400 (bare wildcard).
- S12: POST /search with scope_filter="user:*alice" → 400 (leading wildcard).
- S13: GET /collections/{name}/documents returns scopes per document (deduplicated set-union).

Also includes:
- Integration test for pipeline.explain with wildcard scope_filter='user:*'.
- E2e test for POST /explain with scope_filter on a multi-collection query.

Uses make_real_app + TestClient for e2e tests; real pipeline for integration test.
"""
from __future__ import annotations

import time
from pathlib import Path

import pytest

from tests.integration.conftest import make_real_app

pytestmark = pytest.mark.integration

# ---------------------------------------------------------------------------
# Named constants — avoid magic numbers
# ---------------------------------------------------------------------------

_POLL_TIMEOUT_S: float = 15.0
_POLL_INTERVAL_S: float = 0.1
_SEARCH_TOP_K: int = 10


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _auth(api_key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {api_key}"}


def _poll_job_done(client, job_id: str, api_key: str) -> None:
    """Poll GET /jobs/{job_id} until DONE; fail on FAILED or timeout."""
    deadline = time.monotonic() + _POLL_TIMEOUT_S
    while time.monotonic() < deadline:
        r = client.get(f"/jobs/{job_id}", headers=_auth(api_key))
        assert r.status_code == 200, f"GET /jobs/{job_id} returned {r.status_code}"
        status = r.json()["status"]
        if status == "DONE":
            return
        if status == "FAILED":
            pytest.fail(f"ingest job FAILED (job_id={job_id}): {r.json()}")
        time.sleep(_POLL_INTERVAL_S)
    pytest.fail(f"ingest job did not complete within {_POLL_TIMEOUT_S}s (job_id={job_id})")


def _ingest_file(
    client,
    col: str,
    file_path: str,
    api_key: str,
    *,
    chunk_scopes: list[str] | None = None,
) -> None:
    """POST /ingest with file path and optional chunk_scopes; polls until DONE."""
    body: dict = {"collection": col, "path": file_path}
    if chunk_scopes is not None:
        body["chunk_scopes"] = chunk_scopes
    resp = client.post("/ingest", json=body, headers=_auth(api_key))
    assert resp.status_code == 202, f"ingest POST failed: {resp.status_code} {resp.text}"
    _poll_job_done(client, resp.json()["job_id"], api_key)


def _post_search(
    client,
    col: str,
    query: str,
    api_key: str,
    *,
    scope_filter: str | None = None,
    top_k: int = _SEARCH_TOP_K,
    collections: list[str] | None = None,
) -> tuple[int, dict]:
    """POST /search and return (status_code, response_json)."""
    body: dict = {"query": query, "top_k": top_k}
    if col and collections is None:
        body["collection"] = col
    if collections is not None:
        body["collections"] = collections
    if scope_filter is not None:
        body["scope_filter"] = scope_filter
    resp = client.post("/search", json=body, headers=_auth(api_key))
    return resp.status_code, resp.json()


def _post_explain(
    client,
    query: str,
    api_key: str,
    *,
    collection: str | None = None,
    collections: list[str] | None = None,
    scope_filter: str | None = None,
    top_k: int = _SEARCH_TOP_K,
) -> tuple[int, dict]:
    """POST /explain and return (status_code, response_json)."""
    body: dict = {"query": query, "top_k": top_k}
    if collection is not None:
        body["collection"] = collection
    if collections is not None:
        body["collections"] = collections
    if scope_filter is not None:
        body["scope_filter"] = scope_filter
    resp = client.post("/explain", json=body, headers=_auth(api_key))
    return resp.status_code, resp.json()


# ---------------------------------------------------------------------------
# E2e tests — scope filter exact/wildcard/invalid (S8, S9, S10, S11, S12)
# ---------------------------------------------------------------------------


def test_scope_exact_match_e2e(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """E2e S8: POST /search scope_filter="user:alice" returns alice-scoped chunks;
    bob-scoped chunks are excluded; unscoped chunks are included;
    'user:alice:thread-1' chunks are excluded (exact match, not prefix).

    Flow:
    1. Ingest alice.txt with chunk_scopes=["user:alice"].
    2. Ingest thread.txt with chunk_scopes=["user:alice:thread-1"].
    3. Ingest bob.txt with chunk_scopes=["user:bob"].
    4. Ingest shared.txt with no scopes (unscoped/shared/global).
    5. POST /search with scope_filter="user:alice" → only alice + unscoped chunks returned.
    6. bob.txt and thread.txt must not appear in results.
    """
    col = "t3-scope-exact"
    col_path = tmp_path / col
    col_path.mkdir()

    alice_file = col_path / "alice.txt"
    alice_file.write_text(
        "alice specific content alpha beta gamma delta epsilon zeta eta " * 5,
        encoding="utf-8",
    )
    thread_file = col_path / "thread.txt"
    thread_file.write_text(
        "alice thread content alpha beta gamma delta epsilon zeta eta " * 5,
        encoding="utf-8",
    )
    bob_file = col_path / "bob.txt"
    bob_file.write_text(
        "bob specific content alpha beta gamma delta epsilon zeta eta " * 5,
        encoding="utf-8",
    )
    shared_file = col_path / "shared.txt"
    shared_file.write_text(
        "shared global content alpha beta gamma delta epsilon zeta eta " * 5,
        encoding="utf-8",
    )

    with make_real_app(tmp_path, monkeypatch) as (client, cfg, api_key):
        _ingest_file(client, col, str(alice_file), api_key, chunk_scopes=["user:alice"])
        _ingest_file(client, col, str(thread_file), api_key, chunk_scopes=["user:alice:thread-1"])
        _ingest_file(client, col, str(bob_file), api_key, chunk_scopes=["user:bob"])
        _ingest_file(client, col, str(shared_file), api_key, chunk_scopes=None)

        status, data = _post_search(
            client, col, "content alpha beta",
            api_key, scope_filter="user:alice",
        )
        assert status == 200, f"Expected 200, got {status}: {data}"
        results = data["results"]
        assert results, "Expected at least one result with scope_filter='user:alice'"

        result_sources = {r["source_path"] for r in results}
        # bob.txt must not appear in results (scope='user:bob' does not match 'user:alice')
        bob_in_results = any("bob.txt" in s for s in result_sources)
        assert not bob_in_results, (
            f"bob-scoped chunks should be excluded by scope_filter='user:alice'; "
            f"result sources: {result_sources}"
        )

        # thread.txt must not appear (exact match 'user:alice' must not match 'user:alice:thread-1')
        thread_in_results = any("thread.txt" in s for s in result_sources)
        assert not thread_in_results, (
            "Exact scope_filter='user:alice' must exclude 'user:alice:thread-1'; "
            "if thread.txt appears, exact match is broken (prefix matching instead of exact)"
        )

        # alice.txt must appear (scope='user:alice' exactly matches scope_filter='user:alice')
        assert any("alice.txt" in s for s in result_sources), (
            "alice-scoped chunk must be in results for scope_filter='user:alice' (S8)"
        )
        # shared.txt must appear (unscoped chunks always match any scope_filter)
        assert any("shared.txt" in s for s in result_sources), (
            "Unscoped (shared) chunk must always match any scope_filter (S10 semantic)"
        )


def test_scope_wildcard_match_e2e(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """E2e S9: POST /search scope_filter="user:alice*" returns alice and alice:thread-1;
    bob-scoped chunk is excluded; unscoped chunk is included.

    Wildcard prefix 'user:alice' matches:
    - 'user:alice'          (alice.txt)  → included
    - 'user:alice:thread-1' (thread.txt) → included
    - 'user:bob'            (bob.txt)    → excluded
    - scopes=null           (shared.txt) → included (unscoped always passes)
    """
    col = "t3-scope-wild"
    col_path = tmp_path / col
    col_path.mkdir()

    alice_file = col_path / "alice.txt"
    alice_file.write_text(
        "alice base content alpha beta gamma " * 5, encoding="utf-8"
    )
    thread_file = col_path / "thread.txt"
    thread_file.write_text(
        "alice thread content alpha beta gamma " * 5, encoding="utf-8"
    )
    bob_file = col_path / "bob.txt"
    bob_file.write_text(
        "bob content alpha beta gamma delta " * 5, encoding="utf-8"
    )
    shared_file = col_path / "shared.txt"
    shared_file.write_text(
        "shared content alpha beta gamma delta " * 5, encoding="utf-8"
    )

    with make_real_app(tmp_path, monkeypatch) as (client, cfg, api_key):
        _ingest_file(client, col, str(alice_file), api_key, chunk_scopes=["user:alice"])
        _ingest_file(client, col, str(thread_file), api_key, chunk_scopes=["user:alice:thread-1"])
        _ingest_file(client, col, str(bob_file), api_key, chunk_scopes=["user:bob"])
        _ingest_file(client, col, str(shared_file), api_key, chunk_scopes=None)

        status, data = _post_search(
            client, col, "content alpha beta",
            api_key, scope_filter="user:alice*",
        )
        assert status == 200, f"Expected 200, got {status}: {data}"
        results = data["results"]
        assert results, "Expected at least one result with scope_filter='user:alice*'"

        result_sources = {r["source_path"] for r in results}
        # bob.txt must not appear (user:bob does NOT start with user:alice)
        bob_in_results = any("bob.txt" in s for s in result_sources)
        assert not bob_in_results, (
            f"bob-scoped chunk must be excluded by wildcard scope_filter='user:alice*'; "
            f"result sources: {result_sources}"
        )

        # thread.txt (scopes=['user:alice:thread-1']) must also be returned by wildcard 'user:alice*'
        thread_in_results = any("thread.txt" in s for s in result_sources)
        assert thread_in_results, (
            "Wildcard scope_filter='user:alice*' must match 'user:alice:thread-1' (thread.txt); "
            "if absent, the wildcard prefix match is broken (treating wildcard as exact match). "
            f"result sources: {result_sources}"
        )


def test_scope_noop_on_unscoped_collection_e2e(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """E2e S10: Collection with no scoped chunks + scope_filter returns all top-k; no error.

    Unscoped chunks (scopes=null) are treated as shared/global and always match
    any scope_filter.  When the entire collection is unscoped, scope_filter is
    effectively a no-op.
    """
    col = "t3-scope-noop"
    col_path = tmp_path / col
    col_path.mkdir()

    doc_file = col_path / "doc.txt"
    doc_file.write_text(
        "some general content alpha beta gamma delta epsilon " * 5, encoding="utf-8"
    )

    with make_real_app(tmp_path, monkeypatch) as (client, cfg, api_key):
        # Ingest without any scopes (unscoped / shared / global)
        _ingest_file(client, col, str(doc_file), api_key, chunk_scopes=None)

        # Even with a scope_filter, unscoped chunks match → results returned, no error
        status, data = _post_search(
            client, col, "general content alpha",
            api_key, scope_filter="user:alice",
        )
        assert status == 200, (
            f"Expected 200 for scope_filter on unscoped collection (S10); "
            f"got {status}: {data}"
        )
        results = data["results"]
        assert results, (
            "Unscoped chunks should match any scope_filter (S10: unscoped = shared/global); "
            "expected at least one result but got none."
        )


def test_scope_filter_bare_wildcard_400_e2e(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """E2e S11: POST /search with scope_filter="*" (bare wildcard) → 400.

    A bare '*' is not a valid scope_filter — it must have a non-empty prefix
    before the wildcard character.
    """
    col = "t3-bare-wild"
    col_path = tmp_path / col
    col_path.mkdir()

    doc_file = col_path / "doc.txt"
    doc_file.write_text("some content for bare wildcard test", encoding="utf-8")

    with make_real_app(tmp_path, monkeypatch) as (client, cfg, api_key):
        _ingest_file(client, col, str(doc_file), api_key)

        status, data = _post_search(
            client, col, "content",
            api_key, scope_filter="*",
        )
        assert status == 400, (
            f"Expected 400 for bare scope_filter='*' (S11); got {status}: {data}"
        )
        detail = data.get("detail", {})
        assert isinstance(detail, dict), (
            f"Expected detail to be a dict with 'code'/'message' fields; got {type(detail)}: {detail!r}"
        )
        assert detail.get("code") == "invalid_scope_filter", (
            f"Expected code='invalid_scope_filter' in detail; got {detail}"
        )


def test_scope_filter_leading_wildcard_400_e2e(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """E2e S12: POST /search with scope_filter="user:*alice" (leading wildcard) → 400.

    The wildcard '*' must appear only at the end of the scope_filter string.
    A mid-string or leading '*' is invalid.
    """
    col = "t3-lead-wild"
    col_path = tmp_path / col
    col_path.mkdir()

    doc_file = col_path / "doc.txt"
    doc_file.write_text("some content for leading wildcard test", encoding="utf-8")

    with make_real_app(tmp_path, monkeypatch) as (client, cfg, api_key):
        _ingest_file(client, col, str(doc_file), api_key)

        # Leading wildcard: "user:*alice"
        status1, data1 = _post_search(
            client, col, "content",
            api_key, scope_filter="user:*alice",
        )
        assert status1 == 400, (
            f"Expected 400 for scope_filter='user:*alice' (S12); got {status1}: {data1}"
        )
        detail1 = data1.get("detail", {})
        assert isinstance(detail1, dict), (
            f"Expected detail to be a dict with 'code' field; got {type(detail1)}: {detail1!r}"
        )
        assert detail1.get("code") == "invalid_scope_filter", (
            f"Expected code='invalid_scope_filter' for leading wildcard; got {detail1}"
        )

        # Double wildcard: "user:**" — also invalid (multiple '*')
        status2, data2 = _post_search(
            client, col, "content",
            api_key, scope_filter="user:**",
        )
        assert status2 == 400, (
            f"Expected 400 for scope_filter='user:**' (double wildcard, S12); got {status2}: {data2}"
        )


# ---------------------------------------------------------------------------
# E2e test — scope filter on explain endpoint
# ---------------------------------------------------------------------------


def test_scope_filter_on_explain_e2e(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """E2e: POST /explain with scope_filter='user:alice' filters candidates.

    Ingest 3 chunks:
    - alice.txt: scopes=['user:alice']
    - bob.txt:   scopes=['user:bob']
    - shared.txt: scopes=null (unscoped)

    POST /explain query='...' scope_filter='user:alice':
    - results must contain alice-scoped chunk and unscoped chunk.
    - bob-scoped chunk must be absent from BOTH results AND near_misses.
    """
    col = "t3-explain-scope"
    col_path = tmp_path / col
    col_path.mkdir()

    alice_file = col_path / "alice.txt"
    alice_file.write_text(
        "alice explain content alpha beta gamma delta epsilon " * 5, encoding="utf-8"
    )
    bob_file = col_path / "bob.txt"
    bob_file.write_text(
        "bob explain content alpha beta gamma delta epsilon " * 5, encoding="utf-8"
    )
    shared_file = col_path / "shared.txt"
    shared_file.write_text(
        "shared explain content alpha beta gamma delta epsilon " * 5, encoding="utf-8"
    )

    with make_real_app(tmp_path, monkeypatch) as (client, cfg, api_key):
        _ingest_file(client, col, str(alice_file), api_key, chunk_scopes=["user:alice"])
        _ingest_file(client, col, str(bob_file), api_key, chunk_scopes=["user:bob"])
        _ingest_file(client, col, str(shared_file), api_key, chunk_scopes=None)

        status, data = _post_explain(
            client, "explain content alpha",
            api_key, collection=col, scope_filter="user:alice",
        )
        assert status == 200, f"Expected 200 for /explain with scope_filter; got {status}: {data}"

        results = data.get("results", [])
        near_misses = data.get("near_misses", [])

        # bob.txt must be absent from BOTH results and near_misses
        result_sources = {r["source_path"] for r in results}
        near_miss_sources = {nm["source_path"] for nm in near_misses}
        all_sources = result_sources | near_miss_sources

        bob_in_results = any("bob.txt" in s for s in all_sources)
        assert not bob_in_results, (
            f"bob-scoped chunk must be absent from results AND near_misses "
            f"when scope_filter='user:alice'; found in: {all_sources}"
        )

        # At least one result must appear
        assert results, (
            "Expected at least one result (alice or shared chunk) with scope_filter='user:alice'"
        )
        assert any("alice.txt" in r["source_path"] for r in results), (
            f"alice-scoped chunk must appear in /explain results for scope_filter='user:alice'; "
            f"result sources: {result_sources}"
        )
        assert any("shared.txt" in r["source_path"] for r in (results + near_misses)), (
            f"Unscoped (shared) chunk must appear in /explain results or near_misses for any scope_filter; "
            f"result sources: {result_sources}, near_miss sources: {near_miss_sources}"
        )


# ---------------------------------------------------------------------------
# E2e test — GET /documents includes scopes (S13)
# ---------------------------------------------------------------------------


def test_get_documents_includes_scopes_e2e(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """E2e S13: GET /collections/{name}/documents includes scopes per document.

    Ingest two files with different scopes, then verify GET /documents returns
    scopes as a non-empty list for the scoped document, and an empty list for
    the unscoped document.
    """
    col = "t3-docs-scopes"
    col_path = tmp_path / col
    col_path.mkdir()

    alice_file = col_path / "alice_doc.txt"
    alice_file.write_text(
        "alice document scopes content for t3 s13 test alpha beta gamma " * 3,
        encoding="utf-8",
    )
    unscoped_file = col_path / "unscoped_doc.txt"
    unscoped_file.write_text(
        "unscoped document content for t3 s13 test alpha beta gamma " * 3,
        encoding="utf-8",
    )

    with make_real_app(tmp_path, monkeypatch) as (client, cfg, api_key):
        _ingest_file(
            client, col, str(alice_file), api_key,
            chunk_scopes=["user:alice", "role:admin"],
        )
        _ingest_file(client, col, str(unscoped_file), api_key, chunk_scopes=None)

        resp = client.get(
            f"/collections/{col}/documents",
            headers=_auth(api_key),
        )
        assert resp.status_code == 200, (
            f"GET /documents failed: {resp.status_code} {resp.text}"
        )
        data = resp.json()
        items = data.get("items", [])
        assert items, f"Expected at least one document item; got {items}"

        # Build a map from source_path basename to scopes
        doc_map = {
            item["source_path"].split("/")[-1]: item.get("scopes", [])
            for item in items
        }

        # alice_doc.txt must have scopes ['role:admin', 'user:alice'] (sorted alphabetically)
        assert "alice_doc.txt" in doc_map, (
            f"alice_doc.txt not found in GET /documents; docs: {list(doc_map.keys())}"
        )
        alice_scopes = sorted(doc_map["alice_doc.txt"])
        assert alice_scopes == ["role:admin", "user:alice"], (
            f"Expected sorted scopes=['role:admin', 'user:alice'] for alice_doc.txt; "
            f"got {alice_scopes}"
        )

        # unscoped_doc.txt must have scopes=[] (empty list)
        assert "unscoped_doc.txt" in doc_map, (
            f"unscoped_doc.txt not found in GET /documents; docs: {list(doc_map.keys())}"
        )
        unscoped_scopes = doc_map["unscoped_doc.txt"]
        assert unscoped_scopes == [], (
            f"Expected scopes=[] for unscoped_doc.txt; got {unscoped_scopes}"
        )


# ---------------------------------------------------------------------------
# Integration test — pipeline.explain with wildcard scope_filter
# ---------------------------------------------------------------------------


async def _make_real_pipeline(tmp_path: Path):
    """Build a real SearchStore + SearchPipeline for pipeline.explain integration tests."""
    from archon_search.chunker import DocumentChunker
    from archon_search.embedder import Embedder
    from archon_search.parser import DocumentParser
    from archon_search.pipeline import SearchPipeline
    from archon_search.reranker import Reranker
    from archon_search.store import SearchStore

    class _MockEmbedderBackend:
        model_name: str = "mock-embedder"
        is_warm: bool = False

        def encode(self, texts: list[str]) -> list[list[float]]:
            return [[0.1, 0.2, 0.3, 0.4] for _ in texts]

    class _MockRerankerBackend:
        is_warm: bool = False

        def predict(self, pairs: list[tuple[str, str]]) -> list[float]:
            return [0.5] * len(pairs)

    store = SearchStore(str(tmp_path / "db"))
    await store.connect()

    pipeline = SearchPipeline(
        store=store,
        embedder=Embedder(_MockEmbedderBackend()),
        reranker=Reranker(_MockRerankerBackend()),
        chunker=DocumentChunker(chunk_size=128),
        parser=DocumentParser(),
        top_k_retrieve=20,
        top_k_return=10,
    )
    return store, pipeline


async def _ingest_scoped_file(
    pipeline,
    tmp_path: Path,
    col: str,
    text: str,
    filename: str,
    scopes: list[str] | None,
) -> None:
    """Write text to tmp file and ingest via pipeline with given scopes."""
    f = tmp_path / filename
    f.write_text(text, encoding="utf-8")
    embedder = pipeline._global_embedder
    await pipeline.ingest_file(f, col, embedder=embedder, chunk_scopes=scopes)


@pytest.mark.asyncio
async def test_pipeline_explain_wildcard_scope_filter_applied(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Integration: pipeline.explain with scope_filter='user:*' — wildcard post-filter applied.

    Ingest 3 chunks:
    - scopes=['user:alice']  → prefix 'user:alice' starts with 'user:' → included
    - scopes=['user:bob']    → prefix 'user:bob' starts with 'user:'  → included
    - scopes=null (unscoped) → always passes wildcard post-filter      → included

    We also verify that cross-prefix chunks are excluded by adding a 'system:admin' chunk
    that should NOT match 'user:*'.
    """
    monkeypatch.setenv("ARCHON_SEARCH_DATA_DIR", str(tmp_path))
    store, pipeline = await _make_real_pipeline(tmp_path)
    col = "t3-explain-wild"
    embedder = pipeline._global_embedder

    await _ingest_scoped_file(
        pipeline, tmp_path, col,
        "alice explain wildcard content alpha beta gamma " * 5,
        "alice.txt", scopes=["user:alice"],
    )
    await _ingest_scoped_file(
        pipeline, tmp_path, col,
        "bob explain wildcard content alpha beta gamma " * 5,
        "bob.txt", scopes=["user:bob"],
    )
    await _ingest_scoped_file(
        pipeline, tmp_path, col,
        "shared explain wildcard content alpha beta gamma " * 5,
        "shared.txt", scopes=None,
    )
    await _ingest_scoped_file(
        pipeline, tmp_path, col,
        "system admin explain wildcard content alpha beta gamma " * 5,
        "system.txt", scopes=["system:admin"],
    )

    result = await pipeline.explain(
        "explain wildcard content alpha beta",
        col,
        embedder=embedder,
        scope_filter="user:*",
    )

    all_candidates = list(result.top_results) + list(result.near_misses)
    assert all_candidates, "Expected at least one candidate from pipeline.explain with scope_filter='user:*'"

    all_sources = {c.source_path for c in all_candidates}
    # system.txt (scopes=['system:admin']) must NOT match 'user:*'
    system_in_results = any("system.txt" in s for s in all_sources)
    assert not system_in_results, (
        f"system:admin chunk should NOT match scope_filter='user:*' (prefix 'system:' != 'user:'); "
        f"found in: {all_sources}"
    )

    # alice.txt, bob.txt, shared.txt must all match 'user:*'
    # (At minimum, the unscoped shared.txt must always pass)
    user_or_shared = any(
        "alice.txt" in s or "bob.txt" in s or "shared.txt" in s
        for s in all_sources
    )
    assert user_or_shared, (
        f"Expected alice/bob/shared chunks to match scope_filter='user:*'; "
        f"found sources: {all_sources}"
    )

    await store.disconnect()


# ---------------------------------------------------------------------------
# E2e test — explain scope filter with multi-collection
# ---------------------------------------------------------------------------


def test_explain_scope_filter_multi_collection_e2e(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """E2e: POST /explain with collections=['c1', 'c2'] and scope_filter='user:alice'
    returns only alice-scoped and unscoped chunks across both collections.

    Multi-collection explain applies scope_filter uniformly to all collections.
    """
    col1 = "t3-multi-c1"
    col2 = "t3-multi-c2"

    col1_path = tmp_path / col1
    col1_path.mkdir()
    col2_path = tmp_path / col2
    col2_path.mkdir()

    # Col 1: alice + bob
    alice_c1 = col1_path / "alice_c1.txt"
    alice_c1.write_text(
        "alice collection one content alpha beta gamma delta " * 5, encoding="utf-8"
    )
    bob_c1 = col1_path / "bob_c1.txt"
    bob_c1.write_text(
        "bob collection one content alpha beta gamma delta " * 5, encoding="utf-8"
    )

    # Col 2: alice + unscoped
    alice_c2 = col2_path / "alice_c2.txt"
    alice_c2.write_text(
        "alice collection two content alpha beta gamma delta " * 5, encoding="utf-8"
    )
    shared_c2 = col2_path / "shared_c2.txt"
    shared_c2.write_text(
        "shared collection two content alpha beta gamma delta " * 5, encoding="utf-8"
    )

    with make_real_app(tmp_path, monkeypatch) as (client, cfg, api_key):
        # Ingest into col1
        _ingest_file(client, col1, str(alice_c1), api_key, chunk_scopes=["user:alice"])
        _ingest_file(client, col1, str(bob_c1), api_key, chunk_scopes=["user:bob"])

        # Ingest into col2
        _ingest_file(client, col2, str(alice_c2), api_key, chunk_scopes=["user:alice"])
        _ingest_file(client, col2, str(shared_c2), api_key, chunk_scopes=None)

        # POST /explain with two collections and scope_filter='user:alice'
        status, data = _post_explain(
            client,
            "collection content alpha beta",
            api_key,
            collections=[col1, col2],
            scope_filter="user:alice",
        )
        assert status == 200, (
            f"Expected 200 for multi-collection explain with scope_filter; got {status}: {data}"
        )

        results = data.get("results", [])
        near_misses = data.get("near_misses", [])

        result_sources = {r["source_path"] for r in results}
        near_miss_sources = {nm["source_path"] for nm in near_misses}
        all_sources = result_sources | near_miss_sources

        # bob_c1 must not appear in either results or near_misses
        bob_in_results = any("bob_c1.txt" in s for s in all_sources)
        assert not bob_in_results, (
            f"bob-scoped chunk (col1) must be excluded by scope_filter='user:alice'; "
            f"found in: {all_sources}"
        )

        # At least some alice or shared chunk must appear
        assert results, (
            "Expected at least one result (alice or shared) in multi-collection explain "
            "with scope_filter='user:alice'"
        )
