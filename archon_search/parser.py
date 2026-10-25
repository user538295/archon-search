"""Document parser — extracts plain text from various file formats.

Supported formats:
- Plain text: .md, .txt, .py, .js, .ts, .go, .rs, .java, .sh,
              .yaml, .yml, .json, .toml, .csv, .tsv (and any unknown extension)
- HTML: .html, .htm — via trafilatura
- PDF: .pdf — via docling (lazy import)
- Office: .docx, .pptx, .xlsx, .xls, .rtf, .epub, .eml, .msg — via markitdown (lazy import)
- Images: .png, .jpg, .jpeg, .tiff, .tif, .bmp, .webp — via docling OCR
"""

from __future__ import annotations

import asyncio
import logging
import multiprocessing
import os
import stat
import sys
import threading
import time
import zipfile
from collections.abc import Callable
from concurrent.futures import ProcessPoolExecutor
from concurrent.futures.process import BrokenProcessPool
from pathlib import Path
from typing import TYPE_CHECKING, Final

if TYPE_CHECKING:
    from docling.document_converter import DocumentConverter

    from archon_search.config import IngestConfig

from archon_search.constants import (
    BYTES_PER_MIB,
    DEFAULT_DOCLING_MAX_TASKS_PER_CHILD,
    DEFAULT_MAX_ZIP_ENTRIES,
    DEFAULT_MAX_ZIP_UNCOMPRESSED_MB,
    DEFAULT_PARSE_TIMEOUT_SECONDS,
    DEFAULT_PARSE_TIMEOUT_SECONDS_PER_MB,
)
from archon_search.enricher import PAGE_BREAK_MARKER

logger = logging.getLogger(__name__)

# docling renders the OCR bitmap at OcrOptions.scale before recognition. This override is
# IMAGE-ONLY. For images, scale multiplies the native bitmap (docling/backend/image_backend.py:
# `if scale != 1: img.resize(...)`), so 1.0 is a no-op that preserves source resolution; its
# default 3.0 turned a 1024px icon into a 3072px bitmap (9x the pixels) and was the dominant
# term in the 2026-08-19 OOM incident — measured 3.4x less retained memory, with recognised
# text unchanged on the one text-bearing fixture tested (a single synthetic screenshot; not a
# claim that scale never affects recognition on real-world imagery). For PDFs, scale is instead
# a 72-DPI render multiplier off the page geometry
# (docling/backend/pypdfium2_backend.py: `render(scale=scale * 1.5, ...)`), where 1.0 would mean
# rendering at 72 DPI instead of docling's default 216 DPI (scale 3.0) — one ninth the pixels.
# There is no source bitmap to preserve for a PDF, so PDFs are excluded from format_options
# below and keep docling's defaults; the worker-recycle bound (see `_docling_pool`), not this
# scale, is what caps PDF parse memory.
_OCR_SCALE: Final[float] = 1.0
_OCR_BACKEND: Final[str] = "onnxruntime"

# Set once per parse-worker process; see _docling_to_markdown.
_worker_converter: DocumentConverter | None = None

# Set True only by `_worker_initializer`, which ProcessPoolExecutor runs once per spawned
# parse-worker process (initial spawn and every max_tasks_per_child recycle). Deliberately not
# `multiprocessing.parent_process() is not None`: that predicate is true in ANY multiprocessing
# child, not just this one, so it over-matches if this module is ever imported by some other
# worker pool.
_IS_PARSE_WORKER = False

# How often the parse worker's watchdog thread polls for its parent's death.
_PARSE_WORKER_WATCHDOG_POLL_S: Final[float] = 2.0


