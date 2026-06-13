"""Tests for ExportArchiveWriter and ImportArchiveReader (Task 1.3)."""
from __future__ import annotations

import io
import json
import struct
import tarfile
from pathlib import Path

import pytest

from archon_search.jobs.export_archive import (
    EXPORT_SCHEMA_VERSION,
    ExportArchiveWriter,
    ImportArchiveReader,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_doc(n: int) -> dict:
    """Return a minimal document dict."""
    vector_floats = [float(n), 0.0, 1.0]
    raw = struct.pack(f"{len(vector_floats)}f", *vector_floats)
    import base64
    b64 = base64.standard_b64encode(raw).decode()
    return {
        "doc_id": f"doc-{n}",
        "chunk_id": f"chunk-{n}",
        "text": f"Hello world {n}",
        "vector": b64,
        "source_path": f"/data/file{n}.txt",
        "indexed_at": "2024-01-01T00:00:00Z",
        "file_type": "text",
        "language": "en",
        "metadata": {},
        "acl": None,
        "custom_score": None,
        "ingested_by": "test",
        "updated_at": "2024-01-01T00:00:00Z",
    }


def _make_manifest(doc_count: int = 3) -> dict:
    return {
        "schema_version": EXPORT_SCHEMA_VERSION,
        "collection": "test-col",
        "exported_at": "2024-01-01T00:00:00Z",
        "doc_count": doc_count,
        "active_embedding_model": "BAAI/bge-small-en-v1.5",
        "description": "Test collection",
        "archon_search_version": "1.0.0",
    }


def _build_archive(tmp_path: Path, manifest: dict, docs: list[dict]) -> Path:
    """Build a valid .tar.gz archive in tmp_path; return its path."""
    archive_path = tmp_path / "test.tar.gz"
    tmp_file = tmp_path / "docs.jsonl.tmp"
    writer = ExportArchiveWriter(tmp_file)
    for doc in docs:
        writer.write_doc(doc)
    writer.finalize(manifest, archive_path)
    return archive_path


# ---------------------------------------------------------------------------
# ExportArchiveWriter tests
# ---------------------------------------------------------------------------

class TestExportArchiveWriter:
    def test_writer_creates_valid_tar(self, tmp_path: Path) -> None:
        """Write 3 docs, finalize, open resulting tar, confirm exactly manifest.json + documents.jsonl."""
        docs = [_make_doc(i) for i in range(3)]
        manifest = _make_manifest(doc_count=3)
        archive_path = _build_archive(tmp_path, manifest, docs)

        assert archive_path.exists()
        with tarfile.open(archive_path, "r:gz") as tf:
            names = {m.name for m in tf.getmembers()}
            assert names == {"manifest.json", "documents.jsonl"}
            # Verify line count in documents.jsonl
            member = tf.getmember("documents.jsonl")
            f = tf.extractfile(member)
            assert f is not None
            lines = [ln for ln in f.read().decode().splitlines() if ln.strip()]
            assert len(lines) == 3

    def test_writer_lines_written_counter(self, tmp_path: Path) -> None:
        """Counter increments correctly."""
        tmp_file = tmp_path / "docs.jsonl.tmp"
        writer = ExportArchiveWriter(tmp_file)
        assert writer.lines_written == 0
        writer.write_doc(_make_doc(0))
        assert writer.lines_written == 1
        writer.write_doc(_make_doc(1))
        assert writer.lines_written == 2
        # Cleanup without finalize
        writer.cleanup()

    def test_writer_cleanup_deletes_tmp(self, tmp_path: Path) -> None:
        """Tmp file deleted after finalize."""
        docs = [_make_doc(i) for i in range(2)]
        manifest = _make_manifest(doc_count=2)
        tmp_file = tmp_path / "docs.jsonl.tmp"
        archive_path = tmp_path / "out.tar.gz"
        writer = ExportArchiveWriter(tmp_file)
        for doc in docs:
            writer.write_doc(doc)
        writer.finalize(manifest, archive_path)
        assert not tmp_file.exists(), "Tmp file should be deleted after finalize"

    def test_writer_cleanup_on_exception(self, tmp_path: Path) -> None:
        """Cleanup deletes tmp file even when used as context manager that raises."""
        tmp_file = tmp_path / "docs.jsonl.tmp"
        archive_path = tmp_path / "bad.tar.gz"
        writer = ExportArchiveWriter(tmp_file)
        writer.write_doc(_make_doc(0))
        # Simulate exception during finalize by passing a bad archive path
        # The context manager's __exit__ calls cleanup() only on exception.
        class _BadWriter(ExportArchiveWriter):
            def finalize(self, manifest: dict, archive_path: Path) -> None:
                raise RuntimeError("simulated failure")

        bad_writer = _BadWriter(tmp_file)
        bad_writer.write_doc(_make_doc(1))
        try:
            with bad_writer:
                bad_writer.write_doc(_make_doc(2))
                raise RuntimeError("forced")
        except RuntimeError:
            pass
        assert not tmp_file.exists(), "Tmp file should be cleaned up on context manager exception"


# ---------------------------------------------------------------------------
# ImportArchiveReader tests
# ---------------------------------------------------------------------------

class TestImportArchiveReader:
    def test_reader_read_manifest_valid(self, tmp_path: Path) -> None:
        """Reads manifest from a valid tar correctly."""
        docs = [_make_doc(i) for i in range(2)]
        manifest = _make_manifest(doc_count=2)
        archive_path = _build_archive(tmp_path, manifest, docs)

        reader = ImportArchiveReader(archive_path)
        result = reader.read_manifest()
        assert result["schema_version"] == EXPORT_SCHEMA_VERSION
        assert result["collection"] == "test-col"
        assert result["doc_count"] == 2
        assert result["active_embedding_model"] == "BAAI/bge-small-en-v1.5"

    def test_reader_missing_manifest_key(self, tmp_path: Path) -> None:
        """Tar with manifest missing 'active_embedding_model' raises ValueError."""
        incomplete_manifest = {
            "schema_version": EXPORT_SCHEMA_VERSION,
            "collection": "test-col",
            "exported_at": "2024-01-01T00:00:00Z",
            "doc_count": 0,
            # "active_embedding_model" intentionally missing
        }
        # Build archive manually
        archive_path = tmp_path / "bad.tar.gz"
        tmp_file = tmp_path / "empty.jsonl.tmp"
        tmp_file.write_bytes(b"")
        with tarfile.open(archive_path, "w:gz") as tf:
            manifest_bytes = json.dumps(incomplete_manifest).encode()
            info = tarfile.TarInfo(name="manifest.json")
            info.size = len(manifest_bytes)
            tf.addfile(info, io.BytesIO(manifest_bytes))
            info2 = tarfile.TarInfo(name="documents.jsonl")
            info2.size = 0
            tf.addfile(info2, io.BytesIO(b""))

        reader = ImportArchiveReader(archive_path)
        with pytest.raises(ValueError, match="active_embedding_model"):
            reader.read_manifest()

    def test_reader_iter_docs_all(self, tmp_path: Path) -> None:
        """Iterates 5 docs in order."""
        docs = [_make_doc(i) for i in range(5)]
        manifest = _make_manifest(doc_count=5)
        archive_path = _build_archive(tmp_path, manifest, docs)

        reader = ImportArchiveReader(archive_path)
        result = list(reader.iter_docs())
        assert len(result) == 5
        for i, doc in enumerate(result):
            assert doc["doc_id"] == f"doc-{i}"

    def test_reader_iter_docs_skip(self, tmp_path: Path) -> None:
        """skip=3 yields only the last 2 of 5 docs."""
        docs = [_make_doc(i) for i in range(5)]
        manifest = _make_manifest(doc_count=5)
        archive_path = _build_archive(tmp_path, manifest, docs)

        reader = ImportArchiveReader(archive_path)
        result = list(reader.iter_docs(skip=3))
        assert len(result) == 2
        assert result[0]["doc_id"] == "doc-3"
        assert result[1]["doc_id"] == "doc-4"

    def test_reader_corrupt_line(self, tmp_path: Path) -> None:
        """Malformed JSON line raises ValueError mentioning line number."""
        # Build archive with a corrupt line
        archive_path = tmp_path / "corrupt.tar.gz"
        manifest = _make_manifest(doc_count=2)
        manifest_bytes = json.dumps(manifest).encode()
        # Good line, then corrupt line
        jsonl_content = (
            json.dumps(_make_doc(0)) + "\n"
            + "THIS IS NOT JSON\n"
        ).encode()
        with tarfile.open(archive_path, "w:gz") as tf:
            info = tarfile.TarInfo(name="manifest.json")
            info.size = len(manifest_bytes)
            tf.addfile(info, io.BytesIO(manifest_bytes))
            info2 = tarfile.TarInfo(name="documents.jsonl")
            info2.size = len(jsonl_content)
            tf.addfile(info2, io.BytesIO(jsonl_content))

        reader = ImportArchiveReader(archive_path)
        with pytest.raises(ValueError, match=r"[Cc]orrupt.*2|[Ll]ine.*2"):
            list(reader.iter_docs())

    def test_export_schema_version_is_int_1(self) -> None:
        """EXPORT_SCHEMA_VERSION is defined as int 1."""
        assert isinstance(EXPORT_SCHEMA_VERSION, int)
        assert EXPORT_SCHEMA_VERSION == 1
