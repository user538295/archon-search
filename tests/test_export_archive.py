"""Tests for ExportArchiveWriter and ImportArchiveReader (Task 1.3)."""
from __future__ import annotations

import io
import json
import struct
import tarfile
from pathlib import Path

import pytest

from archon_search._path_safety import PathUnsafeError
from archon_search.jobs.export_archive import (
    ARCHIVE_VALUE_MAX_CHARS,
    EXPORT_SCHEMA_VERSION,
    INVALID_MANIFEST_DETAIL,
    ArchiveRejectedError,
    ExportArchiveWriter,
    ImportArchiveReader,
    manifest_error_detail,
    quote_archive_value,
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
        "namespace": "default",
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


def _archive_with_members(tmp_path: Path, *, manifest: bytes, documents: bytes) -> Path:
    """Build a .tar.gz holding the raw *manifest* / *documents* bytes as its two members."""
    archive_path = tmp_path / "raw.tar.gz"
    with tarfile.open(archive_path, "w:gz") as tf:
        for name, payload in (("manifest.json", manifest), ("documents.jsonl", documents)):
            info = tarfile.TarInfo(name=name)
            info.size = len(payload)
            tf.addfile(info, io.BytesIO(payload))
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
        """An exception inside the context manager closes and deletes the tmp file."""
        tmp_file = tmp_path / "docs.jsonl.tmp"
        writer = ExportArchiveWriter(tmp_file)
        with pytest.raises(RuntimeError, match="forced"), writer:
            writer.write_doc(_make_doc(1))
            raise RuntimeError("forced")
        assert not tmp_file.exists(), "Tmp file should be cleaned up on context manager exception"
        with pytest.raises(RuntimeError, match="closed"):
            writer.write_doc(_make_doc(2))


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
            "namespace": "default",
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
        with pytest.raises(ArchiveRejectedError, match="active_embedding_model"):
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
        with pytest.raises(ArchiveRejectedError) as excinfo:
            list(reader.iter_docs())
        # Fixed text plus our own line number — never the JSON parser's message.
        assert str(excinfo.value) == "Corrupt line 2: not valid UTF-8 JSON"

    def test_reader_invalid_utf8_line_fails_as_corrupt_line(self, tmp_path: Path) -> None:
        """A documents.jsonl line that is not UTF-8 is a corrupt line, not an unexpected crash."""
        archive_path = _archive_with_members(
            tmp_path,
            manifest=json.dumps(_make_manifest(doc_count=2)).encode(),
            documents=(json.dumps(_make_doc(0)) + "\n").encode() + b"\xff\xfe not utf-8\n",
        )

        reader = ImportArchiveReader(archive_path)
        with pytest.raises(ArchiveRejectedError) as excinfo:
            list(reader.iter_docs())
        assert str(excinfo.value) == "Corrupt line 2: not valid UTF-8 JSON"

    def test_reader_invalid_utf8_line_is_skipped_when_skipping(self, tmp_path: Path) -> None:
        """on_error='skip' covers a non-UTF-8 line exactly like a non-JSON one."""
        archive_path = _archive_with_members(
            tmp_path,
            manifest=json.dumps(_make_manifest(doc_count=3)).encode(),
            documents=(
                (json.dumps(_make_doc(0)) + "\n").encode()
                + b"\xff\xfe not utf-8\n"
                + (json.dumps(_make_doc(2)) + "\n").encode()
            ),
        )

        reader = ImportArchiveReader(archive_path)
        docs = list(reader.iter_docs(on_error="skip"))
        assert [doc["doc_id"] for doc in docs] == ["doc-0", "doc-2"]
        assert reader.skipped_lines == 1

    def test_reader_tracks_line_number_of_last_yielded_doc(self, tmp_path: Path) -> None:
        """``line_number`` lets a caller name the archive line of a doc it rejects."""
        docs = [_make_doc(i) for i in range(3)]
        archive_path = _build_archive(tmp_path, _make_manifest(doc_count=3), docs)

        reader = ImportArchiveReader(archive_path)
        seen = [reader.line_number for _doc in reader.iter_docs(skip=1)]
        assert seen == [2, 3]

    @pytest.mark.parametrize("manifest_bytes", [b"{not json", b"\xff\xfe\x00"])
    def test_reader_undecodable_manifest_is_rejected(self, tmp_path: Path, manifest_bytes: bytes) -> None:
        """Non-JSON or non-UTF-8 manifest bytes raise the documented error with fixed text."""
        archive_path = _archive_with_members(tmp_path, manifest=manifest_bytes, documents=b"")

        with pytest.raises(ArchiveRejectedError) as excinfo:
            ImportArchiveReader(archive_path).read_manifest()
        assert str(excinfo.value) == "manifest.json is not valid UTF-8 JSON"

    def test_reader_non_object_manifest_names_the_json_type(self, tmp_path: Path) -> None:
        archive_path = _archive_with_members(tmp_path, manifest=b"[1, 2]", documents=b"")

        with pytest.raises(ArchiveRejectedError) as excinfo:
            ImportArchiveReader(archive_path).read_manifest()
        assert str(excinfo.value) == "manifest.json must be a JSON object, got list"

    def test_export_schema_version_is_int_1(self) -> None:
        """EXPORT_SCHEMA_VERSION is defined as int 1."""
        assert isinstance(EXPORT_SCHEMA_VERSION, int)
        assert EXPORT_SCHEMA_VERSION == 1


# ---------------------------------------------------------------------------
# quote_archive_value
# ---------------------------------------------------------------------------


def test_quote_archive_value_keeps_short_values_as_repr() -> None:
    assert quote_archive_value("BAAI/bge-small-en-v1.5") == "'BAAI/bge-small-en-v1.5'"
    assert quote_archive_value(2) == "2"


def test_quote_archive_value_truncates_long_values() -> None:
    """An archive is untrusted input: a crafted value cannot inflate an error message."""
    quoted = quote_archive_value("x" * (ARCHIVE_VALUE_MAX_CHARS * 10))
    assert len(quoted) == ARCHIVE_VALUE_MAX_CHARS
    assert quoted.startswith("'xxx")
    assert quoted.endswith("…")


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("x" * (ARCHIVE_VALUE_MAX_CHARS - 2), "'" + "x" * (ARCHIVE_VALUE_MAX_CHARS - 2) + "'"),
        ("x" * (ARCHIVE_VALUE_MAX_CHARS - 1), "'" + "x" * (ARCHIVE_VALUE_MAX_CHARS - 2) + "…"),
    ],
    ids=["repr-at-limit-kept", "repr-one-over-limit-truncated"],
)
def test_quote_archive_value_limit_boundary(value: str, expected: str) -> None:
    """A repr of exactly ARCHIVE_VALUE_MAX_CHARS is kept whole; one character more is cut."""
    assert len(expected) == ARCHIVE_VALUE_MAX_CHARS
    assert quote_archive_value(value) == expected


