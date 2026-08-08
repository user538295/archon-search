"""Export/import archive utilities for collection backup and restore.

Provides:
  - EXPORT_SCHEMA_VERSION: int constant identifying the archive format version.
  - ExportArchiveWriter: two-phase writer (temp JSONL → finalized .tar.gz).
  - ImportArchiveReader: validated streaming reader for import archives.
"""
from __future__ import annotations

import importlib.metadata
import io
import json
import logging
import tarfile
from pathlib import Path
from typing import IO, Iterator

from archon_search._path_safety import validate_archive_members

logger = logging.getLogger(__name__)

EXPORT_SCHEMA_VERSION: int = 1


def get_lancedb_version() -> str | None:
    """Resolve the installed lancedb package version for export manifests.

    Returns the version string on success, or ``None`` if the package is not
    installed (in which case a WARNING is logged). Added in D2-1.4 so the
    export manifest records the LanceDB version it was produced against for
    forensic / migration purposes.
    """
    try:
        return importlib.metadata.version("lancedb")
    except importlib.metadata.PackageNotFoundError:
        logger.warning("Could not determine lancedb version")
        return None

# Required keys in the manifest dict.
_REQUIRED_MANIFEST_KEYS = frozenset({
    "schema_version",
    "collection",
    "exported_at",
    "doc_count",
    "active_embedding_model",
})


class ExportArchiveWriter:
    """Two-phase archive writer: stream docs to a temp JSONL file, then finalize to .tar.gz.

    Usage::

        writer = ExportArchiveWriter(tmp_path)
        for doc in docs:
            writer.write_doc(doc)
        writer.finalize(manifest, archive_path)

    Or as a context manager (cleanup is called automatically on exception)::

        with writer:
            writer.write_doc(doc)
            writer.finalize(manifest, archive_path)
    """

    def __init__(self, tmp_path: Path) -> None:
        self._tmp_path = tmp_path
        self._lines_written: int = 0
        self._file: IO[bytes] | None = None
        tmp_path.parent.mkdir(parents=True, exist_ok=True)
        self._file = tmp_path.open("ab")

    def write_doc(self, doc: dict) -> None:
        """Serialize *doc* as a compact JSON line and append to the temp file."""
        if self._file is None:
            raise RuntimeError("ExportArchiveWriter is closed")
        line = json.dumps(doc, ensure_ascii=False) + "\n"
        self._file.write(line.encode())
        self._lines_written += 1

    @property
    def lines_written(self) -> int:
        """Number of documents written so far."""
        return self._lines_written

    def finalize(self, manifest: dict, archive_path: Path) -> None:
        """Close the temp file and build the final .tar.gz archive.

        The archive contains exactly two members:
          - ``manifest.json``: JSON-encoded *manifest* dict.
          - ``documents.jsonl``: the accumulated temp file contents.

        Calls :meth:`cleanup` at the end to remove the temp file.
        """
        if self._file is not None:
            self._file.close()
            self._file = None

        manifest_bytes = json.dumps(manifest, ensure_ascii=False).encode()
        with tarfile.open(archive_path, "w:gz") as tf:
            # Add manifest.json
            info = tarfile.TarInfo(name="manifest.json")
            info.size = len(manifest_bytes)
            tf.addfile(info, io.BytesIO(manifest_bytes))
            # Add documents.jsonl from the temp file
            tf.add(str(self._tmp_path), arcname="documents.jsonl")

        self.cleanup()

    def cleanup(self) -> None:
        """Close the temp file (if open) and delete it (if it exists)."""
        if self._file is not None:
            try:
                self._file.close()
            except Exception:
                pass
            self._file = None
        if self._tmp_path.exists():
            self._tmp_path.unlink()

    def __enter__(self) -> "ExportArchiveWriter":
        return self

    def __exit__(self, exc_type: object, exc_val: object, exc_tb: object) -> None:
        if exc_type is not None:
            self.cleanup()


class ImportArchiveReader:
    """Streaming reader for an archon-search collection export archive.

    Usage::

        reader = ImportArchiveReader(archive_path)
        manifest = reader.read_manifest()
        for doc in reader.iter_docs(skip=0):
            ...
    """

    def __init__(self, archive_path: Path) -> None:
        self._archive_path = archive_path
        self.skipped_lines: int = 0

    def read_manifest(self) -> dict:
        """Open the archive, validate its members, and return the parsed manifest.

        Raises:
            PathUnsafeError: if any tar member is unsafe (via :func:`validate_archive_members`).
            ValueError: if the manifest is missing required keys or is malformed.
        """
        with tarfile.open(self._archive_path, "r:gz") as tf:
            validate_archive_members(tf)
            member = tf.getmember("manifest.json")
            f = tf.extractfile(member)
            if f is None:
                raise ValueError("manifest.json member is not a regular file")
            manifest = json.loads(f.read().decode("utf-8"))

        if not isinstance(manifest, dict):
            raise ValueError(f"manifest.json must be a JSON object, got {type(manifest)}")

        missing = _REQUIRED_MANIFEST_KEYS - manifest.keys()
        if missing:
            raise ValueError(
                f"manifest.json is missing required keys: {', '.join(sorted(missing))}"
            )

        return manifest

    def iter_docs(self, skip: int = 0, on_error: str = "fail") -> Iterator[dict]:
        """Stream documents from ``documents.jsonl``, skipping the first *skip* lines.

        Each yielded value is a parsed JSON dict representing one document.

        When *on_error* is ``"skip"``, corrupt JSON lines are logged, counted
        in :attr:`skipped_lines`, and skipped.  When ``"fail"`` (default), a
        corrupt line raises immediately.

        Raises:
            ValueError: if *on_error* is ``"fail"`` and a line cannot be parsed
                as JSON (includes the 1-based line number in the message).
        """
        self.skipped_lines = 0
        with tarfile.open(self._archive_path, "r:gz") as tf:
            member = tf.getmember("documents.jsonl")
            f = tf.extractfile(member)
            if f is None:
                raise ValueError("documents.jsonl member is not a regular file")
            lines_skipped = 0
            lineno = 0
            for raw_line in f:
                lineno += 1
                line = raw_line.decode("utf-8").rstrip("\n")
                if not line:
                    continue
                if lines_skipped < skip:
                    lines_skipped += 1
                    continue
                try:
                    parsed = json.loads(line)
                except json.JSONDecodeError as exc:
                    if on_error == "skip":
                        self.skipped_lines += 1
                        logger.warning("Corrupt line %d: %s", lineno, exc)
                        continue
                    raise ValueError(f"Corrupt line {lineno}: {exc}") from exc
                yield parsed
