"""S340 — POST /explain must not fail the whole request over one unknown collection.

``Documentation/UserManual/60_searching.md`` states that collections which fail
(not found, metadata error) are "reported in the response ``excluded_collections[]``
rather than failing the whole request", and the complete ``/explain`` status list in
``Documentation/UserManual/80_explain_and_debugging.md`` (422 / 400 / 503 / 500 / 504)
does not include 404 for the fan-out path.

Actual behaviour: ``SearchPipeline.explain`` raised ``CollectionNotFoundError`` as soon
as ANY requested name was absent from the namespace metadata
(``archon_search/pipeline.py`` — ``if missing: raise CollectionNotFoundError(missing)``),
and ``routes_explain.py`` mapped that to ``404 {"detail": "collection not found"}``. One
unknown name therefore discarded the results of every valid collection in the fan-out.

Run with:
    uv run pytest tests/integration/test_s340_explain_unknown_collection_excluded.py -v --no-cov
"""
from __future__ import annotations

from pathlib import Path

import pytest

from tests.integration.conftest import ingest_file_via_path, make_real_app

pytestmark = pytest.mark.integration

_VALID_COLLECTION = "archon_s340_docs"
_UNKNOWN_COLLECTION = "s340_unknown_collection"


def test_explain_unknown_collection_is_excluded_not_fatal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One valid + one unknown collection → 200, unknown in excluded_collections with reason."""
    doc = tmp_path / "s340_corpus.md"
    doc.write_text(
        "# Router centroid pre-ranking\n\n"
        "This document belongs to the S340 regression collection.\n" * 6
    )

    with make_real_app(tmp_path, monkeypatch) as (client, _cfg, api_key):
        headers = {"Authorization": f"Bearer {api_key}"}
        ingest_file_via_path(client, _VALID_COLLECTION, str(doc), api_key=api_key)

        resp = client.post(
            "/explain",
            json={
                "query": "router centroid pre-ranking",
                "collections": [_VALID_COLLECTION, _UNKNOWN_COLLECTION],
                "rerank": True,
            },
            headers=headers,
        )

        assert resp.status_code == 200, (
            f"expected 200 with the unknown collection excluded, got "
            f"{resp.status_code}: {resp.text}"
        )
        data = resp.json()

        # Exact excluded_collections shape — reason must be "not_found".
        excluded = data["excluded_collections"]
        assert excluded == [{"name": _UNKNOWN_COLLECTION, "reason": "not_found"}], (
            f"excluded_collections mismatch: {excluded}"
        )

        # The valid leg must have produced results.
        assert len(data["results"]) > 0, (
            f"valid leg {_VALID_COLLECTION!r} produced no results: {data}"
        )

        # Every result must come from the valid collection (not the unknown one).
        assert all(r["collection"] == _VALID_COLLECTION for r in data["results"]), (
            f"unexpected collection in results: {[r['collection'] for r in data['results']]}"
        )


def test_explain_all_unknown_collections_returns_404(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When EVERY requested collection is absent, explain must return 404 (S340)."""
    with make_real_app(tmp_path, monkeypatch) as (client, _cfg, api_key):
        headers = {"Authorization": f"Bearer {api_key}"}
        resp = client.post(
            "/explain",
            json={
                "query": "no such collection",
                "collections": ["s340_ghost_a", "s340_ghost_b"],
            },
            headers=headers,
        )
        assert resp.status_code == 404, (
            f"expected 404 when every collection is absent, got "
            f"{resp.status_code}: {resp.text}"
        )
