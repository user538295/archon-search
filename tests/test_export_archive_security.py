"""Archive bounds and namespace binding of ``ImportArchiveReader`` / the archive directory layout (SecReview #4, #7).

An archive is untrusted input: ``read_manifest`` used to ``f.read()`` whatever the member held and ``iter_docs`` to
read whatever a line held, so a small gzip bomb could pin the process's memory. The manifest also carried no owner,
so an archive copied from one namespace's directory into another's imported there unchecked.
"""
from __future__ import annotations

import io
import json
import tarfile
from pathlib import Path

import pytest

from archon_search.jobs import export_archive
from archon_search.jobs.export_archive import (
    EXPORT_SCHEMA_VERSION,
    IMPORT_DOC_LINE_MAX_BYTES,
    IMPORT_MANIFEST_MAX_BYTES,
    ArchiveRejectedError,
    ImportArchiveReader,
    backups_dir,
    exports_dir,
    importable_dirs,
)

_SMALL_LINE_LIMIT = 64


def _manifest(**overrides: object) -> dict[str, object]:
    manifest: dict[str, object] = {
        "schema_version": EXPORT_SCHEMA_VERSION,
        "collection": "docs",
        "namespace": "team-a",
        "exported_at": "2024-01-01T00:00:00Z",
        "doc_count": 1,
        "active_embedding_model": "m",
    }
    manifest.update(overrides)
    return manifest


def _archive(tmp_path: Path, *, manifest: bytes, documents: bytes = b"") -> Path:
    path = tmp_path / "a.tar.gz"
    with tarfile.open(path, "w:gz") as tf:
        for name, payload in (("manifest.json", manifest), ("documents.jsonl", documents)):
            info = tarfile.TarInfo(name=name)
            info.size = len(payload)
            tf.addfile(info, io.BytesIO(payload))
    return path


# --- manifest ----------------------------------------------------------------------------------------------


def test_a_manifest_without_a_namespace_is_read_so_the_importer_can_judge_it_by_location(tmp_path: Path) -> None:
    manifest = _manifest()
    del manifest["namespace"]
    archive = _archive(tmp_path, manifest=json.dumps(manifest).encode())

    assert "namespace" not in ImportArchiveReader(archive).read_manifest()


def test_a_manifest_larger_than_the_limit_is_rejected_without_being_parsed(tmp_path: Path) -> None:
    padding = "x" * IMPORT_MANIFEST_MAX_BYTES
    archive = _archive(tmp_path, manifest=json.dumps(_manifest(description=padding)).encode())

    with pytest.raises(ArchiveRejectedError, match="manifest.json exceeds"):
        ImportArchiveReader(archive).read_manifest()


def test_a_manifest_exactly_at_the_limit_is_read(tmp_path: Path) -> None:
    base = json.dumps(_manifest(description="")).encode()
    padding = "x" * (IMPORT_MANIFEST_MAX_BYTES - len(base))
    payload = json.dumps(_manifest(description=padding)).encode()
    assert len(payload) == IMPORT_MANIFEST_MAX_BYTES
    archive = _archive(tmp_path, manifest=payload)

    assert ImportArchiveReader(archive).read_manifest()["namespace"] == "team-a"


# --- document lines ----------------------------------------------------------------------------------------


def _oversized_second_line_archive(tmp_path: Path) -> Path:
    ok = json.dumps({"n": 1}).encode()
    too_long = json.dumps({"text": "y" * (_SMALL_LINE_LIMIT * 3)}).encode()
    last = json.dumps({"n": 3}).encode()
    return _archive(tmp_path, manifest=json.dumps(_manifest()).encode(), documents=b"\n".join([ok, too_long, last]) + b"\n")


def test_a_document_line_over_the_limit_fails_the_import_when_errors_are_not_skipped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(export_archive, "IMPORT_DOC_LINE_MAX_BYTES", _SMALL_LINE_LIMIT)
    reader = ImportArchiveReader(_oversized_second_line_archive(tmp_path))

    with pytest.raises(ArchiveRejectedError, match="Corrupt line 2: line exceeds the size limit"):
        list(reader.iter_docs(on_error="fail"))


def test_a_document_line_over_the_limit_is_skipped_and_the_next_line_still_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(export_archive, "IMPORT_DOC_LINE_MAX_BYTES", _SMALL_LINE_LIMIT)
    reader = ImportArchiveReader(_oversized_second_line_archive(tmp_path))

    docs = list(reader.iter_docs(on_error="skip"))

    assert [d["n"] for d in docs] == [1, 3]
    assert reader.skipped_lines == 1
    assert reader.line_number == 3


def test_a_line_exactly_at_the_limit_is_accepted(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(export_archive, "IMPORT_DOC_LINE_MAX_BYTES", _SMALL_LINE_LIMIT)
    stem = json.dumps({"t": ""}).encode()
    line = json.dumps({"t": "z" * (_SMALL_LINE_LIMIT - len(stem))}).encode()
    assert len(line) == _SMALL_LINE_LIMIT
    archive = _archive(tmp_path, manifest=json.dumps(_manifest()).encode(), documents=line + b"\n")

    assert len(list(ImportArchiveReader(archive).iter_docs())) == 1


def test_an_oversized_line_before_the_resume_position_is_counted_as_consumed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(export_archive, "IMPORT_DOC_LINE_MAX_BYTES", _SMALL_LINE_LIMIT)
    reader = ImportArchiveReader(_oversized_second_line_archive(tmp_path))

    docs = list(reader.iter_docs(skip=2, on_error="fail"))

    assert [d["n"] for d in docs] == [3], "a resume past the oversized line must not re-fail on it"


def test_the_default_limits_leave_room_for_a_real_chunk() -> None:
    """A chunk line holds text, a base64 vector (4 bytes per float, 4/3 for base64) and capped metadata."""
    largest_plausible_vector_floats = 8192
    base64_vector_bytes = largest_plausible_vector_floats * 4 * 4 // 3
    assert IMPORT_DOC_LINE_MAX_BYTES > 100 * base64_vector_bytes


# --- namespace-bound directories ---------------------------------------------------------------------------


def test_exports_are_written_below_a_per_namespace_directory(tmp_path: Path) -> None:
    assert exports_dir(tmp_path, "team-a") == tmp_path / "exports" / "team-a"


def test_backups_live_in_the_per_namespace_directory_the_backup_loop_writes(tmp_path: Path) -> None:
    assert backups_dir(tmp_path / "backups", "team-a") == tmp_path / "backups" / "team-a"


def test_an_import_may_only_read_the_callers_own_export_and_backup_directories(tmp_path: Path) -> None:
    assert importable_dirs(tmp_path, str(tmp_path / "backups"), "team-a") == [
        tmp_path / "exports" / "team-a",
        tmp_path / "backups" / "team-a",
    ]


def test_an_unset_backup_directory_means_the_default_below_the_data_directory(tmp_path: Path) -> None:
    assert importable_dirs(tmp_path, "", "team-a") == [
        tmp_path / "exports" / "team-a",
        tmp_path / "backups" / "team-a",
    ]