def _watch_for_orphaning() -> None:
    """Daemon-thread body: exit this worker if its parent dies without telling it.

    A server killed by the OOM killer (the exact incident this module's fix exists to contain)
    SIGKILLs without warning its children. Verified empirically: a parent process holding the
    pool was killed, and the worker was still alive 8s later, holding its semaphores — an
    orphaned ~1 GB docling worker would sit resident until reboot, invisible to launchd/systemd,
    with the next server start adding another. `parent_process().is_alive()` waits on an OS
    sentinel (not a PID-reuse check), so it detects this reliably.
    """
    parent = multiprocessing.parent_process()
    if parent is None:
        # Not a multiprocessing child, so there is no parent to outlive and nothing to watch.
        # Returning (rather than falling through to os._exit) matters because this is one
        # faithful test improvement away from firing in-process: the inline executor stub used
        # by the default-lane guards ignores `initializer` today, but teaching it to honour
        # `initializer` — the obvious way to make it match the real executor — would run
        # `_worker_initializer` in the pytest process, where parent_process() is None under
        # xdist (execnet subprocesses are not multiprocessing children). Without this guard
        # that kills the xdist worker outright: no traceback, no atexit, no teardown, just a
        # vanished worker.
        return
    while parent.is_alive():
        time.sleep(_PARSE_WORKER_WATCHDOG_POLL_S)
    os._exit(1)  # no cleanup is possible or needed; the parent is already gone


# docling's layout model runs through transformers' RT-DETR v2, whose position embedding
# builds a float64 tensor on the model's device; Apple's MPS backend has no float64, so every
# PDF/image conversion fails ("Cannot convert a MPS Tensor to float64 dtype", docling 2.132 and
# 2.142.1 on transformers 5.10 and 5.13; docling 2.117 was unaffected). docling's own AUTO picks
# MPS whenever it exists, i.e. on every Apple-silicon Mac; Intel Macs have no MPS and already
# fall back to CPU. DOCLING_DEVICE is docling's documented override (AcceleratorOptions reads it
# at construction), so an operator-set value still wins.
_DOCLING_DEVICE_ENV: Final[str] = "DOCLING_DEVICE"
_DOCLING_DEVICE_OFF_MPS: Final[str] = "cpu"


def _pin_docling_off_mps() -> None:
    if sys.platform == "darwin":
        os.environ.setdefault(_DOCLING_DEVICE_ENV, _DOCLING_DEVICE_OFF_MPS)


def _worker_initializer() -> None:
    """ProcessPoolExecutor `initializer`: runs once per spawned parse-worker process."""
    global _IS_PARSE_WORKER
    _IS_PARSE_WORKER = True
    _pin_docling_off_mps()
    threading.Thread(target=_watch_for_orphaning, daemon=True).start()


def _in_parse_worker() -> bool:
    """True when running inside the recycled parse worker, not in the process that owns it."""
    return _IS_PARSE_WORKER


# Wire-facing ``ParseError.detail`` values. ``pipeline.ingest_file`` formats the detail into
# ``IngestResult.error`` (:data:`PARSE_FAILED_TEMPLATE`, code ``_types.PARSE_ERROR_CODE``), which
# is MCP-visible via ``IngestResultSchema.from_result`` (server/mcp_schemas.py). A library cause
# (docling, RapidOCR, markitdown, trafilatura, OSError) may embed model or file paths, so its
# text stays in the exception message and the server log; only codebase-built text goes here.
PARSE_FAILED_DETAIL: Final[str] = "the document could not be parsed; see the server log for details"
# ``Failed to parse <path>: <reason>``: ParseError's own log-only message (reason = the raw
# cause) and the wire-facing ``IngestResult.error`` (reason = ``ParseError.detail``).
PARSE_FAILED_TEMPLATE: Final[str] = "Failed to parse {path}: {reason}"
# A lost parse worker (never surface str(BrokenProcessPool) on the wire).
PARSE_WORKER_LOST_DETAIL: Final[str] = (
    "The document parser worker was lost while converting this file "
    "(it may have been killed by the OS, e.g. out of memory); retry the ingest."
)
# markitdown is a core dependency, so its absence means a broken install, not a missing extra.
MARKITDOWN_MISSING_DETAIL: Final[str] = "markitdown is not installed; reinstall archon-search to restore it"
# Parse timed out: the worker was killed and replaced (the document, not the server, failed).
PARSE_TIMEOUT_DETAIL: Final[str] = (
    "The document took too long to convert and was abandoned (`[ingest].parse_timeout_seconds`); "
    "it may be too large or malformed."
)
# A zip-based Office file declaring more entries or more inflated bytes than the `[ingest]` zip limits allow.
ZIP_LIMIT_DETAIL: Final[str] = (
    "The document is a zip archive larger than the configured limits when unpacked "
    "(`[ingest].max_zip_entries`, `[ingest].max_zip_uncompressed_mb`)."
)
# The path is a symlink, FIFO, device or directory: only regular files are read.
NOT_REGULAR_FILE_DETAIL: Final[str] = "the path is not a regular file"

