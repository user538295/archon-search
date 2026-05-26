"""13-cell ACL × filter integration matrix for SearchPipeline.

Matrix:
    Rows (filter type): no_filter, source_path_prefix, source_path_glob, date_range
    Columns (ACL state): no_acl, acl_match, acl_deny
    Plus one extra cell: prefix=/misc/ + test-ns (acl_filtered=False)

Each test seeds a real LanceDB collection, applies filter+ACL, and asserts:
1. Exactly the expected chunk_ids appear in results
2. SearchPipelineResult.acl_filtered flag is correct
3. For acl_deny+glob and acl_deny+date_range: the combined-attrition WARNING fires
"""
from __future__ import annotations

import asyncio
import hashlib
import logging

import pytest

from archon_search._types import ChunkRecord
from archon_search.filters import SearchFilters
from archon_search.pipeline import SearchPipeline, SearchPipelineResult
from archon_search.store import SearchStore


# ---------------------------------------------------------------------------
# Mock backends (dim=4 — avoids real model weights)
# ---------------------------------------------------------------------------

class _MockEmbedder:
    """Thin wrapper that matches Embedder's public interface."""

    model_name: str = "mock-embedder"
    embedding_dim: int = 4
    is_warm: bool = False

    async def embed(self, texts: list[str]) -> list[list[float]]:
        return [[0.25] * 4 for _ in texts]

    async def embed_one(self, text: str) -> list[float]:
        return [0.25] * 4


class _MockReranker:
    """Thin wrapper that matches Reranker's public interface."""

    async def rerank(self, query: str, candidates: list, top_k: int) -> list:
        return candidates[:top_k]


# ---------------------------------------------------------------------------
# Corpus design
#
# 8 chunks, 6 logical docs:
#
# chunk 0: /docs/alpha.md   indexed_at=2025-01-10  acl=None           (open)
# chunk 1: /docs/beta.md    indexed_at=2025-03-15  acl=None           (open)
# chunk 2: /notes/gamma.txt indexed_at=2025-06-20  acl=None           (open)
# chunk 3: /docs/delta.md   indexed_at=2025-09-01  acl=["test-ns"]    (match)
# chunk 4: /notes/epsilon.md indexed_at=2025-11-05 acl=["test-ns"]    (match)
# chunk 5: /docs/zeta.md    indexed_at=2025-12-31  acl=["other-ns"]   (deny)
# chunk 6: /notes/eta.txt   indexed_at=2024-05-01  acl=["other-ns"]   (deny)
# chunk 7: /misc/theta.md   indexed_at=2024-08-15  acl=None           (open)
#
# Filter predicates:
#   source_path_prefix="/docs/"  → chunks {0,1,3,5}
#   source_path_glob="*.md"      → chunks {0,1,3,4,5,7}  (all .md)
#   date_range (indexed_after=2025-07-01) → chunks {3,4,5}
#
# Expected results per cell:
#
# Namespace legend:
#   no_acl    uses ns="no-acl-ns"  — denies ALL acl-restricted chunks (max denial)
#   acl_match uses ns="test-ns"    — allows test-ns, denies other-ns
#   acl_deny  uses ns="other-ns"   — allows other-ns, denies test-ns
#
# no_filter + no_acl    → {alpha,beta,gamma,theta}           acl_filtered=True
# no_filter + acl_match → {alpha,beta,gamma,delta,eps,theta} acl_filtered=True
# no_filter + acl_deny  → {alpha,beta,gamma,zeta,eta,theta}  acl_filtered=True
#
# prefix + no_acl    → {alpha,beta}       acl_filtered=True
# prefix + acl_match → {alpha,beta,delta} acl_filtered=True
# prefix + acl_deny  → {alpha,beta,zeta}  acl_filtered=True
#
# glob + no_acl    → {alpha,beta,theta}                acl_filtered=True
# glob + acl_match → {alpha,beta,delta,epsilon,theta}  acl_filtered=True
# glob + acl_deny  → {alpha,beta,zeta,theta}           acl_filtered=True + WARNING (4<5)
#
# date_range (after 2025-07-01) + no_acl    → {}    acl_filtered=True
# date_range + acl_match → {delta,epsilon}          acl_filtered=True + WARNING (2<5)
# date_range + acl_deny  → {zeta}                   acl_filtered=True + WARNING (1<5)
#
# EXTRA: prefix=/misc/ + test-ns → {theta}           acl_filtered=False (no denials)
# ---------------------------------------------------------------------------

