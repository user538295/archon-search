"""Maintenance orphan cleanup must not delete a collection populated by ``import``.

Regression: ``MaintenanceLoop._run_orphan_cleanup`` (``archon_search/jobs/maintenance_loop.py``) deleted
every document whose ``source_path`` was a local path absent on this host. Its only exemption for chunks
with no backing file was the S293 inline marker. ``import`` copied each archived chunk's ``source_path``
verbatim, so a disk-ingested collection exported on host A and imported on host B carried host A's paths;
the first maintenance pass on host B silently deleted the whole imported collection. ``import`` now stamps
``_imported`` = ``"true"`` (``IMPORTED_CHUNK_METADATA_KEY``) on every chunk and the sweep spares the
imported documents.

Every test runs two real apps on separate data dirs ("host A", "host B") and runs the sweep through
``POST /maintenance/trigger``, polling ``GET /status`` (``trigger_maintenance_and_wait_for_orphans``).

Run with:
    uv run pytest tests/integration/test_import_orphan_cleanup_integration.py --no-cov -n 2
"""
from __future__ import annotations

import hashlib
import io
import json
import shutil
import tarfile
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from archon_search.constants import (
    DEFAULT_NAMESPACE,
    IMPORTED_CHUNK_METADATA_KEY,
    IMPORTED_CHUNK_METADATA_VALUE,
)
from archon_search.store import SearchStore
from tests.integration.conftest import (
    REAL_APP_DATA_SUBDIR,
    ingest_file_via_path,
    make_real_app,
    trigger_maintenance_and_wait_for_orphans,
    wait_for_job_done,
)

pytestmark = pytest.mark.integration

_COLLECTION = "imported_corpus"
#: Repeats of a document's sentence: long enough text to split into several chunks.
_SENTENCE_REPEATS = 30
_DOC_TEXT = (
    "Imported collection orphan cleanup regression document. " * _SENTENCE_REPEATS
    + "Its chunks must survive maintenance on the host it was imported into."
)
_CONTROL_TEXT = (
    "Disk-ingested control document of the imported collection. " * _SENTENCE_REPEATS
    + "Deleting its file must remove it in the same maintenance pass."
)


#: Where host A mounted the corpus: its resolve() maps the archived path under it, so host A's doc_ids differ from
#: the ones host B derives from the same path (e.g. host A reached the path through a symlinked mount).
_HOST_A_MOUNT = "/mnt/host-a"


def _auth(api_key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {api_key}"}


async def _collect_rows(search_store: SearchStore, collection: str) -> list[dict[str, Any]]:
    return [dict(row) async for row in search_store.list_chunks_raw(collection, DEFAULT_NAMESPACE)]


def _list_collection_rows(client: TestClient) -> list[dict[str, Any]]:
    """Every stored row of the collection, read on the app's own event loop."""
    return client.portal.call(_collect_rows, client.app.state.search_store, _COLLECTION)


def _is_imported(row: dict[str, Any]) -> bool:
    return json.loads(row["metadata"] or "{}").get(IMPORTED_CHUNK_METADATA_KEY) == IMPORTED_CHUNK_METADATA_VALUE


def _chunk_ids(rows: list[dict[str, Any]]) -> list[str]:
    return sorted(row["chunk_id"] for row in rows)


def _export_from_host_a(host_a_dir: Path, source_file: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Disk-ingest *source_file* on host A, export the collection, and return the archive's path."""
    with make_real_app(host_a_dir, monkeypatch) as (client, _cfg, api_key):
        ingest_file_via_path(client, _COLLECTION, str(source_file), api_key=api_key)
        export = client.post(f"/collections/{_COLLECTION}/export", json={}, headers=_auth(api_key))
        assert export.status_code == 202, f"{export.status_code}: {export.text}"
        return Path(wait_for_job_done(client, export.json()["job_id"], what="export").result["archive_path"])


def _copy_archive_to(host_dir: Path, archive: Path) -> Path:
    exports = host_dir / REAL_APP_DATA_SUBDIR / "exports" / DEFAULT_NAMESPACE
    exports.mkdir(parents=True)
    copied = exports / archive.name
    shutil.copy(archive, copied)
    return copied


def _local_doc_id(path: Path) -> str:
    """Independent oracle for the doc_id this host derives from *path*: sha256 of the resolved path's string."""
    return hashlib.sha256(str(path.resolve()).encode()).hexdigest()


def _rewrite_doc_ids(archive: Path, doc_id: str) -> None:
    """Rewrite *archive* in place so every doc carries *doc_id*, as host A would have derived it.

    Each chunk_id is prefixed with its doc_id, so it is rewritten too; the manifest is kept verbatim.
    """
    with tarfile.open(archive, "r:gz") as tf:
        members = {name: tf.extractfile(name).read() for name in ("manifest.json", "documents.jsonl")}
    docs = [json.loads(line) for line in members["documents.jsonl"].decode().splitlines() if line]
    for doc in docs:
        doc["chunk_id"] = doc["chunk_id"].replace(doc["doc_id"], doc_id, 1)
        doc["doc_id"] = doc_id
    members["documents.jsonl"] = "".join(json.dumps(doc) + "\n" for doc in docs).encode()
    with tarfile.open(archive, "w:gz") as tf:
        for name, data in members.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))