# Office formats stored as zip archives, whose central directory is checked before markitdown opens them.
_ZIP_EXTENSIONS: Final[frozenset[str]] = frozenset({".docx", ".pptx", ".xlsx", ".epub"})


def _require_regular_file(path: Path) -> None:
    """Raise ``ParseError`` unless *path* is a regular file that is not a symlink (``lstat``, so nothing is opened).

    Reading a FIFO blocks the parse thread until a writer appears, a device such as ``/dev/zero`` never ends, and a
    symlink may point at something the ingest policy never validated. The check and the later open are separate
    calls; the plain-text readers re-check on the descriptor they open (:func:`_read_regular_text`).
    """
    try:
        mode = os.lstat(path).st_mode
    except OSError as exc:
        raise ParseError(path, exc) from exc
    if not stat.S_ISREG(mode):
        raise ParseError(path, OSError(NOT_REGULAR_FILE_DETAIL), detail=NOT_REGULAR_FILE_DETAIL)


def _read_regular_text(path: Path) -> str:
    """Read *path* as UTF-8 (undecodable bytes replaced) through a descriptor verified to be a regular file.

    Opened ``O_NOFOLLOW | O_NONBLOCK``: a symlink at *path* is refused and opening a FIFO cannot block, and the
    ``fstat`` on the open descriptor closes the window between :func:`_require_regular_file` and the read.
    """
    # O_NOFOLLOW / O_NONBLOCK do not exist on Windows; ``_require_regular_file`` already refuses a symlink there.
    flags = (
        os.O_RDONLY
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_NONBLOCK", 0)
        | getattr(os, "O_CLOEXEC", 0)
    )
    descriptor = os.open(path, flags)
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise ParseError(path, OSError(NOT_REGULAR_FILE_DETAIL), detail=NOT_REGULAR_FILE_DETAIL)
        with os.fdopen(descriptor, "rb", closefd=False) as handle:
            return handle.read().decode("utf-8", errors="replace")
    finally:
        os.close(descriptor)


class ParseError(Exception):
    """Raised when a document cannot be parsed.

    ``str(exc)`` names *path* and the raw *cause* — log it, never send it. ``detail`` is the
    wire-safe text: :data:`PARSE_FAILED_DETAIL` unless the raise site curated its own.
    """

    def __init__(self, path: Path, cause: Exception, *, detail: str = PARSE_FAILED_DETAIL) -> None:
        super().__init__(PARSE_FAILED_TEMPLATE.format(path=path, reason=cause))
        self.path = path
        self.cause = cause
        self.detail = detail


_PLAIN_EXTENSIONS = {
    ".md", ".txt", ".py", ".js", ".ts", ".go", ".rs", ".java", ".sh",
    ".yaml", ".yml", ".json", ".toml",
    ".csv", ".tsv",  # intentionally treated as raw text; no CSV/TSV parser (structure is not useful for retrieval)
}
_HTML_EXTENSIONS = {".html", ".htm"}
_PDF_EXTENSIONS = {".pdf"}
_OFFICE_EXTENSIONS = {
    ".docx", ".pptx", ".xlsx",
    ".xls", ".rtf", ".epub", ".eml", ".msg",
    # .doc, .ppt, .odt excluded: markitdown has no converter for these formats and raises UnsupportedFormatException
}
_IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".tiff", ".tif", ".bmp", ".webp"}
# .gif and .svg are intentionally excluded: .gif has animated frames (OCR on frame 0 is
# misleading) and .svg is XML text (plain-text fallback is more appropriate).


