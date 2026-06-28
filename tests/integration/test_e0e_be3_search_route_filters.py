"""tests/integration/test_e0e_be3_search_route_filters.py

BE-3 integration tests: POST /search with filters + multi-collection via TestClient.

Plan task: BE-3 — Remove `SearchRequest` v1 restriction; wire `filters` +
`applied_filters` through the `POST /search` handler.

Run with:
    uv run pytest tests/integration/test_e0e_be3_search_route_filters.py -v
"""
from __future__ import annotations

from pathlib import Path

import pytest

from tests.integration.conftest import ingest_file_via_path, make_real_app

pytestmark = pytest.mark.integration


def _auth(api_key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {api_key}"}


# ---------------------------------------------------------------------------
# Test 1 — multi-collection + file_type filter returns 200 with filtered results
# ---------------------------------------------------------------------------

def test_post_search_multi_collection_with_file_type_filter(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """POST /search with collections + file_type filter returns 200, filtered results, echoed
    applied_filters.

    Collection A has .md files; Collection B has .py files.  Sending filters.file_type='.md'
    (with leading dot, which must be normalised away) must:
    - Return 200 (previously this request returned 422 due to the v1 restriction)
    - Return results from col-a only (the .md leg)
    - Echo applied_filters.file_type as 'md' (leading dot stripped by SearchFilters validator)
    """
    md_file = tmp_path / "guide.md"
    md_file.write_text(
        "# Setup Guide\n\nMarkdown guide for setup and configuration.\n" * 5
    )
    py_file = tmp_path / "main.py"
    py_file.write_text(
        "# Python module\ndef setup():\n    '''Setup configuration.'''\n    pass\n" * 5
    )

    with make_real_app(tmp_path, monkeypatch) as (client, cfg, api_key):
        col_a = "be3-md-col"
        col_b = "be3-py-col"

        ingest_file_via_path(client, col_a, str(md_file), api_key=api_key)
        ingest_file_via_path(client, col_b, str(py_file), api_key=api_key)

        resp = client.post(
            "/search",
            json={
                "collections": [col_a, col_b],
                "query": "setup guide configuration",
                "filters": {"file_type": ".md"},  # leading dot — must be normalised to 'md'
            },
            headers=_auth(api_key),
        )
        assert resp.status_code == 200, (
            f"expected 200 (v1 restriction removed), got {resp.status_code}: {resp.text}"
        )
        data = resp.json()

        # applied_filters must echo the parsed, normalised filter
        applied = data.get("applied_filters")
        assert applied is not None, "applied_filters must not be null when filters provided"
        assert applied.get("file_type") == "md", (
            f"expected 'md' (dot stripped), got: {applied.get('file_type')!r}"
        )

        # Results must only come from the .md collection leg
        results = data["results"]
        assert results, "expected non-empty results from .md collection leg"
        for r in results:
            assert r["file_type"] == "md", (
                f"non-md result slipped through filter: file_type={r['file_type']!r}"
            )
            assert r["collection"] == col_a, (
                f"result from wrong collection: expected {col_a!r}, got {r['collection']!r}"
            )


# ---------------------------------------------------------------------------
# Test 2 — multi-collection without filters returns applied_filters: null
# ---------------------------------------------------------------------------

def test_post_search_multi_collection_no_filters_applied_filters_null(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """POST /search with collections but no filters returns applied_filters: null (S3, S11).

    The response field must be null (not an empty object) when no filters are sent.
    """
    doc_a = tmp_path / "doc_a.md"
    doc_a.write_text("# Collection Alpha\n\nContent for alpha collection.\n" * 5)
    doc_b = tmp_path / "doc_b.md"
    doc_b.write_text("# Collection Beta\n\nContent for beta collection.\n" * 5)

    with make_real_app(tmp_path, monkeypatch) as (client, cfg, api_key):
        col_a = "be3-null-a"
        col_b = "be3-null-b"

        ingest_file_via_path(client, col_a, str(doc_a), api_key=api_key)
        ingest_file_via_path(client, col_b, str(doc_b), api_key=api_key)

        resp = client.post(
            "/search",
            json={
                "collections": [col_a, col_b],
                "query": "content collection",
            },
            headers=_auth(api_key),
        )
        assert resp.status_code == 200, f"expected 200, got {resp.status_code}: {resp.text}"
        data = resp.json()
        assert data.get("applied_filters") is None, (
            f"applied_filters must be null when no filters sent, "
            f"got: {data.get('applied_filters')}"
        )


# ---------------------------------------------------------------------------
# Test 3 — multi-collection filter that matches nothing returns 200 + empty results
# ---------------------------------------------------------------------------

def test_post_search_multi_collection_all_empty_after_filter(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """POST /search with filter matching no documents returns 200, empty results, non-null
    applied_filters (S5).

    Collections have .md and .py files; filtering by .xyz matches nothing.
    The response must be 200 with results=[], applied_filters non-null.
    """
    md_file = tmp_path / "file.md"
    md_file.write_text("# Document\n\nThis is some content for searching.\n" * 5)
    py_file = tmp_path / "file.py"
    py_file.write_text("# Python\ndef run():\n    '''Run the program.'''\n    pass\n" * 5)

    with make_real_app(tmp_path, monkeypatch) as (client, cfg, api_key):
        col_a = "be3-empty-a"
        col_b = "be3-empty-b"

        ingest_file_via_path(client, col_a, str(md_file), api_key=api_key)
        ingest_file_via_path(client, col_b, str(py_file), api_key=api_key)

        resp = client.post(
            "/search",
            json={
                "collections": [col_a, col_b],
                "query": "content document",
                "filters": {"file_type": ".xyz"},  # matches nothing in either collection
            },
            headers=_auth(api_key),
        )
        assert resp.status_code == 200, (
            f"expected 200 even when filter matches nothing, got {resp.status_code}: {resp.text}"
        )
        data = resp.json()
        assert data["results"] == [], (
            f"expected empty results list when filter matches nothing, got: {data['results']}"
        )
        applied = data.get("applied_filters")
        assert applied is not None, (
            "applied_filters must be non-null even when no results match the filter"
        )
        assert applied.get("file_type") == "xyz", (
            f"applied_filters.file_type must be 'xyz' (dot stripped), "
            f"got: {applied.get('file_type')!r}"
        )


# ---------------------------------------------------------------------------
# Test 4 — applied_filters.indexed_after is serialised as a well-formed ISO 8601 UTC string
# ---------------------------------------------------------------------------

def test_post_search_applied_filters_datetime_serialization(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """POST /search with indexed_after date string echoes a well-formed ISO 8601 UTC datetime.

    Send indexed_after="2024-01-15" (date-only, 10-char string).  SearchFilters coerces
    this to datetime(2024, 1, 15, 0, 0, 0, tzinfo=UTC) internally.  The serialised
    applied_filters.indexed_after in the JSON response must:
    - Be a string (not null, not a number)
    - Start with "2024-01-15T00:00:00"
    - Carry a UTC indicator ("Z" or "+00:00")
    """
    doc = tmp_path / "dated.md"
    doc.write_text("# Dated Document\n\nContent for datetime serialization test.\n" * 5)

    with make_real_app(tmp_path, monkeypatch) as (client, cfg, api_key):
        col = "be3-datetime"
        ingest_file_via_path(client, col, str(doc), api_key=api_key)

        resp = client.post(
            "/search",
            json={
                "collections": [col],
                "query": "dated document content",
                "filters": {"indexed_after": "2024-01-15"},
            },
            headers=_auth(api_key),
        )
        assert resp.status_code == 200, f"expected 200, got {resp.status_code}: {resp.text}"
        data = resp.json()

        applied = data.get("applied_filters")
        assert applied is not None, "applied_filters must not be null when filters provided"

        indexed_after = applied.get("indexed_after")
        assert indexed_after is not None, "applied_filters.indexed_after must not be null"
        assert isinstance(indexed_after, str), (
            f"applied_filters.indexed_after must be a JSON string, "
            f"got type {type(indexed_after).__name__!r}: {indexed_after!r}"
        )
        # date-only "2024-01-15" must be coerced to start-of-day UTC
        assert indexed_after.startswith("2024-01-15T00:00:00"), (
            f"expected datetime coerced to start of 2024-01-15 UTC, "
            f"got: {indexed_after!r}"
        )
        # must carry a UTC indicator
        assert indexed_after.endswith("Z") or indexed_after.endswith("+00:00"), (
            f"expected UTC indicator ('Z' or '+00:00') in applied_filters.indexed_after, "
            f"got: {indexed_after!r}"
        )