def _import(client: TestClient, api_key: str, archive: Path) -> list[dict[str, Any]]:
    """Import *archive* into the collection; return the stored rows, each asserted to carry the imported marker."""
    response = client.post(f"/collections/{_COLLECTION}/import", json={"path": str(archive)}, headers=_auth(api_key))
    assert response.status_code == 202, f"{response.status_code}: {response.text}"
    result = wait_for_job_done(client, response.json()["job_id"], what="import").result
    rows = _list_collection_rows(client)
    # Presence anchor: the import really stored host A's disk-ingested chunks.
    assert rows, f"import stored no chunks: {result}"
    assert len(rows) == result["imported"], (len(rows), result)
    assert all(_is_imported(row) for row in rows), [row["metadata"] for row in rows]
    return rows


def _sweep_with_deleted_control_file(client: TestClient, api_key: str, corpus: Path) -> tuple[int, list[dict[str, Any]]]:
    """Ingest a control file into the collection, delete it, and sweep; return orphans removed and surviving rows.

    The control file is a same-run orphan, so the pass provably ran and swept this collection.
    """
    control_file = corpus / "control.txt"
    control_file.write_text(_CONTROL_TEXT, encoding="utf-8")
    ingest_file_via_path(client, _COLLECTION, str(control_file), api_key=api_key)
    control_file.unlink()

    entry, _maintenance = trigger_maintenance_and_wait_for_orphans(client, _COLLECTION, api_key=api_key)
    assert entry.get("last_error") is None, entry
    return entry["orphans_removed_last_run"], _list_collection_rows(client)


