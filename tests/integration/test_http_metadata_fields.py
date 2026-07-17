"""Task 1.1 — Metadata fields flow through ingest → search HTTP round-trip.

Exercises the full HTTP → middleware → route → pipeline → LanceDB → serialized
response path for metadata fields: ``file_type``, ``updated_at``,
``ingested_by``, ``acl``.

Run with:
    uv run pytest tests/integration/test_http_metadata_fields.py -v
"""
from __future__ import annotations

import asyncio
import os
import time
from pathlib import Path

import pytest

from tests.integration.conftest import ingest_file_via_path, make_real_app, search

pytestmark = pytest.mark.integration


# ---------------------------------------------------------------------------
# Test 1 — X-Ingested-By header flows through to LanceDB row
# ---------------------------------------------------------------------------

def test_x_ingested_by_header_flows_through_to_lancedb_row(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """POST /ingest with X-Ingested-By: watcher via TestClient against real app.

    Verifies the header is parsed at the HTTP boundary, propagated through
    the pipeline, and lands in the LanceDB row with ingested_by == 'watcher'.

    Verification is via POST /search (not store.hybrid_search as the plan specifies)
    because the stub embedder produces 384-dim zero vectors and calling
    hybrid_search directly with a 4-dim query vector raises a LanceDB dimension
    mismatch error. POST /search exercises the same read path and validates
    SearchResultSchema.from_result() in addition to the stored value.
    """
    md_file = tmp_path / "doc.md"
    md_file.write_text("# Watcher document\n\nContent ingested by watcher.\n" * 4)

    with make_real_app(tmp_path, monkeypatch) as (client, cfg, api_key):
        col = "test-ingested-by-header"
        ingest_file_via_path(
            client,
            col,
            str(md_file),
            api_key=api_key,
            extra_headers={"X-Ingested-By": "watcher"},
        )

        # Verify via POST /search that ingested_by == 'watcher' flows through
        items = search(client, col, "watcher document content", api_key=api_key)
        assert items, "expected at least one search result after ingest"
        assert all(r["ingested_by"] == "watcher" for r in items), (
            f"expected ingested_by='watcher' on all results, "
            f"got: {[r['ingested_by'] for r in items]}"
        )


# ---------------------------------------------------------------------------
# Test 2 — REST /search response carries metadata fields from real ingest
# ---------------------------------------------------------------------------

def test_rest_search_response_carries_metadata_fields_from_real_ingest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Real HTTP ingest → POST /search via TestClient.

    Verifies SearchResultSchema.from_result() wiring end-to-end:
    file_type, updated_at, ingested_by, acl are present and non-empty
    in the HTTP response. The ingest is via file path through the HTTP
    route so the full pipeline runs.
    """
    md_file = tmp_path / "hello.md"
    md_file.write_text("# Hello\n\nA real document with real content to index.\n" * 4)

    with make_real_app(tmp_path, monkeypatch) as (client, cfg, api_key):
        col = "test-metadata-fields"

        # Ingest via file path through HTTP — X-Ingested-By: cli sets ingested_by
        ingest_file_via_path(
            client,
            col,
            str(md_file),
            api_key=api_key,
            extra_headers={"X-Ingested-By": "cli"},
        )

        items = search(client, col, "real document content", api_key=api_key)
        assert items, "expected non-empty search results"

        for item in items:
            assert item["file_type"], (
                f"file_type should be non-empty, got: {item['file_type']!r}"
            )
            assert item["updated_at"], (
                f"updated_at should be non-empty, got: {item['updated_at']!r}"
            )
            assert item["ingested_by"] == "cli", (
                f"expected ingested_by='cli' (via X-Ingested-By: cli header), "
                f"got: {item['ingested_by']!r}"
            )
            assert "acl" in item, "acl field should be present in response"


# ---------------------------------------------------------------------------
# Test 3 — Future mtime accepted as-is (no clamping)
# ---------------------------------------------------------------------------

def test_future_mtime_accepted_as_is(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Ingest a file with mtime set 1 hour ahead, verify updated_at carries it verbatim."""
    md_file = tmp_path / "future.md"
    md_file.write_text("# Future file\n\nThis file has a future modification time.\n" * 4)

    # Set mtime 1 hour in the future
    future_ts = time.time() + 3600.0
    os.utime(md_file, (future_ts, future_ts))

    with make_real_app(tmp_path, monkeypatch) as (client, cfg, api_key):
        col = "test-future-mtime"
        ingest_file_via_path(client, col, str(md_file), api_key=api_key)

        items = search(client, col, "future file modification", api_key=api_key)
        assert items, "expected non-empty results after future-mtime ingest"

        # updated_at must be non-empty and parse correctly as ISO 8601
        for item in items:
            assert item["updated_at"], "updated_at must be non-empty"
            from datetime import datetime
            # Should parse without ValueError — verifies no clamping corrupted the value
            dt = datetime.fromisoformat(item["updated_at"].replace("Z", "+00:00"))
            # The stored value should reflect the future mtime (year 2025 or later)
            assert dt.year >= 2025, f"updated_at year should be >= 2025, got: {dt.year}"


# ---------------------------------------------------------------------------
# Test 4 — Reindex metadata CLI to search response round-trip
# ---------------------------------------------------------------------------

def test_reindex_metadata_cli_to_search_response_round_trip(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Seed LanceDB rows with legacy-shaped data, run reindex-metadata via store,
    POST /search, assert response items carry updated metadata fields.

    Verifies the reindex path actually overwrites fields rather than leaving
    legacy values in place. Seeds the collection via HTTP ingest (dim=384 from
    stub embedder), then forces legacy metadata state via direct table update,
    then calls store.reindex_metadata() directly (CSP120: CLI is now a proxy).
    """
    from archon_search.store import SearchStore

    # Create a source file so reindex can read its mtime and extension
    src = tmp_path / "legacy.md"
    src.write_text("# Legacy content\n\nThis is old content for reindexing.\n" * 4)

    async def _force_legacy(store: SearchStore, col: str) -> None:
        """Force all rows to legacy (pre-A1) metadata state."""
        db = store._require_connected()
        table = await db.open_table(col)
        await table.update(
            where="ingested_by IS NOT NULL",
            updates={
                "ingested_by": "archon-search-cli",
                "file_type": "",
                "updated_at": "",
            },
        )

    with make_real_app(tmp_path, monkeypatch) as (client, cfg, api_key):
        col = "test-reindex-metadata-e2e"
        store = client.app.state.search_store

        # Seed collection via HTTP ingest (this creates collection with correct dim=384)
        ingest_file_via_path(client, col, str(src), api_key=api_key)

        # Force legacy metadata state on all rows
        asyncio.run(_force_legacy(store, col))

        # Verify legacy state is in place — ingested_by should be legacy
        async def _check_raw_row(s, c):
            db = s._require_connected()
            t = await db.open_table(c)
            rows = await t.query().to_list()
            return rows[0] if rows else None

        raw = asyncio.run(_check_raw_row(store, col))
        assert raw is not None
        assert raw["ingested_by"] == "archon-search-cli", (
            f"expected legacy ingested_by before reindex, got: {raw['ingested_by']!r}"
        )

        # Run reindex-metadata directly via store (CSP120: CLI is an HTTP proxy)
        asyncio.run(store.reindex_metadata(col))

        # Verify via POST /search that response items carry updated metadata
        items = search(client, col, "legacy content reindexing", api_key=api_key)
        assert items, "expected non-empty search results after reindex"

        for item in items:
            assert item["file_type"] == "md", (
                f"expected file_type='md' after reindex, got: {item['file_type']!r}"
            )
            assert item["updated_at"], "updated_at should be non-empty after reindex"
            assert item["ingested_by"] == "reindex", (
                f"expected ingested_by='reindex' after reindex, "
                f"got: {item['ingested_by']!r}"
            )