_DIM = 4

# Fixed timestamps (fixed-width UTC format)
_T_2025_01 = "2025-01-10T00:00:00.000000Z"
_T_2025_03 = "2025-03-15T00:00:00.000000Z"
_T_2025_06 = "2025-06-20T00:00:00.000000Z"
_T_2025_09 = "2025-09-01T00:00:00.000000Z"
_T_2025_11 = "2025-11-05T00:00:00.000000Z"
_T_2025_12 = "2025-12-31T00:00:00.000000Z"
_T_2024_05 = "2024-05-01T00:00:00.000000Z"
_T_2024_08 = "2024-08-15T00:00:00.000000Z"

# Namespace constants
_NS_TEST = "test-ns"
_NS_OTHER = "other-ns"


def _doc_id(seed: str) -> str:
    return hashlib.sha256(seed.encode()).hexdigest()


def _make_corpus() -> list[ChunkRecord]:
    """Build the 8-chunk fixed corpus."""
    specs = [
        # (seed, source_path, indexed_at, acl, text_suffix)
        ("alpha",   "/docs/alpha.md",    _T_2025_01, None,        "alpha open docs"),
        ("beta",    "/docs/beta.md",     _T_2025_03, None,        "beta open docs"),
        ("gamma",   "/notes/gamma.txt",  _T_2025_06, None,        "gamma open notes"),
        ("delta",   "/docs/delta.md",    _T_2025_09, [_NS_TEST],  "delta match docs"),
        ("epsilon", "/notes/epsilon.md", _T_2025_11, [_NS_TEST],  "epsilon match notes"),
        ("zeta",    "/docs/zeta.md",     _T_2025_12, [_NS_OTHER], "zeta deny docs"),
        ("eta",     "/notes/eta.txt",    _T_2024_05, [_NS_OTHER], "eta deny notes"),
        ("theta",   "/misc/theta.md",    _T_2024_08, None,        "theta open misc"),
    ]
    records = []
    for i, (seed, path, ts, acl, txt) in enumerate(specs):
        doc_id = _doc_id(seed)
        records.append(
            ChunkRecord(
                doc_id=doc_id,
                chunk_id=f"{doc_id}-{0:06d}",
                text=f"Integration test chunk: {txt}",
                vector=[0.25] * _DIM,
                source_path=path,
                indexed_at=ts,
                file_type=path.rsplit(".", 1)[-1],
                acl=acl,
            )
        )
    return records


# Stable chunk_id helpers (index into corpus list)
def _chunk_id(seed: str) -> str:
    return f"{_doc_id(seed)}-000000"


_CID = {
    "alpha":   _chunk_id("alpha"),
    "beta":    _chunk_id("beta"),
    "gamma":   _chunk_id("gamma"),
    "delta":   _chunk_id("delta"),
    "epsilon": _chunk_id("epsilon"),
    "zeta":    _chunk_id("zeta"),
    "eta":     _chunk_id("eta"),
    "theta":   _chunk_id("theta"),
}


# ---------------------------------------------------------------------------
# Shared fixture
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def matrix_store(tmp_path_factory: pytest.TempPathFactory):  # type: ignore[no-untyped-def]
    """One shared SearchStore for the whole module — avoids Tokio thread-pool spam."""
    tmp = tmp_path_factory.mktemp("matrix_db")
    store = SearchStore(tmp)
    asyncio.run(store.connect())
    yield store
    asyncio.run(store.disconnect())