def test_orphan_cleanup_keeps_chunks_of_collection_imported_from_another_host(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Host A's doc_ids are not host B's derivation: the imported chunks survive, deleted host-B files do not.

    Host B disk-ingests its own file at the archived path, then deletes it. The path then holds the imported
    (marked) rows under host A's doc_id and host B's (unmarked) rows under the local one; the sweep deletes
    only the local document, whose doc_id carries no marked chunk.
    """
    host_a_dir, host_b_dir = tmp_path / "host_a", tmp_path / "host_b"
    host_a_corpus, host_b_corpus = tmp_path / "host_a_corpus", tmp_path / "host_b_corpus"
    for directory in (host_a_dir, host_b_dir, host_a_corpus, host_b_corpus):
        directory.mkdir()
    source_file = host_a_corpus / "notes.txt"
    source_file.write_text(_DOC_TEXT, encoding="utf-8")

    host_b_archive = _copy_archive_to(host_b_dir, _export_from_host_a(host_a_dir, source_file, monkeypatch))
    host_a_doc_id = hashlib.sha256(f"{_HOST_A_MOUNT}{source_file}".encode()).hexdigest()
    assert host_a_doc_id != _local_doc_id(source_file), "the scenario needs a doc_id host B does not derive"
    _rewrite_doc_ids(host_b_archive, host_a_doc_id)
    source_file.unlink()  # host A's file does not exist on host B

    with make_real_app(host_b_dir, monkeypatch) as (client, cfg, api_key):
        assert cfg.maintenance.orphan_cleanup, "the scenario needs the default orphan_cleanup=True"
        imported_rows = _import(client, api_key, host_b_archive)
        assert {row["doc_id"] for row in imported_rows} == {host_a_doc_id}, imported_rows
        source_file.write_text(_CONTROL_TEXT, encoding="utf-8")
        ingest_file_via_path(client, _COLLECTION, str(source_file), api_key=api_key)
        local_doc_ids = {row["doc_id"] for row in _list_collection_rows(client)}
        assert local_doc_ids == {host_a_doc_id, _local_doc_id(source_file)}, local_doc_ids
        source_file.unlink()
        orphans_removed, surviving_rows = _sweep_with_deleted_control_file(client, api_key, host_b_corpus)

    assert _chunk_ids(surviving_rows) == _chunk_ids(imported_rows), (
        "the pass must delete both host-B documents and keep every imported chunk"
    )
    assert orphans_removed == 2, f"the control file and host B's document at the archived path, got {orphans_removed}"


def test_orphan_cleanup_keeps_same_host_restore_whose_file_is_deleted_without_reingest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Known tradeoff: a restore of this host's own export stays imported, so deleting its file sweeps nothing.

    The archived path is a real local path here and derives the archived doc_id, but without a re-ingest
    every row still carries the imported marker, and the sweep spares a marked document.
    """
    host_a_dir, host_b_dir, corpus = tmp_path / "host_a", tmp_path / "host_b", tmp_path / "corpus"
    for directory in (host_a_dir, host_b_dir, corpus):
        directory.mkdir()
    source_file = corpus / "notes.txt"
    source_file.write_text(_DOC_TEXT, encoding="utf-8")

    host_b_archive = _copy_archive_to(host_b_dir, _export_from_host_a(host_a_dir, source_file, monkeypatch))

    with make_real_app(host_b_dir, monkeypatch) as (client, _cfg, api_key):
        imported_rows = _import(client, api_key, host_b_archive)
        source_file.unlink()
        orphans_removed, surviving_rows = _sweep_with_deleted_control_file(client, api_key, corpus)

    assert _chunk_ids(surviving_rows) == _chunk_ids(imported_rows), (
        "the restored rows outlive their deleted file until the path is re-ingested"
    )
    assert orphans_removed == 1, f"only the deleted control file is an orphan, got {orphans_removed}"


def test_reingest_from_disk_drops_imported_marker_so_cleanup_removes_deleted_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Same-host restore: re-ingesting the archived path replaces the marked rows, so its later deletion sweeps them."""
    host_a_dir, host_b_dir, corpus = tmp_path / "host_a", tmp_path / "host_b", tmp_path / "corpus"
    for directory in (host_a_dir, host_b_dir, corpus):
        directory.mkdir()
    source_file = corpus / "notes.txt"
    source_file.write_text(_DOC_TEXT, encoding="utf-8")

    host_b_archive = _copy_archive_to(host_b_dir, _export_from_host_a(host_a_dir, source_file, monkeypatch))

    with make_real_app(host_b_dir, monkeypatch) as (client, _cfg, api_key):
        imported_rows = _import(client, api_key, host_b_archive)
        ingest_file_via_path(client, _COLLECTION, str(source_file), api_key=api_key)
        reingested_rows = _list_collection_rows(client)

        source_file.unlink()
        entry, _maintenance = trigger_maintenance_and_wait_for_orphans(client, _COLLECTION, api_key=api_key)
        surviving_rows = _list_collection_rows(client)

    assert entry.get("last_error") is None, entry
    assert {row["doc_id"] for row in reingested_rows} == {row["doc_id"] for row in imported_rows}, (
        "the archived path resolves to the archived doc_id here, so re-ingest replaces the same document"
    )
    assert reingested_rows, "re-ingest stored no chunks"
    assert not any(_is_imported(row) for row in reingested_rows), [row["metadata"] for row in reingested_rows]
    assert entry["orphans_removed_last_run"] == 1, entry
    assert surviving_rows == [], f"cleanup left {len(surviving_rows)} rows of the deleted file"
