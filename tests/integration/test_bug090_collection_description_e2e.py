"""bug-090 e2e: collection responses surface the stored description.

Exercises all three read handlers (list, detail, patch) through the full HTTP
stack against a real ``SearchStore``, proving the fix reads ``meta.description``
from disk instead of returning the old hardcoded ``""``.

Run with:
    uv run pytest tests/integration/test_bug090_collection_description_e2e.py -v --no-cov
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from archon_search.sync import path_to_collection_name
from tests.integration.conftest import make_real_app

pytestmark = pytest.mark.integration

_DESCRIPTION = "A curated corpus about distributed systems and consensus."


def _write_meta_with_description(db_path: str, name: str, description: str) -> None:
    """Persist a meta row carrying ``description`` via a fresh store.

    Opens its own ``SearchStore`` on the same ``db_path`` inside a dedicated
    event loop (never touches the server's store object, whose locks are bound
    to the app's running loop). The server re-reads the meta table on the next
    request, so the write is visible cross-connection.
    """

    async def _run() -> None:
        from archon_search.collection_meta import CollectionMeta
        from archon_search.store import SearchStore

        store = SearchStore(db_path)
        await store.connect()
        try:
            await store.update_collection_meta(
                CollectionMeta(
                    name=name,
                    namespace="default",
                    active_embedding_model="BAAI/bge-small-en-v1.5",
                    description=description,
                )
            )
        finally:
            await store.disconnect()

    asyncio.run(_run())


def test_collection_description_surfaced_across_all_handlers(tmp_path, monkeypatch) -> None:
    """list, detail, and patch responses all return the stored description."""
    with make_real_app(tmp_path, monkeypatch) as (client, cfg, api_key):
        src = tmp_path / "corpus"
        src.mkdir()
        cfg.collections.append(str(src))
        name = path_to_collection_name(str(src))

        _write_meta_with_description(cfg.db_path, name, _DESCRIPTION)

        headers = {"Authorization": f"Bearer {api_key}"}

        # GET /collections/
        r = client.get("/collections/", headers=headers)
        assert r.status_code == 200
        entries = [e for e in r.json() if e["name"] == name]
        assert len(entries) == 1, f"expected collection {name!r} in listing, got {r.json()}"
        assert entries[0]["description"] == _DESCRIPTION

        # GET /collections/{name}
        r = client.get(f"/collections/{name}", headers=headers)
        assert r.status_code == 200
        assert r.json()["description"] == _DESCRIPTION

        # PATCH /collections/{name} (no-op body still rebuilds CollectionDetail)
        r = client.patch(f"/collections/{name}", json={}, headers=headers)
        assert r.status_code == 200
        assert r.json()["description"] == _DESCRIPTION