@pytest.mark.parametrize(
    ("value", "expected"),
    [(None, "None"), ([1, "a"], "[1, 'a']"), ({"k": [2]}, "{'k': [2]}")],
    ids=["none", "list", "dict"],
)
def test_quote_archive_value_quotes_non_string_values(value: object, expected: str) -> None:
    """A manifest field can hold any JSON type; each is quoted by its repr."""
    assert quote_archive_value(value) == expected


def test_quote_archive_value_escapes_control_and_bidi_characters() -> None:
    """A newline, an ANSI escape or a right-to-left override cannot forge log or terminal output."""
    assert quote_archive_value("a\nb\x1b[31mc\u202ed") == "'a\\nb\\x1b[31mc\\u202ed'"


# ---------------------------------------------------------------------------
# Unusable documents.jsonl lines beyond JSON syntax errors
# ---------------------------------------------------------------------------

_DEEPLY_NESTED_JSON = b"[" * 100_000
_OVERSIZED_INT_JSON = b'{"doc_id": ' + b"1" * 5_000 + b"}"


def _docs_with_bad_second_line(bad_line: bytes) -> bytes:
    return (
        (json.dumps(_make_doc(0)) + "\n").encode()
        + bad_line
        + b"\n"
        + (json.dumps(_make_doc(2)) + "\n").encode()
    )


