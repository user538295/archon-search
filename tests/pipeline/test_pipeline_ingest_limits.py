"""Pipeline-level ingest hardening (SecReview #3, #7d): secret files are never indexed, directory discovery runs off the
event loop and is bounded."""
from __future__ import annotations

import os
import threading
from pathlib import Path

import pytest

from archon_search._path_safety import IngestPathPolicy
from archon_search._types import PATH_DENIED_CODE, IngestTooManyFilesError

from .conftest import make_pipeline

_FILE_BODY = "# Note\n\nSome searchable text that is long enough to chunk.\n" * 3
_MAX_FILES = 3


def _pipeline(store, **overrides):  # type: ignore[no-untyped-def]
    pipeline = make_pipeline(store)
    for name, value in overrides.items():
        setattr(pipeline, name, value)
    return pipeline


def _write_files(directory: Path, names: list[str]) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    for name in names:
        (directory / name).write_text(_FILE_BODY, encoding="utf-8")


@pytest.mark.asyncio
async def test_directory_ingest_skips_credential_named_files(connected_store, col_name, tmp_path):
    _write_files(tmp_path, ["notes.md", "server.pem", "id_rsa", "deck.key"])
    (tmp_path / "tls.key").write_text("-----BEGIN PRIVATE KEY-----\nMIIE\n", encoding="utf-8")
    pipeline = make_pipeline(connected_store)

    results = await pipeline.ingest_directory(tmp_path, col_name, embedder=pipeline._global_embedder, rebuild_fts=False)

    assert [r.status for r in results] == ["ok", "ok"]
    documents, _, _ = await connected_store.list_documents(col_name)
    # ``deck.key`` is not a PEM (a Keynote deck uses the extension too): it is indexed; ``tls.key`` is a private key.
    assert sorted(Path(d.source_path).name for d in documents) == ["deck.key", "notes.md"]


@pytest.mark.asyncio
async def test_ingest_file_refuses_a_credential_file_with_a_fixed_code(connected_store, col_name, tmp_path):
    _write_files(tmp_path, ["server.pem"])
    pipeline = make_pipeline(connected_store)

    result = await pipeline.ingest_file(tmp_path / "server.pem", col_name, embedder=pipeline._global_embedder)

    assert result.status == "error"
    assert result.code == PATH_DENIED_CODE
    assert str(tmp_path) not in (result.error or "")
    assert result.chunks_created == 0


@pytest.mark.asyncio
async def test_ingest_file_refuses_a_file_under_a_denied_root(connected_store, col_name, tmp_path):
    state = tmp_path / "state"
    _write_files(state, ["notes.md"])
    pipeline = make_pipeline(connected_store)
    pipeline._path_policy = IngestPathPolicy(deny_roots=(state,))

    result = await pipeline.ingest_file(state / "notes.md", col_name, embedder=pipeline._global_embedder)

    assert result.code == PATH_DENIED_CODE


@pytest.mark.asyncio
async def test_a_collection_path_outside_allowed_roots_still_ingests(connected_store, col_name, tmp_path):
    """Collection paths come from the operator's own config: ``allowed_roots`` scopes client paths only."""
    _write_files(tmp_path / "collection", ["notes.md"])
    pipeline = make_pipeline(connected_store)
    pipeline._path_policy = IngestPathPolicy(allowed_roots=(tmp_path / "somewhere-else",))

    results = await pipeline.ingest_directory(
        tmp_path / "collection", col_name, embedder=pipeline._global_embedder, rebuild_fts=False
    )

    assert [r.status for r in results] == ["ok"]


@pytest.mark.asyncio
async def test_discovery_walks_the_tree_off_the_event_loop(connected_store, col_name, tmp_path, monkeypatch):
    _write_files(tmp_path, ["a.md"])
    loop_thread = threading.get_ident()
    walked_on: list[int] = []
    real_walk = os.walk

    def recording_walk(*args, **kwargs):  # type: ignore[no-untyped-def]
        walked_on.append(threading.get_ident())
        return real_walk(*args, **kwargs)

    monkeypatch.setattr(os, "walk", recording_walk)
    pipeline = make_pipeline(connected_store)

    await pipeline.ingest_directory(tmp_path, col_name, embedder=pipeline._global_embedder, rebuild_fts=False)

    assert walked_on, "discovery never walked the tree"
    assert loop_thread not in walked_on


@pytest.mark.asyncio
async def test_a_directory_over_the_discovery_limit_is_refused_before_anything_is_ingested(
    connected_store, col_name, tmp_path
):
    _write_files(tmp_path, [f"doc{i}.md" for i in range(_MAX_FILES + 1)])
    pipeline = _pipeline(connected_store, _max_discovered_files=_MAX_FILES)

    with pytest.raises(IngestTooManyFilesError) as raised:
        await pipeline.ingest_directory(tmp_path, col_name, embedder=pipeline._global_embedder, rebuild_fts=False)

    assert str(_MAX_FILES) in str(raised.value)
    assert "max_discovered_files" in str(raised.value)
    assert str(tmp_path) not in str(raised.value)
    assert col_name not in [info.name for info in await connected_store.list_collections()]


@pytest.mark.asyncio
async def test_a_directory_exactly_at_the_discovery_limit_ingests(connected_store, col_name, tmp_path):
    _write_files(tmp_path, [f"doc{i}.md" for i in range(_MAX_FILES)])
    pipeline = _pipeline(connected_store, _max_discovered_files=_MAX_FILES)

    results = await pipeline.ingest_directory(tmp_path, col_name, embedder=pipeline._global_embedder, rebuild_fts=False)

    assert len(results) == _MAX_FILES


@pytest.mark.asyncio
async def test_skipped_files_do_not_count_toward_the_discovery_limit(connected_store, col_name, tmp_path):
    _write_files(tmp_path, [f"doc{i}.md" for i in range(_MAX_FILES)] + [f".hidden{i}" for i in range(10)])
    pipeline = _pipeline(connected_store, _max_discovered_files=_MAX_FILES)

    results = await pipeline.ingest_directory(tmp_path, col_name, embedder=pipeline._global_embedder, rebuild_fts=False)

    assert len(results) == _MAX_FILES