def _build_docling_converter() -> DocumentConverter:
    """Build a converter that OCRs images at the source resolution instead of upscaling 3x.

    IMAGE-ONLY override: for images, `scale` multiplies the native bitmap, so 1.0 preserves
    source resolution (see the module comment above `_OCR_SCALE`). For PDFs, `scale` is a
    72-DPI render multiplier with no source bitmap to preserve, so `InputFormat.PDF` is
    deliberately absent from `format_options` — PDFs keep docling's own defaults.

    The OCR options instance must be *concrete*: with the default (auto) options docling
    rebuilds RapidOcrOptions copying only `backend` and `mode`, so a `scale` set on the
    default object is silently discarded (`models/stages/ocr/auto_ocr_model.py`, verified on
    docling 2.117). Deriving the options by copying docling's default and overriding only
    `scale` would therefore reintroduce the 3x upscale while looking correct — do not.

    Consequence, deliberate: images are pinned to RapidOCR while PDFs keep docling's *auto*
    engine selection (ocrmac on darwin, then nemotron_ocr on linux, then rapidocr, easyocr).
    Today every candidate but rapidocr is absent from uv.lock so both paths resolve to the
    same engine, but installing e.g. ocrmac would split them. `test_build_docling_converter_*`
    asserts the PDF path stays `kind == "auto"` so that split cannot happen silently.
    """
    from docling.datamodel.pipeline_options import (
        RapidOcrOptions,
    )
    from docling.document_converter import (
        DocumentConverter,
        ImageFormatOption,
        InputFormat,
    )

    # Start from docling's own default for this format rather than a bare PdfPipelineOptions:
    # ImageFormatOption's pipeline_cls (StandardPdfPipeline) declares ThreadedPdfPipelineOptions,
    # and passing the base class works only by duck typing — the first docling release adding a
    # threaded-only field would break mid-conversion, visible solely in the opt-in docling lane.
    pipeline_options = ImageFormatOption().pipeline_options.model_copy(
        update={"ocr_options": RapidOcrOptions(backend=_OCR_BACKEND, scale=_OCR_SCALE)}
    )
    return DocumentConverter(
        format_options={
            InputFormat.IMAGE: ImageFormatOption(pipeline_options=pipeline_options),
        }
    )


def _docling_to_markdown(path: str) -> tuple[bool, str]:
    """Convert *path* with docling — the parse worker's entry point.

    Module-level and picklable because the pool uses the `spawn` start method.

    Returns `(ok, payload)` and never lets an exception cross the process boundary:
    ParseError does not survive pickling (Exception.__reduce__ replays the single formatted
    `args` string), and unpickling a docling exception would import docling in the caller —
    the one thing this worker exists to prevent. On failure *payload* carries the original
    exception's type and message so the caller can rebuild an equivalent ParseError cause.
    """
    global _worker_converter
    try:
        converter = _worker_converter
        if converter is None:
            converter = _build_docling_converter()
            if _in_parse_worker():
                # Reuse the converter, and the OCR models it loads, for this worker's
                # remaining tasks. Caching it in a process that never exits is precisely the
                # unbounded retention this worker removes, so any in-process caller rebuilds.
                _worker_converter = converter
        text = converter.convert(path).document.export_to_markdown(
            page_break_placeholder=PAGE_BREAK_MARKER
        )
    except Exception as exc:  # noqa: BLE001 — docling can raise anything; the worker reports failure as (False, message)
        return False, f"{type(exc).__name__}: {exc}"
    return True, text.strip() if text else ""


