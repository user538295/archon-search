"""E2d T-3: e2e test for GET /status graph.stale_mention_count and maintenance.last_graph_gc_at fields.

Scenario covered:
- S5: After at least one GC pass, GET /status contains:
  - graph.stale_mention_count: cached integer (O(1) read from state file)
  - maintenance.last_graph_gc_at: ISO-8601 timestamp

Test:
  test_e2d_t3_status_stale_mention_count_zero_after_clean_gc:
    Before GC: verify that status fields are absent or null (pre-condition).
    Ingest a document (clean state with no stale mentions).
    POST /maintenance/trigger to run GC.
    GET /status and verify:
      - graph.stale_mention_count == 0 (no stale data; freshly ingested)
      - maintenance.last_graph_gc_at is a non-null ISO-8601 timestamp
    Verify caching: call GET /status again and assert same stale_mention_count value.

Run with:
    uv run pytest tests/integration/test_e2d_t3_status_fields_gc.py -n0 -v --no-cov
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest

from tests.integration.conftest import (
    ingest_file_via_path,
    install_spacy_stub,
    make_real_app,
)
from tests.integration.test_e2d_t2_graph_gc_e2e import (
    _auth,
    _trigger_and_poll_maintenance,
)

pytestmark = pytest.mark.integration


# ---------------------------------------------------------------------------
# Test: test_e2d_t3_status_stale_mention_count_zero_after_clean_gc
# ---------------------------------------------------------------------------


def test_e2d_t3_status_stale_mention_count_zero_after_clean_gc(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """GET /status contains stale_mention_count and last_graph_gc_at after clean GC pass.

    Scenario S5 — after at least one GC pass:
    - graph.stale_mention_count is a cached integer (== 0 in clean state)
    - maintenance.last_graph_gc_at is a non-null ISO-8601 timestamp
    - The value is cached (multiple reads return the same value)

    Steps:
    1. BEFORE GC: verify that last_graph_gc_at is null (pre-condition).
    2. Ingest a clean document (no deletions, no stale state).
    3. POST /maintenance/trigger to run GC.
    4. GET /status and verify:
       - graph.stale_mention_count == 0 (clean ingestion = no stale mentions)
       - maintenance.last_graph_gc_at is a non-null ISO-8601 timestamp
    5. GET /status again and verify the value is cached (same value returned).
    """
    install_spacy_stub(monkeypatch)

    col = "clean-gc-col"

    # Clean document: contains "Alice" which the stub will extract.
    doc_file = tmp_path / "clean_doc.txt"
    doc_file.write_text(
        "Alice works at Google. Alice is a great engineer.\n" * 5,
        encoding="utf-8",
    )

    with make_real_app(tmp_path, monkeypatch, graph_enabled=True, maintenance_enabled=True) as (
        client,
        cfg,
        api_key,
    ):
        # Step 1: BEFORE GC — verify that last_graph_gc_at is null (pre-condition).
        status_before = client.get("/status", headers=_auth(api_key))
        assert status_before.status_code == 200, (
            f"GET /status before GC failed: {status_before.status_code}"
        )
        status_before_json = status_before.json()
        maint_before = status_before_json.get("maintenance")
        assert maint_before is not None, (
            "maintenance block must be present before GC"
        )
        last_graph_gc_at_before = maint_before.get("last_graph_gc_at")
        assert last_graph_gc_at_before is None, (
            f"maintenance.last_graph_gc_at must be null BEFORE any GC pass (pre-condition). "
            f"Got {last_graph_gc_at_before!r}. "
            f"Full maintenance block: {maint_before}"
        )

        # Step 2: Ingest a clean document (no deletions — no stale state).
        ingest_file_via_path(client, col, str(doc_file), api_key=api_key)

        # Step 3: POST /maintenance/trigger and wait for GC to complete.
        status_after_gc = _trigger_and_poll_maintenance(
            client, api_key, prev_last_run_at=None
        )

        # Step 4: Verify graph.stale_mention_count and maintenance.last_graph_gc_at.

        # S5: graph.stale_mention_count is a cached integer from the GC pass.
        # In clean state (no deletions, no stale mentions), it must be 0.
        graph_status = status_after_gc.get("graph")
        assert graph_status is not None, (
            "graph status sub-object must be non-null when graph.enabled=True (S5). "
            f"Full status: {status_after_gc}"
        )
        assert "stale_mention_count" in graph_status, (
            "graph.stale_mention_count must be present in graph status (S5). "
            f"graph sub-object keys: {list(graph_status.keys())}"
        )
        stale_count = graph_status["stale_mention_count"]
        assert isinstance(stale_count, int), (
            f"graph.stale_mention_count must be an integer; got {type(stale_count).__name__} "
            f"(value={stale_count!r})"
        )
        assert stale_count == 0, (
            f"graph.stale_mention_count must be 0 in clean state (no deletions, no stale mentions). "
            f"Got {stale_count}. "
            "GC may have incorrectly classified live mentions as stale, or the counter is off."
        )

        # S5: maintenance.last_graph_gc_at is a non-null ISO-8601 timestamp.
        maint = status_after_gc.get("maintenance")
        assert maint is not None, "maintenance block must be present in GET /status"
        assert "last_graph_gc_at" in maint, (
            "maintenance.last_graph_gc_at key must be present in status. "
            f"maintenance sub-object keys: {list(maint.keys())}"
        )
        last_graph_gc_at = maint["last_graph_gc_at"]
        assert last_graph_gc_at is not None, (
            "maintenance.last_graph_gc_at must be non-null after a GC pass (S5). "
            f"Full maintenance block: {maint}"
        )
        # Must be ISO-8601 parseable.
        try:
            parsed_time = datetime.fromisoformat(last_graph_gc_at)
        except (ValueError, TypeError) as e:
            pytest.fail(
                f"maintenance.last_graph_gc_at must be a valid ISO-8601 timestamp. "
                f"Got {last_graph_gc_at!r} — parse error: {e}"
            )

        # Sanity: the timestamp is recent (not in the distant past).
        # Allow some clock skew but assert it's within the last 60 seconds (test execution time).
        now = datetime.now(parsed_time.tzinfo) if parsed_time.tzinfo else datetime.now()
        age_seconds = (now - parsed_time).total_seconds()
        assert age_seconds >= 0 and age_seconds < 60, (
            f"maintenance.last_graph_gc_at timestamp appears unreasonable: "
            f"{last_graph_gc_at!r} (age {age_seconds:.1f}s from now). "
            "It should be very recent (within seconds of GC execution)."
        )

        # Step 5: Verify caching — call GET /status again and assert same value.
        status_cached = client.get("/status", headers=_auth(api_key))
        assert status_cached.status_code == 200, (
            f"GET /status for caching check failed: {status_cached.status_code}"
        )
        status_cached_json = status_cached.json()
        graph_status_cached = status_cached_json.get("graph")
        assert graph_status_cached is not None, (
            "graph status must remain present in subsequent GET /status calls"
        )
        stale_count_cached = graph_status_cached.get("stale_mention_count")
        assert stale_count_cached == stale_count, (
            f"stale_mention_count must be cached (same value on repeated reads). "
            f"First read: {stale_count}, second read: {stale_count_cached}. "
            "The field is sourced from state file (cached), not live-scanned."
        )

        # Also verify last_graph_gc_at is cached (same value).
        maint_cached = status_cached_json.get("maintenance")
        last_graph_gc_at_cached = maint_cached.get("last_graph_gc_at") if maint_cached else None
        assert last_graph_gc_at_cached == last_graph_gc_at, (
            f"last_graph_gc_at must be cached (same value on repeated reads). "
            f"First read: {last_graph_gc_at!r}, second read: {last_graph_gc_at_cached!r}. "
            "The field is sourced from state file (cached), not live-scanned."
        )
