"""S293: a two-collection ``POST /search`` fan-out must answer over content ingested
through the documented REST ingest contract.

Bug (``Documentation/Backlog/S293-fan_out_excludes_no_valid_collection.md``).
After ingesting ``alpha`` (volcanoes) and ``beta`` (submarines) over HTTP, the
fan-out ``POST /search {"collections": ["alpha", "beta"], ...}`` never returns the
documented ``200`` with ``excluded_collections == []`` and non-empty ``results``;
the reporter recorded it as ``POST /search fan-out returned 0: None`` /
``assert 0 == 200``. Steps 3 and 4 (the two mutually-exclusive-selector ``422``s)
behave as documented and are pinned here as well, so a regression in the selector
validation cannot hide behind the fan-out failure.

Root cause. ``POST /ingest`` accepts a ``documents`` payload — it is a field of
``IngestRequest`` (``archon_search/server/routes_jobs.py``) and part of the
published OpenAPI contract, which ``Documentation/Architecture/600_api_reference_or_public_interface.md``
describes as "``path: null`` documents-only ingest". A network client is the only
kind this server has for that mode: it cannot place files on the server host, so
the ``documents`` payload is its ingest path. ``_dispatch_ingest``
(``routes_jobs.py``, the ``elif body.documents is not None:`` branch) dispatches
that payload with ``if hasattr(pipeline, "ingest_documents")`` — and
``SearchPipeline`` implements no ``ingest_documents``, so the branch only logs
``"pipeline has no ingest_documents method; skipping documents ingest"`` at
WARNING and returns. The job still transitions to ``DONE`` and the client is told
the ingest succeeded, while nothing is chunked, embedded, or written: no chunk
rows, and no ``CollectionMeta`` row either. The fan-out then finds no metadata for
either requested name and ``SearchPipeline.search_many`` raises
``CollectionNotFoundError`` on ANY missing name (``archon_search/pipeline.py`` —
``if missing: raise CollectionNotFoundError(missing)``), which
``routes_search.py`` maps to ``404 {"detail": "collection not found"}``. Two
collections the client was told it had ingested therefore yield no valid
collection at all for the fan-out.

Fixed looks like: an ingest whose job reports ``DONE`` has actually ingested its
payload — the ``documents`` mode writes chunks and collection metadata the same
way the ``path`` mode does (or, failing that, the request is rejected outright
instead of reporting a successful no-op). The fan-out over the two collections
then returns ``200`` with ``excluded_collections == []`` and results carrying
each chunk's provenance.

The ``path``-based ingest leg was verified working end-to-end against a real
``archon-search serve`` process while this was investigated, so the failure sits
in the ``documents`` ingest leg, not in ``search_many``'s fan-out, merge, or
rerank.

Run with:
    uv run pytest tests/integration/test_s293_fanout_two_collections.py -v --no-cov
"""
from __future__ import annotations

import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from tests.integration.conftest import make_real_app

pytestmark = pytest.mark.integration

_ALPHA = "alpha"
_BETA = "beta"

_ALPHA_TEXT = (
    "Volcanoes erupt when magma rises through the crust. Lava flows out of the "
    "caldera and basalt shields build up over many eruptions."
)
_BETA_TEXT = (
    "Submarines dive by flooding their ballast tanks. Sonar and a periscope give "
    "the crew a picture of the surface above the pressure hull."
)

#: Matches ingested content in both collections, so a correct fan-out has
#: something to return from either leg.
_QUERY = "volcano lava eruption submarine sonar"

#: Every field the bug report requires on each fan-out result.
_DOCUMENTED_RESULT_FIELDS = (
    "doc_id",
    "chunk_id",
    "text",
    "score",
    "source_path",
    "file_type",
    "language",
    "collection",
)

_JOB_POLL_TIMEOUT_SECONDS = 30.0
_JOB_POLL_INTERVAL_SECONDS = 0.1