@pytest.fixture(scope="module")
def matrix_col(matrix_store) -> str:  # type: ignore[no-untyped-def]
    """A single collection seeded with the fixed corpus, shared across all 13 tests."""
    col = "acl-matrix"
    corpus = _make_corpus()

    async def _setup() -> None:
        await matrix_store.ensure_collection(col, _DIM)
        await matrix_store.ingest_chunks(col, corpus)
        await matrix_store.rebuild_fts_index(col)

    asyncio.run(_setup())
    return col


def _make_pipeline(store: SearchStore, top_k_retrieve: int = 20, top_k_return: int = 5) -> SearchPipeline:
    from archon_search.chunker import DocumentChunker
    from archon_search.parser import DocumentParser

    return SearchPipeline(
        store=store,
        embedder=_MockEmbedder(),  # type: ignore[arg-type]
        reranker=_MockReranker(),  # type: ignore[arg-type]
        chunker=DocumentChunker(chunk_size=128),
        parser=DocumentParser(),
        top_k_retrieve=top_k_retrieve,
        top_k_return=top_k_return,
    )


def _ids(result: SearchPipelineResult) -> set[str]:
    return {r.chunk_id for r in result.results}


# ---------------------------------------------------------------------------
# Helper: date_range filter (indexed_after=2025-07-01)
# ---------------------------------------------------------------------------
_DATE_AFTER = "2025-07-01"


# ===========================================================================
# Row 1: no_filter
# ===========================================================================

@pytest.mark.integration
@pytest.mark.asyncio
async def test_no_filter_no_acl(matrix_store, matrix_col):
    """no_filter + no_acl (ns=no-acl-ns): open chunks only {alpha,beta,gamma,theta}."""
    pipeline = _make_pipeline(matrix_store, top_k_return=8)
    result = await pipeline.search("test", matrix_col, namespace="no-acl-ns", filters=None)

    expected = {_CID["alpha"], _CID["beta"], _CID["gamma"], _CID["theta"]}
    assert _ids(result) == expected
    assert result.acl_filtered is True  # delta,epsilon (test-ns) and zeta,eta (other-ns) denied


@pytest.mark.integration
@pytest.mark.asyncio
async def test_no_filter_acl_match(matrix_store, matrix_col):
    """no_filter + acl_match (ns=test-ns): open + test-ns chunks {alpha,beta,gamma,delta,epsilon,theta}."""
    pipeline = _make_pipeline(matrix_store, top_k_return=8)
    result = await pipeline.search("test", matrix_col, namespace=_NS_TEST, filters=None)

    expected = {
        _CID["alpha"], _CID["beta"], _CID["gamma"],
        _CID["delta"], _CID["epsilon"], _CID["theta"],
    }
    assert _ids(result) == expected
    assert result.acl_filtered is True  # zeta (other-ns) and eta (other-ns) denied


@pytest.mark.integration
@pytest.mark.asyncio
async def test_no_filter_acl_deny(matrix_store, matrix_col):
    """no_filter + acl_deny (ns=other-ns): open + other-ns chunks {alpha,beta,gamma,zeta,eta,theta}."""
    pipeline = _make_pipeline(matrix_store, top_k_return=8)
    result = await pipeline.search("test", matrix_col, namespace=_NS_OTHER, filters=None)

    expected = {
        _CID["alpha"], _CID["beta"], _CID["gamma"],
        _CID["zeta"], _CID["eta"], _CID["theta"],
    }
    assert _ids(result) == expected
    assert result.acl_filtered is True  # delta (test-ns) and epsilon (test-ns) denied


# ===========================================================================
# Row 2: source_path_prefix = "/docs/"
# ===========================================================================

@pytest.mark.integration
@pytest.mark.asyncio
async def test_prefix_no_acl(matrix_store, matrix_col):
    """prefix=/docs/ + no_acl (ns=no-acl-ns): only open /docs/ chunks {alpha,beta}."""
    pipeline = _make_pipeline(matrix_store)
    filters = SearchFilters(source_path_prefix="/docs/")
    result = await pipeline.search("test", matrix_col, namespace="no-acl-ns", filters=filters)

    expected = {_CID["alpha"], _CID["beta"]}
    assert _ids(result) == expected
    assert result.acl_filtered is True  # delta (test-ns) and zeta (other-ns) denied