def _archive_with_bad_second_line(tmp_path: Path, bad_line: bytes) -> Path:
    return _archive_with_members(
        tmp_path,
        manifest=json.dumps(_make_manifest(doc_count=3)).encode(),
        documents=_docs_with_bad_second_line(bad_line),
    )


@pytest.mark.parametrize(
    ("bad_line", "reason"),
    [
        (_DEEPLY_NESTED_JSON, "not valid UTF-8 JSON"),
        (_OVERSIZED_INT_JSON, "not valid UTF-8 JSON"),
        (b"[1, 2]", "not a JSON object"),
        (b"42", "not a JSON object"),
    ],
    ids=["deep-nesting", "oversized-int", "array", "number"],
)
class TestUnusableDocumentLine:
    def test_fails_as_corrupt_line(self, tmp_path: Path, bad_line: bytes, reason: str) -> None:
        archive_path = _archive_with_bad_second_line(tmp_path, bad_line)

        with pytest.raises(ArchiveRejectedError) as excinfo:
            list(ImportArchiveReader(archive_path).iter_docs())
        assert str(excinfo.value) == f"Corrupt line 2: {reason}"

    def test_is_skipped_when_skipping(self, tmp_path: Path, bad_line: bytes, reason: str) -> None:
        reader = ImportArchiveReader(_archive_with_bad_second_line(tmp_path, bad_line))

        docs = list(reader.iter_docs(on_error="skip"))
        assert [doc["doc_id"] for doc in docs] == ["doc-0", "doc-2"]
        assert reader.skipped_lines == 1


_READER_LOGGER = "archon_search.jobs.export_archive"


def test_reader_logs_a_corrupt_line_only_when_skipping_it(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """In fail mode the caller logs the refusal with its job context; the reader adds no second WARNING."""
    archive_path = _archive_with_bad_second_line(tmp_path, b"NOT_JSON")

    with caplog.at_level("WARNING", logger=_READER_LOGGER):
        list(ImportArchiveReader(archive_path).iter_docs(on_error="skip"))
    skip_warnings = [r.getMessage() for r in caplog.records if r.name == _READER_LOGGER]
    assert len(skip_warnings) == 1
    assert skip_warnings[0].startswith("Corrupt line 2: ")

    caplog.clear()
    with caplog.at_level("WARNING", logger=_READER_LOGGER), pytest.raises(ArchiveRejectedError):
        list(ImportArchiveReader(archive_path).iter_docs(on_error="fail"))
    assert [r.getMessage() for r in caplog.records if r.name == _READER_LOGGER] == []


# ---------------------------------------------------------------------------
# Manifest refusals beyond JSON syntax errors, and non-regular members
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "manifest_bytes", [_DEEPLY_NESTED_JSON, _OVERSIZED_INT_JSON], ids=["deep-nesting", "oversized-int"]
)
def test_reader_unparseable_manifest_is_rejected(tmp_path: Path, manifest_bytes: bytes) -> None:
    """Deep nesting (RecursionError) and an oversized int (ValueError) are refusals, not crashes."""
    archive_path = _archive_with_members(tmp_path, manifest=manifest_bytes, documents=b"")

    with pytest.raises(ArchiveRejectedError) as excinfo:
        ImportArchiveReader(archive_path).read_manifest()
    assert str(excinfo.value) == "manifest.json is not valid UTF-8 JSON"


