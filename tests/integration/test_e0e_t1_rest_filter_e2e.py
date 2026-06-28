"""tests/integration/test_e0e_t1_rest_filter_e2e.py

T-1: Integration e2e — REST filter + multi-collection search via TestClient.

Plan task: T-1 — Integration e2e: REST filter + multi-collection search via TestClient
#tester-role

Covers scenarios: S1, S2, S4, S5, S6, S10, S11 and temporal filters (indexed_after,
indexed_before).

Run with:
    uv run pytest tests/integration/test_e0e_t1_rest_filter_e2e.py -v
"""
from __future__ import annotations

import time
from datetime import UTC, datetime
from pathlib import Path

import pytest

from tests.integration.conftest import ingest_file_via_path, make_real_app

pytestmark = pytest.mark.integration


def _auth(api_key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {api_key}"}


# ---------------------------------------------------------------------------
# T-1 / test 1 — file_type filter: results from matching leg only (S1, S4)
# ---------------------------------------------------------------------------


def test_e2e_filter_multi_collection_file_type(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two collections, both containing .md files (and col_b also has .py).
    file_type='.md' filter must return .md results from BOTH legs (S1).
    Only .md files pass through — .py is excluded per-leg.
    excluded_collections must be [] — legs are excluded only for embedding model
    mismatches, never for zero-result types within a contributing leg (S1, S4).

    Applied filters must echo the normalised value 'md' (leading dot stripped).
    """
    md_file_a = tmp_path / "guide_a.md"
    md_file_a.write_text(
        "# Setup Guide Alpha\n\nMarkdown guide for setup and configuration.\n" * 5
    )
    md_file_b = tmp_path / "guide_b.md"
    md_file_b.write_text(
        "# Setup Guide Beta\n\nMarkdown configuration and installation guide.\n" * 5
    )
    py_file_b = tmp_path / "main.py"
    py_file_b.write_text(
        "# Python module\ndef setup():\n    '''Setup configuration.'''\n    pass\n" * 5
    )

    with make_real_app(tmp_path, monkeypatch) as (client, _cfg, api_key):
        col_a = "t1-md-col"
        col_b = "t1-py-col"

        # col_a: .md only; col_b: .md + .py (both types)
        ingest_file_via_path(client, col_a, str(md_file_a), api_key=api_key)
        ingest_file_via_path(client, col_b, str(md_file_b), api_key=api_key)
        ingest_file_via_path(client, col_b, str(py_file_b), api_key=api_key)

        resp = client.post(
            "/search",
            json={
                "collections": [col_a, col_b],
                "query": "setup guide configuration",
                "filters": {"file_type": ".md"},  # leading dot — must be normalised
            },
            headers=_auth(api_key),
        )
        assert resp.status_code == 200, (
            f"expected 200 (multi-collection + filter now permitted), "
            f"got {resp.status_code}: {resp.text}"
        )
        data = resp.json()

        # applied_filters must echo the normalised file_type (S1)
        applied = data.get("applied_filters")
        assert applied is not None, "applied_filters must be non-null when filters were submitted"
        assert applied.get("file_type") == "md", (
            f"expected normalised 'md' (dot stripped), got: {applied.get('file_type')!r}"
        )

        # Results must come from BOTH collections — per-leg filtering kept .md from each (S1)
        results = data["results"]
        assert results, "expected non-empty results from .md filter across both collections"
        seen_collections = {r["collection"] for r in results}
        assert col_a in seen_collections, (
            f"expected results from {col_a!r} in multi-collection .md filter; "
            f"seen: {seen_collections}"
        )
        assert col_b in seen_collections, (
            f"expected results from {col_b!r} in multi-collection .md filter; "
            f"seen: {seen_collections}"
        )

        # Only .md files must appear — .py from col_b must be excluded per-leg (S1)
        for r in results:
            assert r["file_type"] == "md", (
                f"non-md result slipped through per-leg filter: file_type={r['file_type']!r}"
            )

        # excluded_collections must be empty — filter legs are silent (never listed) (S4)
        excluded = data.get("excluded_collections", [])
        assert excluded == [], (
            f"excluded_collections must be [] for zero-result filter legs; got: {excluded}"
        )


# ---------------------------------------------------------------------------
# T-1 / test 2 — language + source_path_prefix combined filter (S2)
# ---------------------------------------------------------------------------


def test_e2e_filter_multi_collection_language_prefix_combined(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Multi-collection search with combined language + source_path_prefix filter.

    With no real language detector in the test environment, chunks have language=""
    (undetected).  A language filter of "en" will produce an empty result — this
    is correct and expected behaviour (language SQL match, no match → no results).

    Key assertions (S2):
    - Response is 200 (combined filter now accepted — was previously rejected as 422).
    - applied_filters echoes BOTH filter fields (source_path_prefix + language).
    - excluded_collections is [] (zero-result legs are silent).
    """
    doc_a = tmp_path / "en_guide.md"
    doc_a.write_text(
        "# English Setup Guide\n\nThis is an English language guide.\n" * 5
    )
    doc_b = tmp_path / "fr_guide.md"
    doc_b.write_text(
        "# Guide de configuration\n\nCeci est un guide en français.\n" * 5
    )

    with make_real_app(tmp_path, monkeypatch) as (client, _cfg, api_key):
        col_a = "t1-lang-a"
        col_b = "t1-lang-b"

        ingest_file_via_path(client, col_a, str(doc_a), api_key=api_key)
        ingest_file_via_path(client, col_b, str(doc_b), api_key=api_key)

        resp = client.post(
            "/search",
            json={
                "collections": [col_a, col_b],
                "query": "guide setup configuration",
                "filters": {
                    "source_path_prefix": str(tmp_path) + "/",
                    "language": "en",
                },
            },
            headers=_auth(api_key),
        )
        assert resp.status_code == 200, (
            f"expected 200 for combined language+source_path_prefix filter, "
            f"got {resp.status_code}: {resp.text}"
        )
        data = resp.json()

        # applied_filters must echo BOTH filter fields (S2)
        applied = data.get("applied_filters")
        assert applied is not None, "applied_filters must be non-null when filters were submitted"
        assert applied.get("source_path_prefix") == str(tmp_path) + "/", (
            f"applied_filters.source_path_prefix mismatch: {applied.get('source_path_prefix')!r}"
        )
        assert applied.get("language") == "en", (
            f"applied_filters.language must echo 'en', got: {applied.get('language')!r}"
        )

        # Zero-result legs are silent — excluded_collections is always []
        excluded = data.get("excluded_collections", [])
        assert excluded == [], (
            f"excluded_collections must be empty for zero-result legs; got: {excluded}"
        )

        # The language detector stub assigns language="" to all chunks in the test
        # environment, so language="en" matches nothing — results is expected empty.
        # This verifies the language filter path runs without error (not silently ignored).
        assert data["results"] == [], (
            f"expected empty results: stub language detector assigns language='', "
            f"so language='en' filter matches nothing; got: {data['results']}"
        )


# ---------------------------------------------------------------------------
# T-1 / test 3 — all-empty result: 200, empty list, applied_filters non-null (S5)
# ---------------------------------------------------------------------------


def test_e2e_filter_multi_collection_all_empty(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Filter matching no documents in any collection returns 200, results=[], applied_filters set.

    Both collections have .md and .py files; filtering by .xyz matches nothing (S5).
    """
    md_file = tmp_path / "readme.md"
    md_file.write_text("# README\n\nProject overview and documentation.\n" * 5)
    py_file = tmp_path / "main.py"
    py_file.write_text("# main\ndef run():\n    '''Run the application.'''\n    pass\n" * 5)

    with make_real_app(tmp_path, monkeypatch) as (client, _cfg, api_key):
        col_a = "t1-empty-a"
        col_b = "t1-empty-b"

        ingest_file_via_path(client, col_a, str(md_file), api_key=api_key)
        ingest_file_via_path(client, col_b, str(py_file), api_key=api_key)

        resp = client.post(
            "/search",
            json={
                "collections": [col_a, col_b],
                "query": "project overview documentation",
                "filters": {"file_type": ".xyz"},  # matches nothing
            },
            headers=_auth(api_key),
        )
        assert resp.status_code == 200, (
            f"expected 200 even when filter matches nothing, "
            f"got {resp.status_code}: {resp.text}"
        )
        data = resp.json()
        assert data["results"] == [], (
            f"expected empty results list, got: {data['results']}"
        )

        applied = data.get("applied_filters")
        assert applied is not None, (
            "applied_filters must be non-null even when no results match the filter (S5)"
        )
        assert applied.get("file_type") == "xyz", (
            f"applied_filters.file_type must be 'xyz' (dot stripped), "
            f"got: {applied.get('file_type')!r}"
        )


# ---------------------------------------------------------------------------
# T-1 / test 4 — source_path_glob per-leg post-filter (S6)
# ---------------------------------------------------------------------------


def test_e2e_filter_multi_collection_glob(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """source_path_glob filter is applied per-leg as a Python post-filter (S6).

    Collection A has .md files; Collection B has .txt files.
    A glob filter of '*.md' must return only results from the .md leg.
    Collection B (only .txt) contributes nothing and is not listed in excluded_collections.
    """
    md_file = tmp_path / "design.md"
    md_file.write_text(
        "# Design Document\n\nArchitecture design and system overview.\n" * 5
    )
    txt_file = tmp_path / "notes.txt"
    txt_file.write_text(
        "Project notes. Architecture and system design overview.\n" * 5
    )

    with make_real_app(tmp_path, monkeypatch) as (client, _cfg, api_key):
        col_a = "t1-glob-md"
        col_b = "t1-glob-txt"

        ingest_file_via_path(client, col_a, str(md_file), api_key=api_key)
        ingest_file_via_path(client, col_b, str(txt_file), api_key=api_key)

        resp = client.post(
            "/search",
            json={
                "collections": [col_a, col_b],
                "query": "design architecture system",
                "filters": {"source_path_glob": "*.md"},
            },
            headers=_auth(api_key),
        )
        assert resp.status_code == 200, (
            f"expected 200 for glob filter with multi-collection, "
            f"got {resp.status_code}: {resp.text}"
        )
        data = resp.json()

        applied = data.get("applied_filters")
        assert applied is not None, "applied_filters must be non-null when filters submitted"
        assert applied.get("source_path_glob") == "*.md", (
            f"applied_filters.source_path_glob must echo '*.md', "
            f"got: {applied.get('source_path_glob')!r}"
        )

        # All results must have .md suffix (glob post-filter applied per-leg)
        results = data["results"]
        assert results, "expected non-empty results from the .md glob filter"
        for r in results:
            assert r["source_path"].endswith(".md"), (
                f"non-.md result passed glob filter: source_path={r['source_path']!r}"
            )

        # Zero-result leg (col_b with .txt) must NOT appear in excluded_collections
        excluded = data.get("excluded_collections", [])
        assert excluded == [], (
            f"excluded_collections must be [] for zero-result filter legs; got: {excluded}"
        )


# ---------------------------------------------------------------------------
# T-1 / test 5 — single-collection + filter unchanged (S10 regression)
# ---------------------------------------------------------------------------


def test_e2e_single_collection_filter_regression(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Single-collection search with language filter is unaffected by E0e changes (S10).

    Plan S10 specifies filters: {language: "en"} — this is the filter whose restriction
    was changed by E0e (previously single-collection only, now multi-collection too).
    The language detector is stubbed in the test environment, so all chunks have
    language="", meaning language="en" matches nothing — results are expected empty.
    The key assertion is status 200: proves the single-collection language path was
    not accidentally broken by E0e.
    """
    doc_file = tmp_path / "api_docs.md"
    doc_file.write_text(
        "# API Documentation\n\nComplete REST API reference and examples.\n" * 5
    )

    with make_real_app(tmp_path, monkeypatch) as (client, _cfg, api_key):
        col = "t1-single-col"
        ingest_file_via_path(client, col, str(doc_file), api_key=api_key)

        resp = client.post(
            "/search",
            json={
                "collection": col,
                "query": "api endpoint documentation",
                "filters": {"language": "en"},
            },
            headers=_auth(api_key),
        )
        assert resp.status_code == 200, (
            f"expected 200 for single-collection + language filter (S10 regression), "
            f"got {resp.status_code}: {resp.text}"
        )
        data = resp.json()

        # applied_filters must echo the language filter even in single-collection mode
        applied = data.get("applied_filters")
        assert applied is not None, (
            "applied_filters must be non-null when language filter was submitted"
        )
        assert applied.get("language") == "en", (
            f"applied_filters.language must be 'en', got: {applied.get('language')!r}"
        )

        # Language detector stub assigns language="" to all chunks, so "en" matches nothing.
        # 200 with empty results is the correct, non-regressed behaviour for single-collection
        # language filter (S10).
        assert data["results"] == [], (
            f"expected empty results: stub language detector assigns language='', "
            f"so language='en' filter matches nothing; got: {data['results']}"
        )


# ---------------------------------------------------------------------------
# T-1 / test 6 — multi-collection, no filter: applied_filters null (S11 regression)
# ---------------------------------------------------------------------------


def test_e2e_multi_collection_no_filter_regression(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Multi-collection search without filters returns applied_filters: null (S11).

    Behaviour must be identical to pre-E0e: results merged from both legs,
    applied_filters absent from the response (null).
    """
    doc_a = tmp_path / "corpus_a.md"
    doc_a.write_text(
        "# Corpus Alpha\n\nThis is alpha collection content for multi-collection search.\n" * 5
    )
    doc_b = tmp_path / "corpus_b.md"
    doc_b.write_text(
        "# Corpus Beta\n\nThis is beta collection content for multi-collection search.\n" * 5
    )

    with make_real_app(tmp_path, monkeypatch) as (client, _cfg, api_key):
        col_a = "t1-nofilter-a"
        col_b = "t1-nofilter-b"

        ingest_file_via_path(client, col_a, str(doc_a), api_key=api_key)
        ingest_file_via_path(client, col_b, str(doc_b), api_key=api_key)

        resp = client.post(
            "/search",
            json={
                "collections": [col_a, col_b],
                "query": "corpus collection content",
            },
            headers=_auth(api_key),
        )
        assert resp.status_code == 200, (
            f"expected 200 for multi-collection no-filter, got {resp.status_code}: {resp.text}"
        )
        data = resp.json()

        # applied_filters must be null when no filters submitted (S11)
        assert data.get("applied_filters") is None, (
            f"applied_filters must be null when no filter submitted; "
            f"got: {data.get('applied_filters')}"
        )

        # Results should be present from both collections (regression guard)
        results = data["results"]
        assert results, "expected non-empty results from multi-collection no-filter search"
        seen_collections = {r["collection"] for r in results}
        assert col_a in seen_collections, (
            f"expected results from {col_a!r}, seen: {seen_collections}"
        )
        assert col_b in seen_collections, (
            f"expected results from {col_b!r}, seen: {seen_collections}"
        )


# ---------------------------------------------------------------------------
# T-1 / test 7 — indexed_after temporal filter (multi-collection)
# ---------------------------------------------------------------------------


def test_e2e_filter_multi_collection_indexed_after(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """indexed_after filter applied to multi-collection search.

    Strategy: ingest first batch → sleep briefly → capture cutoff timestamp →
    ingest second batch.  Filter with indexed_after=cutoff must return only
    docs from the second batch.

    A sleep of 50 ms between ingest calls ensures the two batches have distinct
    indexed_at timestamps — more reliable than relying on natural microsecond jitter.
    """
    first_file = tmp_path / "first_batch.md"
    first_file.write_text(
        "# First Batch Document\n\nThis document was indexed in the first batch.\n" * 5
    )
    second_file = tmp_path / "second_batch.md"
    second_file.write_text(
        "# Second Batch Document\n\nThis document was indexed in the second batch.\n" * 5
    )

    with make_real_app(tmp_path, monkeypatch) as (client, _cfg, api_key):
        col_first = "t1-ts-first"
        col_second = "t1-ts-second"

        # First ingest
        ingest_file_via_path(client, col_first, str(first_file), api_key=api_key)

        # Sleep to ensure the timestamp boundary is after the first ingest
        time.sleep(0.05)
        cutoff = datetime.now(UTC)
        # Sleep again to ensure the second ingest is after the cutoff
        time.sleep(0.05)

        # Second ingest
        ingest_file_via_path(client, col_second, str(second_file), api_key=api_key)

        resp = client.post(
            "/search",
            json={
                "collections": [col_first, col_second],
                "query": "batch document indexed",
                "filters": {"indexed_after": cutoff.isoformat()},
            },
            headers=_auth(api_key),
        )
        assert resp.status_code == 200, (
            f"expected 200 for indexed_after filter, got {resp.status_code}: {resp.text}"
        )
        data = resp.json()

        applied = data.get("applied_filters")
        assert applied is not None, "applied_filters must be non-null when filter submitted"
        indexed_after_echoed = applied.get("indexed_after")
        assert indexed_after_echoed, "applied_filters.indexed_after must be echoed in response"
        assert isinstance(indexed_after_echoed, str), (
            f"applied_filters.indexed_after must serialize as a string, "
            f"got: {type(indexed_after_echoed).__name__!r}"
        )

        # Only docs from the second ingest should appear
        results = data["results"]
        assert results, "expected non-empty results from the second ingest batch"
        seen_collections = {r["collection"] for r in results}
        assert col_first not in seen_collections, (
            f"first-batch collection {col_first!r} must be excluded by indexed_after filter; "
            f"got collections: {seen_collections}"
        )
        assert col_second in seen_collections, (
            f"second-batch collection {col_second!r} must appear in results; "
            f"got collections: {seen_collections}"
        )


# ---------------------------------------------------------------------------
# T-1 / test 8 — indexed_before temporal filter (multi-collection)
# ---------------------------------------------------------------------------


def test_e2e_filter_multi_collection_indexed_before(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """indexed_before filter applied to multi-collection search.

    Strategy: ingest first batch → sleep briefly (50 ms) → capture cutoff timestamp →
    sleep briefly (50 ms) → ingest second batch.  Filter with indexed_before=cutoff must
    return only docs from the first batch.
    """
    early_file = tmp_path / "early.md"
    early_file.write_text(
        "# Early Document\n\nThis document was indexed early, before the cutoff.\n" * 5
    )
    late_file = tmp_path / "late.md"
    late_file.write_text(
        "# Late Document\n\nThis document was indexed late, after the cutoff.\n" * 5
    )

    with make_real_app(tmp_path, monkeypatch) as (client, _cfg, api_key):
        col_early = "t1-before-early"
        col_late = "t1-before-late"

        # First ingest (early documents)
        ingest_file_via_path(client, col_early, str(early_file), api_key=api_key)

        # Sleep to ensure the cutoff is after the first ingest completes
        time.sleep(0.05)
        cutoff = datetime.now(UTC)
        # Sleep again to ensure the second ingest is strictly after the cutoff
        time.sleep(0.05)

        # Second ingest (late documents)
        ingest_file_via_path(client, col_late, str(late_file), api_key=api_key)

        resp = client.post(
            "/search",
            json={
                "collections": [col_early, col_late],
                "query": "document indexed cutoff",
                "filters": {"indexed_before": cutoff.isoformat()},
            },
            headers=_auth(api_key),
        )
        assert resp.status_code == 200, (
            f"expected 200 for indexed_before filter, got {resp.status_code}: {resp.text}"
        )
        data = resp.json()

        applied = data.get("applied_filters")
        assert applied is not None, "applied_filters must be non-null when filter submitted"
        indexed_before_echoed = applied.get("indexed_before")
        assert indexed_before_echoed, "applied_filters.indexed_before must be echoed in response"
        assert isinstance(indexed_before_echoed, str), (
            f"applied_filters.indexed_before must serialize as a string, "
            f"got: {type(indexed_before_echoed).__name__!r}"
        )

        # Only early docs (before cutoff) should appear
        results = data["results"]
        assert results, "expected non-empty results from the early ingest batch"
        seen_collections = {r["collection"] for r in results}
        assert col_late not in seen_collections, (
            f"late-batch collection {col_late!r} must be excluded by indexed_before filter; "
            f"got collections: {seen_collections}"
        )
        assert col_early in seen_collections, (
            f"early-batch collection {col_early!r} must appear in results; "
            f"got collections: {seen_collections}"
        )