@pytest.mark.integration
@pytest.mark.asyncio
async def test_prefix_acl_match(matrix_store, matrix_col):
    """prefix=/docs/ + acl_match (ns=test-ns): open + test-ns /docs/ chunks {alpha,beta,delta}."""
    pipeline = _make_pipeline(matrix_store)
    filters = SearchFilters(source_path_prefix="/docs/")
    result = await pipeline.search("test", matrix_col, namespace=_NS_TEST, filters=filters)

    expected = {_CID["alpha"], _CID["beta"], _CID["delta"]}
    assert _ids(result) == expected
    assert result.acl_filtered is True  # zeta (other-ns) denied


@pytest.mark.integration
@pytest.mark.asyncio
async def test_prefix_acl_deny(matrix_store, matrix_col):
    """prefix=/docs/ + acl_deny (ns=other-ns): open + other-ns /docs/ chunks {alpha,beta,zeta}."""
    pipeline = _make_pipeline(matrix_store)
    filters = SearchFilters(source_path_prefix="/docs/")
    result = await pipeline.search("test", matrix_col, namespace=_NS_OTHER, filters=filters)

    expected = {_CID["alpha"], _CID["beta"], _CID["zeta"]}
    assert _ids(result) == expected
    assert result.acl_filtered is True  # delta (test-ns) denied


# ===========================================================================
# Row 3: source_path_glob = "*.md"
# ===========================================================================

@pytest.mark.integration
@pytest.mark.asyncio
async def test_glob_no_acl(matrix_store, matrix_col):
    """glob=*.md + no_acl (ns=no-acl-ns): open .md chunks {alpha,beta,theta}."""
    pipeline = _make_pipeline(matrix_store)
    filters = SearchFilters(source_path_glob="*.md")
    result = await pipeline.search("test", matrix_col, namespace="no-acl-ns", filters=filters)

    expected = {_CID["alpha"], _CID["beta"], _CID["theta"]}
    assert _ids(result) == expected
    assert result.acl_filtered is True  # delta,epsilon (test-ns) and zeta (other-ns) denied


@pytest.mark.integration
@pytest.mark.asyncio
async def test_glob_acl_match(matrix_store, matrix_col):
    """glob=*.md + acl_match (ns=test-ns): open + test-ns .md chunks {alpha,beta,delta,epsilon,theta}."""
    pipeline = _make_pipeline(matrix_store)
    filters = SearchFilters(source_path_glob="*.md")
    result = await pipeline.search("test", matrix_col, namespace=_NS_TEST, filters=filters)

    expected = {_CID["alpha"], _CID["beta"], _CID["delta"], _CID["epsilon"], _CID["theta"]}
    assert _ids(result) == expected
    assert result.acl_filtered is True  # zeta (other-ns) denied


@pytest.mark.integration
@pytest.mark.asyncio
async def test_glob_acl_deny(matrix_store, matrix_col, caplog):
    """glob=*.md + acl_deny (ns=other-ns): {alpha,beta,zeta,theta}; WARNING fires (4<5)."""
    # glob hits: alpha,beta,delta,epsilon,zeta,theta (6 .md chunks)
    # other-ns: delta (test-ns) DENIED, epsilon (test-ns) DENIED, zeta (other-ns) ALLOWED
    # Result: {alpha,beta,zeta,theta} = 4 < top_k_return=5 → WARNING
    pipeline = _make_pipeline(matrix_store, top_k_return=5)
    filters = SearchFilters(source_path_glob="*.md")

    with caplog.at_level(logging.WARNING, logger="archon"):
        result = await pipeline.search("test", matrix_col, namespace=_NS_OTHER, filters=filters)

    expected = {_CID["alpha"], _CID["beta"], _CID["zeta"], _CID["theta"]}
    assert _ids(result) == expected
    assert result.acl_filtered is True

    warning_msgs = [r.message for r in caplog.records if r.levelno == logging.WARNING]
    assert any("filter+ACL combined attrition" in m for m in warning_msgs), (
        f"Expected combined-attrition WARNING; got: {warning_msgs}"
    )