def _archive_with_directory_member(tmp_path: Path, directory_member: str) -> Path:
    """A .tar.gz whose *directory_member* is a directory entry; the other member is valid."""
    archive_path = tmp_path / "dir-member.tar.gz"
    payloads = {
        "manifest.json": json.dumps(_make_manifest(doc_count=0)).encode(),
        "documents.jsonl": b"",
    }
    with tarfile.open(archive_path, "w:gz") as tf:
        for name, payload in payloads.items():
            info = tarfile.TarInfo(name=name)
            if name == directory_member:
                info.type = tarfile.DIRTYPE
                tf.addfile(info)
            else:
                info.size = len(payload)
                tf.addfile(info, io.BytesIO(payload))
    return archive_path


def test_reader_refuses_a_manifest_that_is_not_a_regular_file(tmp_path: Path) -> None:
    archive_path = _archive_with_directory_member(tmp_path, "manifest.json")

    with pytest.raises(ArchiveRejectedError) as excinfo:
        ImportArchiveReader(archive_path).read_manifest()
    assert str(excinfo.value) == "manifest.json member is not a regular file"


def test_reader_refuses_documents_that_are_not_a_regular_file(tmp_path: Path) -> None:
    archive_path = _archive_with_directory_member(tmp_path, "documents.jsonl")

    with pytest.raises(ArchiveRejectedError) as excinfo:
        list(ImportArchiveReader(archive_path).iter_docs())
    assert str(excinfo.value) == "documents.jsonl member is not a regular file"


# ---------------------------------------------------------------------------
# manifest_error_detail
# ---------------------------------------------------------------------------

_ARCHIVE = Path("/archives/a.tar.gz")
_INTERNAL_CAUSE = "internal: /var/lib/secret/state.db is locked"


@pytest.mark.parametrize(
    ("exc", "expected"),
    [
        (ArchiveRejectedError("manifest.json is not valid UTF-8 JSON"), "manifest.json is not valid UTF-8 JSON"),
        (PathUnsafeError("unsafe_tar_member"), "unsafe_tar_member"),
        (ValueError(_INTERNAL_CAUSE), INVALID_MANIFEST_DETAIL),
    ],
    ids=["archive-rejected", "path-unsafe", "other-value-error"],
)
def test_manifest_error_detail_sends_only_text_archon_search_built(exc: Exception, expected: str) -> None:
    assert manifest_error_detail(exc, _ARCHIVE) == expected


def test_manifest_error_detail_logs_the_cause_it_withholds(caplog: pytest.LogCaptureFixture) -> None:
    """The cause the wire never sees is logged, with its traceback, for diagnosis."""
    cause = ValueError(_INTERNAL_CAUSE)

    with caplog.at_level("WARNING", logger=_READER_LOGGER):
        manifest_error_detail(cause, _ARCHIVE)

    [record] = [r for r in caplog.records if r.name == _READER_LOGGER]
    assert str(_ARCHIVE) in record.getMessage()
    assert _INTERNAL_CAUSE in record.getMessage()
    assert record.exc_info is not None
    assert record.exc_info[1] is cause


def test_open_private_closes_the_fd_when_restricting_it_fails(tmp_path, monkeypatch):
    import os

    from archon_search.jobs import export_archive

    opened: list[int] = []
    real_open = os.open

    def tracking_open(*args, **kwargs):
        fd = real_open(*args, **kwargs)
        opened.append(fd)
        return fd

    def failing_restrict(fd):
        raise OSError("fchmod refused")

    monkeypatch.setattr(export_archive.os, "open", tracking_open)
    monkeypatch.setattr(export_archive, "restrict_fd_to_owner", failing_restrict)

    with pytest.raises(OSError, match="fchmod refused"):
        export_archive._open_private(tmp_path / "x.tmp", 0, "wb")

    assert len(opened) == 1
    with pytest.raises(OSError):  # EBADF: the descriptor was closed, not leaked
        os.fstat(opened[0])
