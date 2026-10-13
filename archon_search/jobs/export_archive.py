"""Export/import archive utilities for collection backup and restore.

Provides:
  - EXPORT_SCHEMA_VERSION: int constant identifying the archive format version.
  - ExportArchiveWriter: two-phase writer (temp JSONL → finalized .tar.gz).
  - ImportArchiveReader: validated streaming reader for import archives.
  - ArchiveRejectedError: an archive the import refuses, with a caller-safe message.
  - INVALID_ARCHIVE_DETAIL / INVALID_MANIFEST_DETAIL / manifest_error_detail: the
    wire-safe texts the REST route and the MCP tool share for an unreadable archive.
"""
from __future__ import annotations

import importlib.metadata
import io
import json
import logging
import tarfile
from pathlib import Path
from typing import IO, Iterator

from archon_search._path_safety import PathUnsafeError, validate_archive_members

logger = logging.getLogger(__name__)

EXPORT_SCHEMA_VERSION: int = 1


class ArchiveRejectedError(ValueError):
    """A refusal the import makes: the archive's content is unusable, or the target collection already exists.

    The message is fixed text built by archon-search, quoting only line numbers, its own
    field names and request values, and archive values through :func:`quote_archive_value`
    — never a library's or the server's exception text — so it is safe for a wire-facing
    ``detail`` or a job's ``error``.
    """


# Wire-safe detail for an import archive that does not open as a tar.gz (tarfile/gzip
# error text can reflect the archive's own bytes); the cause is logged.
INVALID_ARCHIVE_DETAIL = "archive could not be opened: invalid or corrupt tar.gz"

# Wire-safe detail for a manifest that failed to read for any reason other than an
# ArchiveRejectedError (whose own message is safe to send); the cause is logged.
INVALID_MANIFEST_DETAIL = "manifest.json could not be read"

#: Longest archive-supplied value an ArchiveRejectedError message quotes: an archive is
#: untrusted input, so a crafted manifest value cannot inflate a ``detail`` or ``error``.
ARCHIVE_VALUE_MAX_CHARS = 80
_TRUNCATION_MARK = "…"


def quote_archive_value(value: object) -> str:
    """``repr(value)``, cut to :data:`ARCHIVE_VALUE_MAX_CHARS` with a trailing ``…``."""
    quoted = repr(value)
    if len(quoted) <= ARCHIVE_VALUE_MAX_CHARS:
        return quoted
    return quoted[: ARCHIVE_VALUE_MAX_CHARS - len(_TRUNCATION_MARK)] + _TRUNCATION_MARK


def manifest_error_detail(exc: Exception, archive_path: Path) -> str:
    """The wire-safe text for a failed :meth:`ImportArchiveReader.read_manifest`.

    An :class:`ArchiveRejectedError` message or a ``PathUnsafeError`` reason code is
    text archon-search built itself; anything else is logged (with its traceback) and
    reported as :data:`INVALID_MANIFEST_DETAIL`.
    """
    if isinstance(exc, (ArchiveRejectedError, PathUnsafeError)):
        return str(exc)
    logger.warning("import archive %s: manifest could not be read: %s", archive_path, exc, exc_info=exc)
    return INVALID_MANIFEST_DETAIL


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
        #: 1-based ``documents.jsonl`` line of the document :meth:`iter_docs` last yielded.
        self.line_number: int = 0

    def read_manifest(self) -> dict:
        """Open the archive, validate its members, and return the parsed manifest.

        Raises:
            PathUnsafeError: if any tar member is unsafe (via :func:`validate_archive_members`).
            ArchiveRejectedError: if the manifest is not a regular file, not UTF-8 JSON
                (including an oversized int or nesting past the parser's recursion
                limit), not a JSON object, or is missing required keys.
        """
        with tarfile.open(self._archive_path, "r:gz") as tf:
            validate_archive_members(tf)
            member = tf.getmember("manifest.json")
            f = tf.extractfile(member)
            if f is None:
                raise ArchiveRejectedError("manifest.json member is not a regular file")
            try:
                manifest = json.loads(f.read().decode("utf-8"))
            # ValueError covers UnicodeDecodeError, JSONDecodeError and an oversized int;
            # RecursionError covers nesting deeper than the parser's recursion limit.
            except (ValueError, RecursionError) as exc:
                logger.warning("manifest.json of %s is not valid UTF-8 JSON: %s", self._archive_path, exc)
                raise ArchiveRejectedError("manifest.json is not valid UTF-8 JSON") from exc

        if not isinstance(manifest, dict):
            raise ArchiveRejectedError(
                f"manifest.json must be a JSON object, got {type(manifest).__name__}"
            )

        missing = _REQUIRED_MANIFEST_KEYS - manifest.keys()
        if missing:
            raise ArchiveRejectedError(
                f"manifest.json is missing required keys: {', '.join(sorted(missing))}"
            )

        return manifest

    def iter_docs(self, skip: int = 0, on_error: str = "fail") -> Iterator[dict]:
        """Stream documents from ``documents.jsonl``, skipping the first *skip* lines.

        Each yielded value is a parsed JSON dict representing one document.

        When *on_error* is ``"skip"``, corrupt lines (not UTF-8 JSON — including an
        oversized int or nesting past the parser's recursion limit — or not a JSON
        object) are logged, counted in :attr:`skipped_lines`, and skipped.  When
        ``"fail"`` (default), a corrupt line raises immediately, unlogged: the caller
        logs the refusal.  :attr:`line_number` holds the line of the document last
        yielded.

        Raises:
            ArchiveRejectedError: if *on_error* is ``"fail"`` and a line is corrupt
                (the message names its 1-based line number), or ``documents.jsonl``
                is not a regular file.
        """
        self.skipped_lines = 0
        with tarfile.open(self._archive_path, "r:gz") as tf:
            member = tf.getmember("documents.jsonl")
            f = tf.extractfile(member)
            if f is None:
                raise ArchiveRejectedError("documents.jsonl member is not a regular file")
            lines_skipped = 0
            lineno = 0
            for raw_line in f:
                lineno += 1
                line = raw_line.rstrip(b"\n")
                if not line:
                    continue
                if lines_skipped < skip:
                    lines_skipped += 1
                    continue
                cause: Exception | None = None
                try:
                    parsed = json.loads(line.decode("utf-8"))
                # Same cases as read_manifest: decode/syntax errors, oversized ints, deep nesting.
                except (ValueError, RecursionError) as exc:
                    cause, reason = exc, "not valid UTF-8 JSON"
                else:
                    reason = "" if isinstance(parsed, dict) else "not a JSON object"
                if reason:
                    if on_error != "skip":
                        # The caller logs the refusal, with its job context and this cause.
                        raise ArchiveRejectedError(f"Corrupt line {lineno}: {reason}") from cause
                    self.skipped_lines += 1
                    logger.warning("Corrupt line %d: %s", lineno, cause or reason)
                    continue
                self.line_number = lineno
                yield parsed