def _auth(api_key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {api_key}"}


def _ingest_documents(
    client: TestClient, collection: str, text: str, source_path: str, *, api_key: str
) -> None:
    """Ingest one inline document over ``POST /ingest`` and wait for the job to settle.

    Deliberately the ``documents`` payload rather than ``path``: an HTTP client
    cannot write files onto the server host, so this is the ingest mode the
    reported scenario uses. The job must report ``DONE`` — if it reported
    ``FAILED`` the client would know its content never landed.
    """
    headers = _auth(api_key)
    resp = client.post(
        "/ingest",
        json={"collection": collection, "documents": [{"text": text, "source_path": source_path}]},
        headers=headers,
    )
    assert resp.status_code == 202, f"ingest POST failed: {resp.status_code} {resp.text}"
    job_id = resp.json()["job_id"]

    deadline = time.monotonic() + _JOB_POLL_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        job = client.get(f"/jobs/{job_id}", headers=headers)
        assert job.status_code == 200, job.text
        status = job.json()["status"]
        if status == "DONE":
            return
        if status != "PENDING" and status != "RUNNING":
            pytest.fail(f"ingest job for {collection!r} ended {status}: {job.json()}")
        time.sleep(_JOB_POLL_INTERVAL_SECONDS)
    pytest.fail(f"ingest job for {collection!r} did not settle in {_JOB_POLL_TIMEOUT_SECONDS}s")


def test_s293_fanout_over_two_ingested_collections_returns_both(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Steps 1-2: ingest ``alpha`` and ``beta``, then fan out one search across both."""
    with make_real_app(tmp_path, monkeypatch) as (client, _cfg, api_key):
        _ingest_documents(client, _ALPHA, _ALPHA_TEXT, "/corpus/alpha/volcanoes.md", api_key=api_key)
        _ingest_documents(client, _BETA, _BETA_TEXT, "/corpus/beta/submarines.md", api_key=api_key)

        resp = client.post(
            "/search",
            json={"collections": [_ALPHA, _BETA], "query": _QUERY},
            headers=_auth(api_key),
        )

        assert resp.status_code == 200, (
            f"POST /search fan-out returned {resp.status_code}: {resp.text}. Both requested "
            f"collections were ingested over POST /ingest and both jobs reported DONE, so the "
            f"fan-out must answer over them."
        )
        data = resp.json()

        # Both requested collections exist, so the fan-out excludes neither.
        assert data["excluded_collections"] == [], (
            f"expected no excluded collections, got {data['excluded_collections']}"
        )

        results = data["results"]
        assert results, f"expected non-empty fan-out results, got {data}"

        for item in results:
            for field in _DOCUMENTED_RESULT_FIELDS:
                assert field in item, f"result missing documented field {field!r}: {item}"
            assert item["collection"] in {_ALPHA, _BETA}, (
                f"result carries a collection outside the requested fan-out: {item['collection']!r}"
            )


def test_s293_search_rejects_collection_and_collections_together(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Step 3: ``collection`` and ``collections`` are mutually exclusive → 422."""
    with make_real_app(tmp_path, monkeypatch) as (client, _cfg, api_key):
        resp = client.post(
            "/search",
            json={"collection": _ALPHA, "collections": [_ALPHA, _BETA], "query": _QUERY},
            headers=_auth(api_key),
        )
        assert resp.status_code == 422, (
            f"expected 422 when both collection and collections are supplied, got "
            f"{resp.status_code}: {resp.text}"
        )


def test_s293_search_requires_a_collection_selector(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Step 4: neither ``collection`` nor ``collections`` supplied → 422."""
    with make_real_app(tmp_path, monkeypatch) as (client, _cfg, api_key):
        resp = client.post("/search", json={"query": _QUERY}, headers=_auth(api_key))
        assert resp.status_code == 422, (
            f"expected 422 when no collection selector is supplied, got "
            f"{resp.status_code}: {resp.text}"
        )