# ===========================================================================
# Row 4: date_range (indexed_after=2025-07-01)
# ===========================================================================

@pytest.mark.integration
@pytest.mark.asyncio
async def test_date_range_no_acl(matrix_store, matrix_col, caplog):
    """date_range (after 2025-07-01) + no_acl (ns=no-acl-ns): all in-range chunks denied → {}; WARNING fires."""
    # Chunks after 2025-07-01: delta (test-ns), epsilon (test-ns), zeta (other-ns)
    # With no-acl-ns: all three denied → empty result, 0 < top_k_return=5 → WARNING
    pipeline = _make_pipeline(matrix_store, top_k_return=5)
    filters = SearchFilters(indexed_after=_DATE_AFTER)

    with caplog.at_level(logging.WARNING, logger="archon"):
        result = await pipeline.search("test", matrix_col, namespace="no-acl-ns", filters=filters)

    assert _ids(result) == set()
    assert result.acl_filtered is True  # all 3 in range are denied

    warning_msgs = [r.message for r in caplog.records if r.levelno == logging.WARNING]
    assert any("filter+ACL combined attrition" in m for m in warning_msgs), (
        f"Expected combined-attrition WARNING; got: {warning_msgs}"
    )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_date_range_acl_match(matrix_store, matrix_col, caplog):
    """date_range (after 2025-07-01) + acl_match (ns=test-ns): {delta,epsilon}; WARNING fires (2<5)."""
    pipeline = _make_pipeline(matrix_store, top_k_return=5)
    filters = SearchFilters(indexed_after=_DATE_AFTER)

    with caplog.at_level(logging.WARNING, logger="archon"):
        result = await pipeline.search("test", matrix_col, namespace=_NS_TEST, filters=filters)

    expected = {_CID["delta"], _CID["epsilon"]}
    assert _ids(result) == expected
    assert result.acl_filtered is True  # zeta (other-ns) denied

    warning_msgs = [r.message for r in caplog.records if r.levelno == logging.WARNING]
    assert any("filter+ACL combined attrition" in m for m in warning_msgs), (
        f"Expected combined-attrition WARNING; got: {warning_msgs}"
    )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_date_range_acl_deny(matrix_store, matrix_col, caplog):
    """date_range + acl_deny (ns=other-ns): {zeta}; WARNING fires (1<5)."""
    # After 2025-07-01: delta (test-ns), epsilon (test-ns), zeta (other-ns)
    # With other-ns: delta DENIED, epsilon DENIED, zeta ALLOWED → 1 < top_k_return=5 → WARNING
    pipeline = _make_pipeline(matrix_store, top_k_return=5)
    filters = SearchFilters(indexed_after=_DATE_AFTER)

    with caplog.at_level(logging.WARNING, logger="archon"):
        result = await pipeline.search("test", matrix_col, namespace=_NS_OTHER, filters=filters)

    expected = {_CID["zeta"]}
    assert _ids(result) == expected
    assert result.acl_filtered is True

    warning_msgs = [r.message for r in caplog.records if r.levelno == logging.WARNING]
    assert any("filter+ACL combined attrition" in m for m in warning_msgs), (
        f"Expected combined-attrition WARNING; got: {warning_msgs}"
    )


# ===========================================================================
# Extra cell: acl_filtered=False (no denials)
# ===========================================================================

@pytest.mark.integration
@pytest.mark.asyncio
async def test_prefix_misc_no_denial(matrix_store, matrix_col):
    """prefix=/misc/ + test-ns: only theta matches (acl=None) → no ACL denials → acl_filtered=False."""
    pipeline = _make_pipeline(matrix_store)
    filters = SearchFilters(source_path_prefix="/misc/")
    result = await pipeline.search("test", matrix_col, namespace=_NS_TEST, filters=filters)

    assert _ids(result) == {_CID["theta"]}
    assert result.acl_filtered is False
