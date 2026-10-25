"""A foreign ingest is refused at the pipeline entry and the route, before any lock, parse or embed (SecReview #2).

Without the early check a namespace could make another namespace's writes see ``store_busy`` (the foreign request took
the owner's collection lock before it was refused) and made the server parse and embed a file for nothing.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import httpx
import pytest

from archon_search.config import SearchConfig
from archon_search.embedder import Embedder
from archon_search.jobs.store import JobStore
from archon_search.server.app import create_app
from archon_search.store import CollectionOwnershipError

from .conftest import MockEmbedderBackend, make_pipeline

_OWNER = "team-a"
_INTRUDER = "team-b"
_BODY = "# Note\n\nSome searchable text that is long enough to chunk.\n" * 3


class _SpyParser:
    def __init__(self) -> None:
        self.parsed: list[Path] = []

    async def parse(self, path: Path) -> str:
        self.parsed.append(path)
        return _BODY


class _CountingBackend(MockEmbedderBackend):
    def __init__(self) -> None:
        self.calls = 0

    def encode(self, texts: list[str]) -> list[list[float]]:
        self.calls += 1
        return super().encode(texts)


class _SpyEmbedder(Embedder):
    """A real ``Embedder`` over a backend that counts how often it embeds."""

    def __init__(self) -> None:
        self.counting_backend = _CountingBackend()
        super().__init__(self.counting_backend)

    @property
    def calls(self) -> int:
        return self.counting_backend.calls


@pytest.fixture
async def owned_collection(connected_store, col_name, tmp_path):  # type: ignore[no-untyped-def]
    """``col_name`` owned by ``_OWNER``: it ingested one file through a real pipeline."""
    owner_file = tmp_path / "owner.md"
    owner_file.write_text(_BODY, encoding="utf-8")
    pipeline = make_pipeline(connected_store)
    result = await pipeline.ingest_file(owner_file, col_name, embedder=pipeline._global_embedder, namespace=_OWNER)
    assert result.status == "ok"
    return col_name


def _spied_pipeline(store):  # type: ignore[no-untyped-def]
    pipeline = make_pipeline(store)
    parser = _SpyParser()
    pipeline._parser = parser
    return pipeline, parser


def _record_locks(store) -> list[str]:  # type: ignore[no-untyped-def]
    taken: list[str] = []
    original = store.lock_for

    def spy(name: str):  # type: ignore[no-untyped-def]
        taken.append(name)
        return original(name)

    store.lock_for = spy
    return taken


@pytest.mark.asyncio
async def test_foreign_ingest_file_is_refused_before_parse_embed_or_lock(connected_store, owned_collection, tmp_path):
    pipeline, parser = _spied_pipeline(connected_store)
    spy_embedder = _SpyEmbedder()
    locks = _record_locks(connected_store)
    intruder_file = tmp_path / "intruder.md"
    intruder_file.write_text(_BODY, encoding="utf-8")

    with pytest.raises(CollectionOwnershipError):
        await pipeline.ingest_file(intruder_file, owned_collection, embedder=spy_embedder, namespace=_INTRUDER)

    assert parser.parsed == []
    assert spy_embedder.calls == 0
    assert locks == []


@pytest.mark.asyncio
async def test_foreign_ingest_directory_is_refused_before_any_file_is_parsed(connected_store, owned_collection, tmp_path):
    pipeline, parser = _spied_pipeline(connected_store)
    locks = _record_locks(connected_store)
    directory = tmp_path / "docs"
    directory.mkdir()
    (directory / "a.md").write_text(_BODY, encoding="utf-8")

    with pytest.raises(CollectionOwnershipError):
        await pipeline.ingest_directory(
            directory, owned_collection, embedder=_SpyEmbedder(), namespace=_INTRUDER
        )

    assert parser.parsed == []
    assert locks == []


@pytest.mark.asyncio
async def test_foreign_inline_documents_are_refused_before_embedding(connected_store, owned_collection):
    pipeline, _ = _spied_pipeline(connected_store)
    spy_embedder = _SpyEmbedder()
    locks = _record_locks(connected_store)

    with pytest.raises(CollectionOwnershipError):
        await pipeline.ingest_documents(
            [{"text": _BODY, "source_path": "/virtual/doc.md"}],
            owned_collection,
            embedder=spy_embedder,
            namespace=_INTRUDER,
        )

    assert spy_embedder.calls == 0
    assert locks == []


@pytest.mark.asyncio
async def test_the_owner_still_ingests(connected_store, owned_collection, tmp_path):
    pipeline, parser = _spied_pipeline(connected_store)
    more = tmp_path / "more.md"
    more.write_text(_BODY, encoding="utf-8")

    result = await pipeline.ingest_file(more, owned_collection, embedder=pipeline._global_embedder, namespace=_OWNER)

    assert result.status == "ok"
    assert parser.parsed == [more]


@pytest.mark.asyncio
async def test_a_directory_ingest_checks_ownership_once_not_per_file(connected_store, owned_collection, tmp_path):
    pipeline, _ = _spied_pipeline(connected_store)
    checks: list[str] = []
    original = connected_store.assert_collection_access

    async def counting(collection: str, namespace: str) -> None:
        checks.append(namespace)
        await original(collection, namespace)

    connected_store.assert_collection_access = counting
    directory = tmp_path / "many"
    directory.mkdir()
    for index in range(3):
        (directory / f"{index}.md").write_text(_BODY, encoding="utf-8")

    await pipeline.ingest_directory(
        directory, owned_collection, embedder=pipeline._global_embedder, namespace=_OWNER, rebuild_fts=False
    )

    assert checks == [_OWNER]


# --- POST /ingest takes no lock for a foreign collection ---------------------------------------------------


class _RouteStore:
    """A search store for the ingest route: ``_OWNER`` owns every collection; it records the locks taken."""

    def __init__(self) -> None:
        self.locks_taken: list[str] = []

    def lock_for(self, name: str):  # type: ignore[no-untyped-def]
        self.locks_taken.append(name)
        return asyncio.Lock()

    async def assert_collection_access(self, collection: str, namespace: str) -> None:
        if namespace != _OWNER:
            raise CollectionOwnershipError(collection, namespace)


_INTRUDER_KEY = "b" * 64
_OWNER_KEY = "a" * 64
_ACCEPTED = 202


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("key", "expected_locks"), [(_INTRUDER_KEY, 0), (_OWNER_KEY, 1)], ids=["foreign-takes-no-lock", "owner-takes-it"]
)
async def test_post_ingest_takes_the_collection_lock_only_for_a_collection_the_caller_may_write(
    tmp_path: Path, key: str, expected_locks: int
) -> None:
    config = SearchConfig()
    config.db_path = str(tmp_path / "search")
    config.namespaces = {_OWNER_KEY: _OWNER, _INTRUDER_KEY: _INTRUDER}
    app = create_app(config, JobStore(path=tmp_path / "jobs.json"))
    store = _RouteStore()
    app.state.search_store = store
    body = {"collection": "shared", "documents": [{"text": "hello", "source_path": "/virtual/a.md"}]}

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as client:
        response = await client.post("/ingest", json=body, headers={"Authorization": f"Bearer {key}"})
        while app.state._background_tasks:
            await asyncio.gather(*list(app.state._background_tasks))

    assert response.status_code == _ACCEPTED
    assert len(store.locks_taken) == expected_locks
