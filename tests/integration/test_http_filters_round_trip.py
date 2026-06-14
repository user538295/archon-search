"""Task 1.2 — Metadata filter HTTP round-trips.

Exercises the full HTTP → middleware → route → pipeline → LanceDB path for
filter dimensions: ``source_path_prefix`` SQL-escaping, system metadata
fields with ``include_metadata``, ``indexed_after``, and ``language``.

Run with:
    uv run pytest tests/integration/test_http_filters_round_trip.py -v
"""
from __future__ import annotations

import asyncio
import hashlib
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

from tests.integration.conftest import ingest_file_via_path, make_real_app

pytestmark = pytest.mark.integration

# Embedding dimension produced by the stub fastembed backend (384-dim zeros).
_EMBEDDING_DIM = 384


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_chunk(doc_id: str, idx: int, text: str, source_path: str, *, language: str = "") -> "ChunkRecord":  # type: ignore[name-defined]  # noqa: F821
    """Build a ``ChunkRecord`` for direct store injection."""
    from archon_search._types import ChunkRecord, normalize_iso_utc

    return ChunkRecord(
        doc_id=doc_id,
        chunk_id=f"{doc_id}-{idx:06d}",
        text=text,
        vector=[0.0] * _EMBEDDING_DIM,
        source_path=source_path,
        indexed_at=normalize_iso_utc(datetime.now(timezone.utc)),
        language=language,
    )


def _doc_id(path: str) -> str:
    """Return the SHA-256 hex digest of *path* — generates a unique doc_id for test purposes.

    Note: the pipeline derives doc_id as ``sha256(str(path.resolve()).encode()).hexdigest()``
    (using the resolved absolute path).  This helper does NOT call ``resolve()`` because the
    paths used in direct-injection tests are synthetic strings, not real filesystem paths.
    The function is only used to produce collision-free identifiers for ``ChunkRecord``.
    """
    return hashlib.sha256(path.encode()).hexdigest()


async def _inject_chunks(store, col: str, chunks, *, embedding_model: str) -> None:
    """Ensure *col* exists, inject *chunks*, and create a minimal collection meta record.

    ``store.ingest_chunks`` populates LanceDB but does not create a
    ``CollectionMeta`` row.  The search route calls ``get_all_collections_meta``
    to validate that the collection exists; without a meta record it raises
    ``CollectionNotFoundError`` (404).  We create the meta row here so tests
    that inject directly can search via HTTP.

    ``embedding_model`` must be passed explicitly — callers should use
    ``cfg.embedding_model`` from the enclosing ``make_real_app`` context so
    the meta record matches the app's configured model.  A mismatch causes the
    collection to be excluded from search with zero results.
    """
    from archon_search.collection_meta import CollectionMeta

    await store.ensure_collection(col, _EMBEDDING_DIM)
    await store.ingest_chunks(col, chunks)
    await store.rebuild_fts_index(col)
    meta = CollectionMeta(
        name=col,
        active_embedding_model=embedding_model,
        doc_count=len({c.doc_id for c in chunks}),
        chunk_count=len(chunks),
    )
    await store.update_collection_meta(meta)


# ---------------------------------------------------------------------------
# Test 1 — source_path_prefix with special chars round-trip via HTTP
# ---------------------------------------------------------------------------

