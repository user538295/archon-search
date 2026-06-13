"""Unit tests for Task 2.1: validate_export_path() and validate_archive_members()."""
from __future__ import annotations

import io
import json
import tarfile
from pathlib import Path

import pytest

from archon_search._path_safety import (
    PathUnsafeError,
    validate_archive_members,
    validate_export_path,
)


# ---------------------------------------------------------------------------
# validate_export_path
# ---------------------------------------------------------------------------


def test_validate_export_path_allowed(tmp_path: Path) -> None:
    """Path within an allowed base dir passes validation."""
    allowed = tmp_path / "exports"
    allowed.mkdir()
    target = allowed / "my-export.tar.gz"
    result = validate_export_path(str(target), [allowed])
    assert result == target.resolve()


def test_validate_export_path_outside_raises(tmp_path: Path) -> None:
    """Path outside all allowed dirs raises PathUnsafeError with reason outside_allowed_dirs."""
    allowed = tmp_path / "exports"
    allowed.mkdir()
    outside = tmp_path / "other" / "archive.tar.gz"
    with pytest.raises(PathUnsafeError) as exc_info:
        validate_export_path(str(outside), [allowed])
    assert exc_info.value.reason == "outside_allowed_dirs"


def test_validate_export_path_dotdot_still_rejected(tmp_path: Path) -> None:
    """Paths containing '..' are rejected by the parent validate_ingest_path before allowlist check."""
    allowed = tmp_path / "exports"
    allowed.mkdir()
    with pytest.raises(PathUnsafeError) as exc_info:
        validate_export_path(str(allowed / ".." / "outside"), [allowed])
    # validate_ingest_path catches this first
    assert exc_info.value.reason == "contains_dotdot"


def test_validate_export_path_multiple_allowed_dirs(tmp_path: Path) -> None:
    """Path within any one of multiple allowed dirs passes."""
    dir_a = tmp_path / "a"
    dir_a.mkdir()
    dir_b = tmp_path / "b"
    dir_b.mkdir()
    target = dir_b / "archive.tar.gz"
    result = validate_export_path(str(target), [dir_a, dir_b])
    assert result == target.resolve()


def test_validate_export_path_relative_rejected(tmp_path: Path) -> None:
    """Relative paths are rejected before the allowlist check."""
    allowed = tmp_path / "exports"
    allowed.mkdir()
    with pytest.raises(PathUnsafeError) as exc_info:
        validate_export_path("relative/path.tar.gz", [allowed])
    assert exc_info.value.reason == "not_absolute"


# ---------------------------------------------------------------------------
# validate_archive_members
# ---------------------------------------------------------------------------


def _build_tar(members: list[tuple[str, bytes]], tmp_path: Path) -> Path:
    """Helper: write a tar.gz with the given (name, content) pairs."""
    archive = tmp_path / "test.tar.gz"
    with tarfile.open(archive, "w:gz") as tf:
        for name, content in members:
            info = tarfile.TarInfo(name=name)
            info.size = len(content)
            tf.addfile(info, io.BytesIO(content))
    return archive


def test_validate_archive_members_valid(tmp_path: Path) -> None:
    """A tar with exactly manifest.json + documents.jsonl passes."""
    archive = _build_tar(
        [("manifest.json", b"{}"), ("documents.jsonl", b"")],
        tmp_path,
    )
    with tarfile.open(archive, "r:gz") as tf:
        validate_archive_members(tf)  # must not raise


def test_validate_archive_members_extra_member(tmp_path: Path) -> None:
    """A tar with an extra entry raises PathUnsafeError."""
    archive = _build_tar(
        [
            ("manifest.json", b"{}"),
            ("documents.jsonl", b""),
            ("extra.txt", b"surprise"),
        ],
        tmp_path,
    )
    with tarfile.open(archive, "r:gz") as tf:
        with pytest.raises(PathUnsafeError) as exc_info:
            validate_archive_members(tf)
    assert exc_info.value.reason == "unsafe_tar_member"


def test_validate_archive_members_traversal_name(tmp_path: Path) -> None:
    """A member with '..' in its path raises PathUnsafeError."""
    archive = tmp_path / "bad.tar.gz"
    with tarfile.open(archive, "w:gz") as tf:
        info = tarfile.TarInfo(name="../../etc/passwd")
        info.size = 0
        tf.addfile(info, io.BytesIO(b""))
    with tarfile.open(archive, "r:gz") as tf:
        with pytest.raises(PathUnsafeError) as exc_info:
            validate_archive_members(tf)
    assert exc_info.value.reason == "unsafe_tar_member"


def test_validate_archive_members_absolute_name(tmp_path: Path) -> None:
    """A member with an absolute path name raises PathUnsafeError."""
    archive = tmp_path / "abs.tar.gz"
    with tarfile.open(archive, "w:gz") as tf:
        info = tarfile.TarInfo(name="/etc/passwd")
        info.size = 0
        tf.addfile(info, io.BytesIO(b""))
    with tarfile.open(archive, "r:gz") as tf:
        with pytest.raises(PathUnsafeError) as exc_info:
            validate_archive_members(tf)
    assert exc_info.value.reason == "unsafe_tar_member"


def test_validate_archive_members_only_manifest(tmp_path: Path) -> None:
    """A tar with only manifest.json (missing documents.jsonl) raises PathUnsafeError."""
    archive = _build_tar([("manifest.json", b"{}")], tmp_path)
    with tarfile.open(archive, "r:gz") as tf:
        with pytest.raises(PathUnsafeError) as exc_info:
            validate_archive_members(tf)
    assert exc_info.value.reason == "unsafe_tar_member"


def test_validate_archive_members_only_documents(tmp_path: Path) -> None:
    """A tar with only documents.jsonl (missing manifest.json) raises PathUnsafeError."""
    archive = _build_tar([("documents.jsonl", b"")], tmp_path)
    with tarfile.open(archive, "r:gz") as tf:
        with pytest.raises(PathUnsafeError) as exc_info:
            validate_archive_members(tf)
    assert exc_info.value.reason == "unsafe_tar_member"
