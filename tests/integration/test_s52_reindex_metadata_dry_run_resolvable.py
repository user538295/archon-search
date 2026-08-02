"""S52 — reindex-metadata --dry-run must not make a collection transiently unresolvable.

A ``--dry-run`` reports counts and must write NOTHING to the collection-meta
table (``_archon_collection_meta``), so a preview must NOT open a window during
which ``POST /search`` for the collection returns 404 "collection not found".
(A dry-run does still write to the job store — the "writes nothing" guarantee
is scoped to the meta table, which is what the S52 404 window depends on.)

Root cause: even for ``dry_run=True``, ``reindex_metadata`` (routes_collections.py)
sets ``meta.metadata_reindex_job_id`` and awaits ``update_collection_meta`` SYNCHRONOUSLY
during the POST, before returning 202. ``update_collection_meta`` is a NON-ATOMIC
upsert on the shared ``_archon_collection_meta`` table: ``table.delete(...)`` then
``table.add(...)``. ``get_collection_meta`` does NOT hold the per-collection lock, so
a resolve landing between the delete and the add sees zero rows and returns None →
``POST /search`` 404s.

This test forces the interleaving deterministically by hooking ``AsyncTable.add``:
when armed and adding to the meta table, it runs ``get_collection_meta`` at the exact
moment after the delete and before the add, capturing what a concurrent resolve sees.

Run with:
    uv run pytest tests/integration/test_s52_reindex_metadata_dry_run_resolvable.py -v --no-cov
"""
from __future__ import annotations

import time
from pathlib import Path

import pytest
import lancedb.table
from fastapi.testclient import TestClient

# Import the real meta-table name from the store rather than hardcoding it, so a
# rename cannot silently disable the AsyncTable.add hook below and turn this
# regression test into a vacuous always-pass.
from archon_search.store import _META_TABLE

pytestmark = pytest.mark.integration

_NOT_OBSERVED = "NOT_OBSERVED"


def _poll_job_until_terminal(
    client: TestClient, job_id: str, api_key: str, timeout: float = 10.0
) -> dict:
    deadline = time.monotonic() + timeout
    terminal = {"DONE", "FAILED", "FAILED_EXPIRED", "CANCELLED"}
    while time.monotonic() < deadline:
        r = client.get(f"/jobs/{job_id}", headers={"Authorization": f"Bearer {api_key}"})
        assert r.status_code == 200, f"GET /jobs/{job_id} returned {r.status_code}: {r.text}"
        if r.json()["status"] in terminal:
            return r.json()
        time.sleep(0.05)
    pytest.fail(f"Job {job_id} did not reach a terminal status within {timeout}s")


@pytest.mark.integration
def test_reindex_metadata_dry_run_keeps_collection_resolvable(
    tmp_path: Path, monkeypatch
) -> None:
    """A dry-run reindex-metadata must never make the collection unresolvable.

    Primary proof: at the exact instant of the meta-table write triggered by the
    dry-run (delete-then-add window), a concurrent ``get_collection_meta`` must
    still resolve the collection (not None). A None capture proves the dry-run
    write opened a 404 window — the S52 bug.
    """
    from tests.integration.conftest import ingest_file_via_path, make_real_app

    collection_dir = tmp_path / "collection_dir"
    collection_dir.mkdir()
    (collection_dir / "sample.txt").write_text(
        "Hello world. This is a test document for reindex-metadata dry-run.",
        encoding="utf-8",
    )

    toml_content = f"[collections]\ncollections=[{str(collection_dir)!r}]\n"

    with make_real_app(tmp_path, monkeypatch, toml_content=toml_content) as (client, cfg, api_key):
        from archon_search.sync import path_to_collection_name

        headers = {"Authorization": f"Bearer {api_key}"}
        col = path_to_collection_name(str(collection_dir))
        ns = "default"

        # Ingest a real file so the collection + meta row exist.
        ingest_file_via_path(client, col, str(collection_dir / "sample.txt"), api_key=api_key)

        store = client.app.state.search_store

        # Cell captures what a concurrent resolve sees inside the delete->add window.
        cell = {"armed": False, "captured": _NOT_OBSERVED}

        orig_add = lancedb.table.AsyncTable.add

        async def patched_add(self, *args, **kwargs):
            # Only for the meta-table write during the armed dry-run, and only the
            # first such write (the route's synchronous pre-202 meta upsert).
            if (
                cell["armed"]
                and self.name == _META_TABLE
                and cell["captured"] is _NOT_OBSERVED
            ):
                # We are between table.delete(...) and table.add(...): resolve now.
                cell["captured"] = await store.get_collection_meta(col, namespace=ns)
            return await orig_add(self, *args, **kwargs)

        monkeypatch.setattr(lancedb.table.AsyncTable, "add", patched_add)

        # Arm, then trigger the dry-run reindex-metadata.
        cell["armed"] = True
        resp = client.post(
            f"/collections/{col}/reindex-metadata",
            json={"dry_run": True},
            headers=headers,
        )
        assert resp.status_code == 202, f"Expected 202, got {resp.status_code}: {resp.text}"
        job_id = resp.json()["job_id"]

        job = _poll_job_until_terminal(client, job_id, api_key)
        assert job["status"] == "DONE", f"Expected DONE, got {job['status']}: {job.get('error')}"
        cell["armed"] = False

        # PRIMARY PROOF: the dry-run must not touch the meta table at all, so the
        # armed hook must never have fired and ``captured`` must still be the exact
        # sentinel. This locks the "writes nothing to the meta table" contract:
        #   - no-fix code: the dry-run write hits the delete->add window, the hook
        #     fires and captures None -> ``None is _NOT_OBSERVED`` is False -> FAIL.
        #   - fixed code: no meta write happens -> sentinel untouched -> PASS.
        # Asserting the identity of the sentinel (not merely ``is not None``) rejects
        # a future regression that reintroduces the meta write but resolves in-window.
        captured = cell["captured"]
        assert captured is _NOT_OBSERVED, (
            "S52: dry-run reindex-metadata wrote to the meta table (the armed hook "
            f"fired and captured {captured!r} inside the delete->add window). A "
            "--dry-run preview must write nothing to the meta table — otherwise a "
            "concurrent get_collection_meta sees zero rows and POST /search 404s."
        )

        # Belt-and-suspenders end-to-end check: after DONE, search resolves (200).
        # Passes both before and after the fix (the window closes on DONE).
        search_resp = client.post(
            "/search",
            json={"collection": col, "query": "test document"},
            headers=headers,
        )
        assert search_resp.status_code == 200, (
            f"POST /search after dry-run returned {search_resp.status_code}: {search_resp.text}"
        )