def test_source_path_prefix_special_chars_round_trip_via_http(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    r"""POST /search with source_path_prefix containing %, _, \, and '.

    Verifies ``build_where()`` / ``escape_like()`` SQL-escaping through the
    full HTTP→LanceDB path.  Four documents are injected:

    - ``matching_path``: starts exactly with the special-char prefix (must appear).
    - ``fp_percent_path``: would match the unescaped LIKE pattern if ``%`` acted as
      a wildcard but must NOT appear once ``%`` is escaped to a literal.
    - ``fp_underscore_path``: would match the unescaped LIKE pattern if ``_`` acted as
      a single-char wildcard but must NOT appear once ``_`` is escaped to a literal.
      In the prefix ``/data/back\\slash_corpus_%/…``, the ``_`` before ``corpus``
      is a LIKE metacharacter when unescaped (matches any single char).  This path
      replaces that ``_`` with ``X`` (``/data/back\\slashXcorpus_…``); if ``_``
      is not properly escaped, the ``_`` wildcard in the pattern would match ``X``
      and leak this document into results.
    - ``other_path``: unrelated path that must never appear.
    """
    col = "test-src-prefix-special"
    # Prefix contains all four SQL-special chars: %, _, \, and '
    # Backslash is a valid character in Unix path strings (not a path separator).
    special_prefix = "/data/back\\slash_corpus_%/sec'tion"
    matching_path = f"{special_prefix}/match.md"

    # % probe: "Xfoo" sits where "%" was in the prefix segment.
    # If % is not escaped (acts as wildcard), "%" matches "Xfoo" → false positive leaks.
    fp_percent_path = "/data/back\\slash_corpus_Xfoo/sec'tion/should_not_match_percent.md"

    # _ probe: the underscore separator before "corpus" is replaced by "X".
    # In the prefix the LIKE pattern contains "_corpus"; if _ is not escaped (acts as
    # wildcard), that _ matches any single char including "X" → false positive leaks.
    fp_underscore_path = "/data/back\\slashXcorpus_%/sec'tion/should_not_match_underscore.md"

    other_path = "/data/other/no_match.md"

    match_id = _doc_id(matching_path)
    fp_pct_id = _doc_id(fp_percent_path)
    fp_us_id = _doc_id(fp_underscore_path)
    other_id = _doc_id(other_path)

    with make_real_app(tmp_path, monkeypatch) as (client, cfg, api_key):
        store = client.app.state.search_store

        chunks = [
            _make_chunk(match_id, 0, "matching document content special prefix", matching_path),
            _make_chunk(fp_pct_id, 0, "percent probe content would match unescaped percent wildcard", fp_percent_path),
            _make_chunk(fp_us_id, 0, "underscore probe content would match unescaped underscore wildcard", fp_underscore_path),
            _make_chunk(other_id, 0, "other document content no prefix match", other_path),
        ]
        asyncio.run(_inject_chunks(store, col, chunks, embedding_model=cfg.embedding_model))

        headers = {"Authorization": f"Bearer {api_key}"}
        resp = client.post(
            "/search",
            json={
                "collection": col,
                "query": "document content",
                "top_k": 10,
                "filters": {"source_path_prefix": special_prefix},
            },
            headers=headers,
        )
        assert resp.status_code == 200, f"search failed: {resp.status_code} {resp.text}"
        items = resp.json()["results"]
        assert items, "expected at least one result matching the special-char prefix"
        for item in items:
            assert item["source_path"].startswith(special_prefix), (
                f"result source_path {item['source_path']!r} does not start with "
                f"prefix {special_prefix!r}"
            )
        result_paths = {item["source_path"] for item in items}
        # The % probe must NOT appear — proves % is treated as a literal, not a wildcard.
        assert fp_percent_path not in result_paths, (
            f"percent-probe path appeared in results — '%' SQL escaping is broken. "
            f"Got: {result_paths}"
        )
        # The _ probe must NOT appear — proves _ is treated as a literal, not a wildcard.
        assert fp_underscore_path not in result_paths, (
            f"underscore-probe path appeared in results — '_' SQL escaping is broken. "
            f"Got: {result_paths}"
        )
        assert other_path not in result_paths, (
            "non-matching document must not appear when source_path_prefix filter is active"
        )


# ---------------------------------------------------------------------------
# Test 2 — System metadata fields always present regardless of include_metadata
# ---------------------------------------------------------------------------

def test_system_metadata_fields_always_present_regardless_of_include_metadata(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """POST /search twice: once with include_metadata=false, once with include_metadata=true.

    System fields ``file_type``, ``updated_at``, and ``ingested_by`` must be
    non-empty in both responses.  Note: ``language`` is ``""`` (empty) for docs
    ingested without the optional fasttext language detector, so no assertion is
    made on it here.

    Custom ``metadata`` dict must be empty (``{}``) when ``include_metadata=false``
    and must be a dict (possibly empty for plain markdown) when
    ``include_metadata=true``.
    """
    md_file = tmp_path / "system-fields.md"
    md_file.write_text(
        "# System Fields Test\n\nThis document tests that system metadata fields "
        "are always returned by the search endpoint.\n" * 4
    )

    with make_real_app(tmp_path, monkeypatch) as (client, cfg, api_key):
        col = "test-system-fields"
        ingest_file_via_path(client, col, str(md_file), api_key=api_key)

        headers = {"Authorization": f"Bearer {api_key}"}

        # --- without include_metadata ---
        resp_no_meta = client.post(
            "/search",
            json={
                "collection": col,
                "query": "system metadata fields",
                "filters": {"include_metadata": False},
            },
            headers=headers,
        )
        assert resp_no_meta.status_code == 200
        items_no_meta = resp_no_meta.json()["results"]
        assert items_no_meta, "expected results (include_metadata=false)"

        for item in items_no_meta:
            assert item["file_type"], (
                f"file_type must be non-empty regardless of include_metadata, got: {item['file_type']!r}"
            )
            assert item["updated_at"], (
                f"updated_at must be non-empty regardless of include_metadata, got: {item['updated_at']!r}"
            )
            assert item["ingested_by"], (
                f"ingested_by must be non-empty regardless of include_metadata, got: {item['ingested_by']!r}"
            )
            # metadata dict must be empty when include_metadata=False
            assert item["metadata"] == {}, (
                f"metadata must be empty dict when include_metadata=false, got: {item['metadata']!r}"
            )

        # --- with include_metadata ---
        resp_with_meta = client.post(
            "/search",
            json={
                "collection": col,
                "query": "system metadata fields",
                "filters": {"include_metadata": True},
            },
            headers=headers,
        )
        assert resp_with_meta.status_code == 200
        items_with_meta = resp_with_meta.json()["results"]
        assert items_with_meta, "expected results (include_metadata=true)"

        for item in items_with_meta:
            assert item["file_type"], (
                f"file_type must be non-empty with include_metadata=true, got: {item['file_type']!r}"
            )
            assert item["updated_at"], (
                f"updated_at must be non-empty with include_metadata=true, got: {item['updated_at']!r}"
            )
            assert item["ingested_by"], (
                f"ingested_by must be non-empty with include_metadata=true, got: {item['ingested_by']!r}"
            )
            # metadata is always returned as a dict (may be {} for plain markdown without front-matter)
            assert isinstance(item["metadata"], dict), (
                f"metadata must be a dict when include_metadata=true, got: {type(item['metadata'])}"
            )


# ---------------------------------------------------------------------------
# Test 3 — indexed_after filter excludes older docs
# ---------------------------------------------------------------------------

def test_indexed_after_filter_excludes_older_docs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Ingest doc A, record timestamp T, ingest doc B. POST /search indexed_after=T.

    Assert only doc B is returned.  Verifies the ``indexed_after`` filter is
    correctly threaded through the HTTP→LanceDB path.
    """
    doc_a_path = tmp_path / "doc_a.md"
    doc_a_path.write_text("# Document Alpha\n\nThis is the older document.\n" * 4)

    doc_b_path = tmp_path / "doc_b.md"
    doc_b_path.write_text("# Document Beta\n\nThis is the newer document.\n" * 4)

    with make_real_app(tmp_path, monkeypatch) as (client, cfg, api_key):
        col = "test-indexed-after"

        # Ingest doc A
        ingest_file_via_path(client, col, str(doc_a_path), api_key=api_key)

        # Record timestamp T (slightly in the future to ensure doc B will be strictly after)
        time.sleep(0.01)
        timestamp_t = datetime.now(timezone.utc)
        time.sleep(0.01)

        # Ingest doc B
        ingest_file_via_path(client, col, str(doc_b_path), api_key=api_key)

        headers = {"Authorization": f"Bearer {api_key}"}
        resp = client.post(
            "/search",
            json={
                "collection": col,
                "query": "document",
                "top_k": 10,
                "filters": {"indexed_after": timestamp_t.isoformat()},
            },
            headers=headers,
        )
        assert resp.status_code == 200, f"search failed: {resp.status_code} {resp.text}"
        items = resp.json()["results"]
        assert items, "expected at least one result after indexed_after filter"

        # Only doc B source path should appear
        result_paths = {item["source_path"] for item in items}
        assert str(doc_b_path) in result_paths, (
            f"doc B (newer) must appear in results, got: {result_paths}"
        )
        assert str(doc_a_path) not in result_paths, (
            f"doc A (older) must be excluded by indexed_after filter, got: {result_paths}"
        )


# ---------------------------------------------------------------------------
# Test 4 — language filter returns matching lang only
# ---------------------------------------------------------------------------

def test_language_filter_http_response_returns_matching_lang_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Ingest French and English docs with explicit language tags.

    POST /search with filters.language=fr — assert all results have language='fr'.

    Since the fasttext language detector is not available in tests, chunks are
    injected directly into the store with explicit language codes via
    ``store.ingest_chunks``.  This exercises the SQL-filter path without
    requiring the detection pipeline.
    """
    col = "test-language-filter"
    fr_path = "/data/docs/french_doc.md"
    en_path = "/data/docs/english_doc.md"

    fr_id = _doc_id(fr_path)
    en_id = _doc_id(en_path)

    with make_real_app(tmp_path, monkeypatch) as (client, cfg, api_key):
        store = client.app.state.search_store

        chunks = [
            _make_chunk(fr_id, 0, "Bonjour le monde document en français", fr_path, language="fr"),
            _make_chunk(fr_id, 1, "Le contenu de ce document est en français", fr_path, language="fr"),
            _make_chunk(en_id, 0, "Hello world document in English language", en_path, language="en"),
            _make_chunk(en_id, 1, "The content of this document is in English", en_path, language="en"),
        ]
        asyncio.run(_inject_chunks(store, col, chunks, embedding_model=cfg.embedding_model))

        headers = {"Authorization": f"Bearer {api_key}"}
        resp = client.post(
            "/search",
            json={
                "collection": col,
                "query": "document language content",
                "top_k": 10,
                "filters": {"language": "fr"},
            },
            headers=headers,
        )
        assert resp.status_code == 200, f"search failed: {resp.status_code} {resp.text}"
        items = resp.json()["results"]
        assert items, "expected at least one French result when filtering by language='fr'"
        for item in items:
            assert item["language"] == "fr", (
                f"expected language='fr' on all results, got: {item['language']!r} "
                f"for source_path={item['source_path']!r}"
            )
        # No English documents should appear
        result_paths = {item["source_path"] for item in items}
        assert en_path not in result_paths, (
            "English document must not appear when language filter is 'fr'"
        )