class DocumentParser:
    """Parses documents into plain text, routing by file extension."""

    def __init__(
        self,
        max_tasks_per_child: int = DEFAULT_DOCLING_MAX_TASKS_PER_CHILD,
        *,
        parse_timeout_seconds: float = DEFAULT_PARSE_TIMEOUT_SECONDS,
        parse_timeout_seconds_per_mb: float = DEFAULT_PARSE_TIMEOUT_SECONDS_PER_MB,
        max_zip_entries: int = DEFAULT_MAX_ZIP_ENTRIES,
        max_zip_uncompressed_mb: int = DEFAULT_MAX_ZIP_UNCOMPRESSED_MB,
    ) -> None:
        # Fail at construction, not at the first PDF/image parse (possibly hours later): the
        # pool itself is lazy, and ProcessPoolExecutor only validates once it is finally built.
        # Mirror both of its checks, not just the range one — it raises TypeError for non-int
        # too, so a float would otherwise slip through here and fail at that first parse, which
        # is precisely the deferred failure this guard exists to eliminate. bool is excluded
        # explicitly because bool is a subclass of int (same rule as config.py's TOML path).
        if not isinstance(max_tasks_per_child, int) or isinstance(max_tasks_per_child, bool):
            raise TypeError(
                f"max_tasks_per_child must be an int, got {type(max_tasks_per_child).__name__}"
            )
        if max_tasks_per_child < 1:
            raise ValueError(f"max_tasks_per_child must be >= 1, got {max_tasks_per_child!r}")
        self._max_tasks_per_child = max_tasks_per_child
        self._parse_timeout_seconds = parse_timeout_seconds
        self._parse_timeout_seconds_per_mb = parse_timeout_seconds_per_mb
        self._max_zip_entries = max_zip_entries
        self._max_zip_uncompressed_bytes = max_zip_uncompressed_mb * BYTES_PER_MIB
        self._pool: ProcessPoolExecutor | None = None  # lazy-created on first docling call
        self._pool_lock = threading.Lock()
        self._worker_lock = threading.Lock()

    @classmethod
    def from_config(cls, ingest: IngestConfig) -> DocumentParser:
        """A parser bounded by the ``[ingest]`` section: worker recycling, the parse timeout and the zip limits."""
        return cls(
            max_tasks_per_child=ingest.max_tasks_per_child,
            parse_timeout_seconds=ingest.parse_timeout_seconds,
            parse_timeout_seconds_per_mb=ingest.parse_timeout_seconds_per_mb,
            max_zip_entries=ingest.max_zip_entries,
            max_zip_uncompressed_mb=ingest.max_zip_uncompressed_mb,
        )

    async def parse(self, path: Path) -> str:
        """Parse *path* and return its text content.

        All CPU-bound work runs in a thread via asyncio.to_thread(); the docling formats
        (PDF, images) run in a separate worker process driven from that thread.
        Raises ParseError on failure.
        """
        suffix = path.suffix.lower()
        if suffix in _HTML_EXTENSIONS:
            fn = self._parse_html
        elif suffix in _PDF_EXTENSIONS:
            fn = self._parse_pdf
        elif suffix in _OFFICE_EXTENSIONS:
            fn = self._parse_office
        elif suffix in _IMAGE_EXTENSIONS:
            fn = self._parse_image
        else:
            fn = self._parse_plain

        _require_regular_file(path)
        try:
            return await asyncio.to_thread(fn, path)
        except ParseError:
            raise
        except Exception as exc:
            raise ParseError(path, exc) from exc

    # ------------------------------------------------------------------
    # Format handlers
    # ------------------------------------------------------------------

    def _parse_plain(self, path: Path) -> str:
        try:
            return _read_regular_text(path)
        except ParseError:
            raise
        except Exception as exc:
            raise ParseError(path, exc) from exc

    def _parse_html(self, path: Path) -> str:
        try:
            import trafilatura  # lazy: optional search extra
            raw = _read_regular_text(path)
            extracted = trafilatura.extract(raw, include_tables=True, include_links=False)
            if extracted is None:
                return raw
            return extracted
        except ParseError:
            raise
        except Exception as exc:
            raise ParseError(path, exc) from exc

    def _docling_pool(self) -> ProcessPoolExecutor:
        """The single-worker pool that runs docling, recycled every max_tasks_per_child files.

        Worker exit is the only complete reclaim of the native memory docling/RapidOCR/
        onnxruntime retain per conversion — CPython can never unload a C extension in-process.
        `spawn` is explicit for clarity of intent, not to avoid a failure: onnxruntime/torch are
        unsafe under `fork`, and `ProcessPoolExecutor` itself already defaults to `spawn`
        whenever `max_tasks_per_child` is set and no `mp_context` is given (CPython
        `concurrent/futures/process.py`) — so this is pinning the safe choice explicitly, not
        working around a platform default that would otherwise break the Docker image. Creation
        is lazy: spawning a worker re-imports docling, which must never be paid by a caller that
        only ever parses text, nor on the server's startup path. Dropping the executor stops its
        worker, so the pool dies with its parser.

        Guarded by `self._pool_lock` (double-checked): concurrent ingest jobs call
        `parser.parse()` from multiple `asyncio.to_thread` workers — job-scope exclusivity is
        dropped after dispatch (see
        Documentation/Backlog/2026-08-19-060-ingest-job-level-lock-dropped-brief.md item 3), so
        an unguarded check-then-set race here would construct two `ProcessPoolExecutor`s.
        Post-brief-010 the duplicate would cost a spawned worker process with a ~1 GB model
        stack, not merely a second in-process `DocumentConverter` — worth a Lock even though the
        critical section is just object construction.
        """
        if self._pool is not None:
            return self._pool
        with self._pool_lock:
            if self._pool is None:
                logger.info(
                    "docling parse worker pool starting (max_tasks_per_child=%d)",
                    self._max_tasks_per_child,
                )
                self._pool = ProcessPoolExecutor(
                    max_workers=1,
                    mp_context=multiprocessing.get_context("spawn"),
                    max_tasks_per_child=self._max_tasks_per_child,
                    initializer=_worker_initializer,
                )
            return self._pool

    def _parse_with_docling(self, path: Path) -> str:
        return self._run_in_worker(_docling_to_markdown, path)

    def _parse_timeout_for(self, path: Path) -> float:
        """The wall-clock bound for converting *path*: ``parse_timeout_seconds`` plus the per-MiB allowance for its size.

        A file that cannot be measured gets the base bound alone.
        """
        try:
            size_bytes = path.stat().st_size
        except OSError:
            return self._parse_timeout_seconds
        return self._parse_timeout_seconds + self._parse_timeout_seconds_per_mb * (size_bytes / BYTES_PER_MIB)

    def _run_in_worker(self, fn: Callable[[str], tuple[bool, str]], path: Path) -> str:
        """Run ``fn(str(path))`` in the docling worker, bounded by :meth:`_parse_timeout_for` (``parse_timeout_seconds``
        plus ``parse_timeout_seconds_per_mb`` per MiB of the file).

        One parse at a time (``_worker_lock``): the pool has a single worker, so serialising here makes the
        timeout cover the parse itself and never the wait behind another caller's parse. A parse that outlives the
        timeout fails *this* document with :data:`PARSE_TIMEOUT_DETAIL`, and the pool is discarded with its worker
        process killed, so the next document gets a fresh worker instead of queueing behind a wedged one forever.
        """
        timeout_seconds = self._parse_timeout_for(path)
        with self._worker_lock:
            pool = self._docling_pool()
            try:
                ok, payload = pool.submit(fn, str(path)).result(timeout=timeout_seconds)
            # BrokenProcessPool subclasses RuntimeError, so this clause MUST stay above
            # `except Exception` below — reordering it would silently disable recovery, since the
            # broader clause would catch it first and never drop the broken pool.
            except BrokenProcessPool as exc:
                # A worker killed from outside (OOM killer) would otherwise fail every later parse
                # for the life of the process; drop it so the next file gets a fresh one. `_terminate_
                # broken` (concurrent/futures/process.py) has already terminated and joined the dead
                # workers before this exception reaches us, so there is nothing left to shut down —
                # only the reference needs dropping. Compare identity before clearing: a concurrent
                # caller may already have raced past this same broken pool and installed a fresh one
                # via `_docling_pool()`'s lock; clearing unconditionally would orphan that live
                # replacement.
                with self._pool_lock:
                    if self._pool is pool:
                        self._pool = None
                logger.warning("docling parse worker pool broken, dropping it: %s", exc)
                raise ParseError(
                    path, RuntimeError(PARSE_WORKER_LOST_DETAIL), detail=PARSE_WORKER_LOST_DETAIL
                ) from exc
            except TimeoutError as exc:
                self._discard_pool(pool)
                logger.warning(
                    "docling parse of %s exceeded %.0fs; its worker was killed and will be replaced",
                    path, timeout_seconds,
                )
                raise ParseError(path, TimeoutError(PARSE_TIMEOUT_DETAIL), detail=PARSE_TIMEOUT_DETAIL) from exc
            except Exception as exc:
                raise ParseError(path, exc) from exc
        if not ok:
            raise ParseError(path, RuntimeError(payload))
        return payload

    def _discard_pool(self, pool: ProcessPoolExecutor) -> None:
        """Drop *pool* and kill its worker processes: a task that is already running cannot be cancelled.

        ``ProcessPoolExecutor.shutdown`` cannot interrupt a running task, and exposes no handle on its workers; the
        worker processes are reached through ``_processes`` (the same attribute CPython's own broken-pool handling
        terminates). The reference is cleared only if it still names *pool*, as in the broken-pool path.
        """
        with self._pool_lock:
            if self._pool is pool:
                self._pool = None
        workers = list(getattr(pool, "_processes", {}).values())
        pool.shutdown(wait=False, cancel_futures=True)
        for worker in workers:
            worker.terminate()

    def _parse_pdf(self, path: Path) -> str:
        return self._parse_with_docling(path)

    def _parse_image(self, path: Path) -> str:
        return self._parse_with_docling(path)

    def _check_zip_bounds(self, path: Path) -> None:
        """Refuse a zip-based file whose central directory declares too many entries or too many inflated bytes.

        markitdown's readers inflate entries through Python's ``zipfile``, which stops at an entry's declared size, so
        the declared sizes are a real bound on what is read. A file that is not a zip is left to markitdown.
        """
        if path.suffix.lower() not in _ZIP_EXTENSIONS or not zipfile.is_zipfile(path):
            return
        with zipfile.ZipFile(path) as archive:
            entries = archive.infolist()
            declared = sum(entry.file_size for entry in entries)
        if len(entries) > self._max_zip_entries or declared > self._max_zip_uncompressed_bytes:
            logger.warning(
                "refusing %s: %d zip entries, %d bytes inflated (limits %d and %d)",
                path, len(entries), declared, self._max_zip_entries, self._max_zip_uncompressed_bytes,
            )
            raise ParseError(path, ValueError(ZIP_LIMIT_DETAIL), detail=ZIP_LIMIT_DETAIL)

    def _parse_office(self, path: Path) -> str:
        try:
            from markitdown import MarkItDown
        except ImportError as exc:
            raise ParseError(
                path,
                ImportError(MARKITDOWN_MISSING_DETAIL),
                detail=MARKITDOWN_MISSING_DETAIL,
            ) from exc
        try:
            self._check_zip_bounds(path)
            return MarkItDown().convert(str(path)).text_content or ""
        except ParseError:
            raise
        except Exception as exc:
            raise ParseError(path, exc) from exc
