"""packages/archon-search/tests/test_parser.py — unit tests for DocumentParser."""
from __future__ import annotations

import asyncio
import concurrent.futures
import multiprocessing
import pickle
from collections.abc import Iterator
from concurrent.futures.process import BrokenProcessPool
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from _docling_stubs import DOCLING_MODULES as _DOCLING_MODULES
from _docling_stubs import DOCLING_OCR_TIMEOUT_S as _DOCLING_OCR_TIMEOUT_S
from _docling_stubs import docling_stubbed as _docling_stubbed
from _docling_stubs import reset_parser_worker_globals  # noqa: F401 — autouse fixture
from archon_search import parser as archon_parser
from archon_search.parser import DocumentParser, ParseError

# docling parsing runs in a recycled worker process (see the parse-worker docstrings in
# archon_search/parser.py). These unit tests keep it in-process via _docling_stubbed
# (tests/_docling_stubs.py, shared with tests/test_parser_ocr_memory.py — C1-B-5): it stubs
# every docling module the worker imports and swaps the pool for an inline executor.


# `reset_parser_worker_globals` is imported above as an autouse fixture: it resets BOTH
# `_worker_converter` and `_IS_PARSE_WORKER` around every test in this module. It lives in
# _docling_stubs.py so this file and test_parser_ocr_memory.py share one mechanism (C2-B-5).


@pytest.mark.asyncio
async def test_parser_md_returns_content(tmp_path: Path) -> None:
    f = tmp_path / "doc.md"
    f.write_text("# Hello\nworld")
    parser = DocumentParser()
    result = await parser.parse(f)
    assert "Hello" in result


@pytest.mark.asyncio
async def test_parser_txt_returns_content(tmp_path: Path) -> None:
    f = tmp_path / "doc.txt"
    f.write_text("plain text content")
    parser = DocumentParser()
    result = await parser.parse(f)
    assert "plain text content" in result


@pytest.mark.asyncio
async def test_parser_unknown_extension_falls_back_to_plain(tmp_path: Path) -> None:
    f = tmp_path / "doc.xyz"
    f.write_text("unknown ext content")
    parser = DocumentParser()
    result = await parser.parse(f)
    assert "unknown ext content" in result


@pytest.mark.asyncio
async def test_parser_html_calls_trafilatura(tmp_path: Path) -> None:
    f = tmp_path / "page.html"
    f.write_text("<html><body><p>hello</p></body></html>")
    parser = DocumentParser()
    mock_trafilatura = MagicMock()
    mock_trafilatura.extract.return_value = "extracted html"
    with patch.dict("sys.modules", {"trafilatura": mock_trafilatura}):
        result = await parser.parse(f)
    mock_trafilatura.extract.assert_called_once()
    assert result == "extracted html"


@pytest.mark.asyncio
async def test_parser_html_trafilatura_returns_none_falls_back(tmp_path: Path) -> None:
    """If trafilatura.extract returns None, fall back to plain read."""
    f = tmp_path / "page.html"
    f.write_text("<html><body>fallback</body></html>")
    parser = DocumentParser()
    mock_trafilatura = MagicMock()
    mock_trafilatura.extract.return_value = None
    with patch.dict("sys.modules", {"trafilatura": mock_trafilatura}):
        result = await parser.parse(f)
    assert "fallback" in result


@pytest.mark.asyncio
async def test_parser_pdf_calls_docling(tmp_path: Path) -> None:
    f = tmp_path / "doc.pdf"
    f.write_bytes(b"fake pdf")
    parser = DocumentParser()

    mock_converter = MagicMock()
    mock_converter.return_value.convert.return_value.document.export_to_markdown.return_value = (
        "# PDF content"
    )
    mock_docling = MagicMock()
    mock_docling.DocumentConverter = mock_converter

    with _docling_stubbed(mock_docling):
        result = await parser.parse(f)

    mock_converter.return_value.convert.assert_called_once()
    assert "PDF content" in result


@pytest.mark.asyncio
@pytest.mark.parametrize("ext", [".docx", ".pptx", ".xlsx"])
async def test_parser_office_calls_markitdown(ext: str, tmp_path: Path) -> None:
    f = tmp_path / f"doc{ext}"
    f.write_bytes(b"fake office doc")
    parser = DocumentParser()

    mock_md_instance = MagicMock()
    mock_md_instance.convert.return_value.text_content = "office content"
    mock_md_cls = MagicMock(return_value=mock_md_instance)
    mock_markitdown = MagicMock()
    mock_markitdown.MarkItDown = mock_md_cls

    with patch.dict("sys.modules", {"markitdown": mock_markitdown}):
        result = await parser.parse(f)

    mock_md_instance.convert.assert_called_once()
    assert result == "office content"


@pytest.mark.asyncio
async def test_parser_unreadable_raises_parse_error(tmp_path: Path) -> None:
    f = tmp_path / "unreadable.txt"
    f.write_text("data")
    f.chmod(0o000)  # remove read permission
    parser = DocumentParser()
    try:
        with pytest.raises(ParseError) as exc_info:
            await parser.parse(f)
        assert exc_info.value.path == f
        assert isinstance(exc_info.value.cause, Exception)
    finally:
        f.chmod(0o644)  # restore for cleanup


@pytest.mark.asyncio
async def test_parser_image_calls_docling(tmp_path: Path) -> None:
    f = tmp_path / "image.png"
    f.write_bytes(b"fake png")
    parser = DocumentParser()

    mock_converter = MagicMock()
    mock_converter.return_value.convert.return_value.document.export_to_markdown.return_value = "extracted text"
    mock_docling = MagicMock()
    mock_docling.DocumentConverter = mock_converter

    with _docling_stubbed(mock_docling):
        result = await parser.parse(f)

    mock_converter.return_value.convert.assert_called_once_with(str(f))
    assert result == "extracted text"


@pytest.mark.asyncio
async def test_parser_image_empty_ocr_returns_empty_string(tmp_path: Path) -> None:
    f = tmp_path / "image.jpg"
    f.write_bytes(b"fake jpg")
    parser = DocumentParser()

    mock_converter = MagicMock()
    mock_converter.return_value.convert.return_value.document.export_to_markdown.return_value = "   \n  "
    mock_docling = MagicMock()
    mock_docling.DocumentConverter = mock_converter

    with _docling_stubbed(mock_docling):
        result = await parser.parse(f)

    assert result == ""


@pytest.mark.asyncio
async def test_parser_image_none_ocr_returns_empty_string(tmp_path: Path) -> None:
    f = tmp_path / "image.jpeg"
    f.write_bytes(b"fake jpeg")
    parser = DocumentParser()

    mock_converter = MagicMock()
    mock_converter.return_value.convert.return_value.document.export_to_markdown.return_value = None
    mock_docling = MagicMock()
    mock_docling.DocumentConverter = mock_converter

    with _docling_stubbed(mock_docling):
        result = await parser.parse(f)

    assert result == ""


@pytest.mark.asyncio
async def test_parser_image_docling_failure_raises_parse_error(tmp_path: Path) -> None:
    f = tmp_path / "image.tiff"
    f.write_bytes(b"fake tiff")
    parser = DocumentParser()

    mock_converter = MagicMock()
    mock_converter.return_value.convert.side_effect = RuntimeError("ocr failed")
    mock_docling = MagicMock()
    mock_docling.DocumentConverter = mock_converter

    with _docling_stubbed(mock_docling):
        with pytest.raises(ParseError) as exc_info:
            await parser.parse(f)

    assert exc_info.value.path == f
    # `_parse_with_docling` always rebuilds the cause as RuntimeError(payload) (parser.py), so
    # `isinstance(cause, RuntimeError)` alone would hold for any original exception type —
    # assert the original type name is preserved in the message instead (mirrors the sibling
    # `ValueError` check in test_parser_image_corrupt_file_raises_parse_error below).
    assert "RuntimeError" in str(exc_info.value.cause)
    assert "ocr failed" in str(exc_info.value.cause)


@pytest.mark.asyncio
async def test_parser_image_corrupt_file_raises_parse_error(tmp_path: Path) -> None:
    f = tmp_path / "corrupt.png"
    f.write_bytes(b"")  # zero-byte file
    parser = DocumentParser()

    mock_converter = MagicMock()
    mock_converter.return_value.convert.side_effect = ValueError("invalid image")
    mock_docling = MagicMock()
    mock_docling.DocumentConverter = mock_converter

    with _docling_stubbed(mock_docling):
        with pytest.raises(ParseError) as exc_info:
            await parser.parse(f)

    assert exc_info.value.path == f
    # The worker reports its failure as text, not as an exception object: docling exceptions
    # do not reliably survive pickling and unpickling one would import docling in this
    # process. The original type and message must therefore both reach the caller.
    assert "ValueError" in str(exc_info.value.cause)
    assert "invalid image" in str(exc_info.value.cause)


@pytest.mark.asyncio
@pytest.mark.parametrize("ext", [".png", ".jpg", ".jpeg", ".tiff", ".tif", ".bmp", ".webp"])
async def test_parser_all_image_extensions_routed(ext: str, tmp_path: Path) -> None:
    f = tmp_path / f"image{ext}"
    f.write_bytes(b"fake image")
    parser = DocumentParser()

    with patch.object(parser, "_parse_image", return_value="image text") as mock_parse_image:
        result = await parser.parse(f)

    mock_parse_image.assert_called_once_with(f)
    assert result == "image text"


@pytest.mark.asyncio
async def test_parser_converter_reused_within_one_worker(tmp_path: Path) -> None:
    """Inside a parse worker the converter — and its loaded OCR models — is built once.

    Rebuilding it per file would reload the OCR models on every image (measured ~0.9 s each).
    The worker still exits after `max_tasks_per_child` files, which is what bounds memory.
    """
    f1 = tmp_path / "image1.png"
    f2 = tmp_path / "image2.png"
    f1.write_bytes(b"fake png 1")
    f2.write_bytes(b"fake png 2")
    parser = DocumentParser()

    mock_converter = MagicMock()
    mock_converter.return_value.convert.return_value.document.export_to_markdown.return_value = "text"
    mock_docling = MagicMock()
    mock_docling.DocumentConverter = mock_converter

    with _docling_stubbed(mock_docling):
        with patch("archon_search.parser._in_parse_worker", return_value=True):
            try:
                await parser.parse(f1)
                await parser.parse(f2)
            finally:
                archon_parser._worker_converter = None

    mock_converter.assert_called_once()


@pytest.mark.asyncio
async def test_parser_converter_not_cached_outside_a_worker(tmp_path: Path) -> None:
    """A caller that never exits must not keep a converter alive — that is the OOM bug.

    The cache belongs to the worker process, which exits and returns the native OCR memory
    to the OS; a converter cached in the server process would be retained forever.
    """
    f1 = tmp_path / "image1.png"
    f2 = tmp_path / "image2.png"
    f1.write_bytes(b"fake png 1")
    f2.write_bytes(b"fake png 2")
    parser = DocumentParser()

    mock_converter = MagicMock()
    mock_converter.return_value.convert.return_value.document.export_to_markdown.return_value = "text"
    mock_docling = MagicMock()
    mock_docling.DocumentConverter = mock_converter

    with _docling_stubbed(mock_docling):
        await parser.parse(f1)
        await parser.parse(f2)

    # mock_converter.call_count == 2 already proves a converter is rebuilt per call (not
    # cached); a direct `_worker_converter is None` check would be redundant with the autouse
    # `_reset_worker_converter` fixture and is dropped (C1 tests-hygiene follow-up).
    assert mock_converter.call_count == 2


@pytest.mark.asyncio
async def test_parser_pdf_none_ocr_returns_empty_string(tmp_path: Path) -> None:
    f = tmp_path / "doc.pdf"
    f.write_bytes(b"fake pdf")
    parser = DocumentParser()

    mock_converter = MagicMock()
    mock_converter.return_value.convert.return_value.document.export_to_markdown.return_value = None
    mock_docling = MagicMock()
    mock_docling.DocumentConverter = mock_converter

    with _docling_stubbed(mock_docling):
        result = await parser.parse(f)

    assert result == ""


@pytest.mark.asyncio
async def test_parser_pdf_whitespace_returns_empty_string(tmp_path: Path) -> None:
    f = tmp_path / "doc.pdf"
    f.write_bytes(b"fake pdf")
    parser = DocumentParser()

    mock_converter = MagicMock()
    mock_converter.return_value.convert.return_value.document.export_to_markdown.return_value = "  \n  "
    mock_docling = MagicMock()
    mock_docling.DocumentConverter = mock_converter

    with _docling_stubbed(mock_docling):
        result = await parser.parse(f)

    assert result == ""


# ---------------------------------------------------------------------------
# Task 1.2 — page_break_placeholder kwarg tests
# ---------------------------------------------------------------------------


def test_parse_with_docling_kwarg_passes_page_break_marker(
    tmp_path: Path,
) -> None:
    """Verify _parse_with_docling passes page_break_placeholder=PAGE_BREAK_MARKER to docling.

    Uses monkeypatch-style sys.modules patching to capture kwargs without real docling.
    Independent of fixture availability.
    """
    from archon_search.enricher import PAGE_BREAK_MARKER

    f = tmp_path / "doc.pdf"
    f.write_bytes(b"fake pdf")
    parser = DocumentParser()

    captured_kwargs: dict = {}

    mock_document = MagicMock()

    def capture_export(**kwargs: object) -> str:
        captured_kwargs.update(kwargs)
        return "page1 content"

    mock_document.export_to_markdown.side_effect = capture_export
    mock_converter = MagicMock()
    mock_converter.return_value.convert.return_value.document = mock_document
    mock_docling = MagicMock()
    mock_docling.DocumentConverter = mock_converter

    with _docling_stubbed(mock_docling):
        parser._parse_with_docling(f)

    assert "page_break_placeholder" in captured_kwargs, (
        f"Expected page_break_placeholder kwarg, got: {captured_kwargs}"
    )
    assert captured_kwargs["page_break_placeholder"] == PAGE_BREAK_MARKER, (
        f"Expected PAGE_BREAK_MARKER, got: {captured_kwargs['page_break_placeholder']!r}"
    )


# ---------------------------------------------------------------------------
# Parse-worker contract
# (Documentation/Backlog/2026-08-19-010-image-ocr-unbounded-memory-brief.md)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_parser_spawns_no_worker_until_a_docling_format_is_parsed(
    tmp_path: Path,
) -> None:
    """Constructing a parser, or parsing text, must never spawn a worker.

    A worker re-imports docling. Paying that at construction time would hit the server's
    lifespan startup, which must never await slow work, and every caller that only ever
    parses text.
    """
    f = tmp_path / "doc.md"
    f.write_text("# plain")

    with patch("archon_search.parser.ProcessPoolExecutor") as pool_cls:
        parser = DocumentParser()
        assert pool_cls.call_count == 0, "DocumentParser() eagerly created a parse pool"
        await parser.parse(f)
        assert pool_cls.call_count == 0, "parsing markdown created a parse pool"


def _child_reports_is_parse_worker(queue: multiprocessing.Queue) -> None:
    """Spawn-process target for test_parse_worker_flag_set_only_in_spawned_child.

    Module-level and picklable (the `spawn` context pickles the target); imports only
    archon_search.parser, no docling, so the test stays in the default lane.
    """
    archon_parser._worker_initializer()
    queue.put(archon_parser._in_parse_worker())


def test_parse_worker_flag_set_only_in_spawned_child() -> None:
    """`_in_parse_worker()` must read the explicit flag `_worker_initializer` sets: True only
    inside a process that ran it, never in the parent.

    Replaces the old `multiprocessing.parent_process() is not None` heuristic, which was true
    in ANY multiprocessing child — over-broad if this module were ever imported by some other
    worker pool (C1-I-11).
    """
    assert archon_parser._in_parse_worker() is False, "must be False in the parent process"

    ctx = multiprocessing.get_context("spawn")
    queue: multiprocessing.Queue = ctx.Queue()
    proc = ctx.Process(target=_child_reports_is_parse_worker, args=(queue,))
    proc.start()
    try:
        child_result = queue.get(timeout=30)
    finally:
        proc.join(timeout=30)
        if proc.is_alive():  # a wedged child would otherwise outlive this xdist worker
            proc.kill()
            proc.join(timeout=30)
    assert child_result is True, "expected _in_parse_worker() to read True inside the spawned child"
    # Without this, a child that crashed *after* queue.put would pass silently (C2-B-8).
    assert proc.exitcode == 0, f"spawned child exited uncleanly: exitcode={proc.exitcode!r}"


@pytest.mark.parametrize(
    ("bad_value", "expected_exc"),
    [
        (0, ValueError),
        (-1, ValueError),
        (2.5, TypeError),
        ("25", TypeError),
        (True, TypeError),
    ],
    ids=["zero", "negative", "float", "string", "bool"],
)
def test_document_parser_rejects_invalid_max_tasks_per_child(
    bad_value: object, expected_exc: type[Exception]
) -> None:
    """Fails at construction, not at the first PDF/image parse (possibly hours later): the pool
    itself is lazy, and ProcessPoolExecutor only validates once it is finally built.

    Both of its checks are mirrored, hence the split expectation: it raises TypeError for a
    non-int and ValueError for <= 0. A float that only failed at the first parse would be the
    exact deferred failure this guard removes. `True` is rejected because bool subclasses int.
    """
    with pytest.raises(expected_exc, match="max_tasks_per_child"):
        DocumentParser(max_tasks_per_child=bad_value)  # type: ignore[arg-type]


def test_watchdog_returns_when_there_is_no_parent(monkeypatch: pytest.MonkeyPatch) -> None:
    """No parent process means we are not a parse worker, so the watchdog must simply return.

    Without the guard, `_watch_for_orphaning` falls straight through to `os._exit(1)` and kills
    its caller with no traceback, no atexit and no pytest teardown. That is reachable from the
    test seam: teaching the inline executor stub to honour `initializer` — the obvious way to
    make it match the real executor — would run this inside the pytest process, where
    `parent_process()` is None under xdist, and an xdist worker would silently vanish.
    """
    monkeypatch.setattr(archon_parser.multiprocessing, "parent_process", lambda: None)
    monkeypatch.setattr(
        archon_parser.os, "_exit", lambda code: pytest.fail(f"os._exit({code}) with no parent")
    )

    archon_parser._watch_for_orphaning()  # must return, not exit


def test_watchdog_exits_when_the_parent_dies(monkeypatch: pytest.MonkeyPatch) -> None:
    """A dead parent means an orphaned ~1 GB worker, so the watchdog must exit the process."""
    monkeypatch.setattr(
        archon_parser.multiprocessing, "parent_process", lambda: SimpleNamespace(is_alive=lambda: False)
    )
    exits: list[int] = []
    monkeypatch.setattr(archon_parser.os, "_exit", exits.append)

    archon_parser._watch_for_orphaning()

    assert exits == [1], f"expected exactly one os._exit(1), got {exits!r}"


def test_worker_initializer_starts_the_watchdog_as_a_daemon_thread(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The flag alone is not enough — the initializer must also start the watchdog.

    Daemon is load-bearing: a non-daemon thread would block the worker's own clean exit at
    every `max_tasks_per_child` recycle, since `threading._shutdown()` joins non-daemon threads.
    """
    started: list[MagicMock] = []

    def _fake_thread(*args: object, **kwargs: object) -> MagicMock:
        thread = MagicMock()
        thread.kwargs = kwargs
        started.append(thread)
        return thread

    monkeypatch.setattr(archon_parser.threading, "Thread", _fake_thread)
    monkeypatch.setattr(archon_parser, "_IS_PARSE_WORKER", False)

    archon_parser._worker_initializer()

    assert archon_parser._IS_PARSE_WORKER is True, "initializer must set the parse-worker flag"
    assert len(started) == 1, f"expected exactly one watchdog thread, got {len(started)}"
    assert started[0].kwargs.get("target") is archon_parser._watch_for_orphaning
    assert started[0].kwargs.get("daemon") is True, (
        "the watchdog thread must be a daemon, or it blocks the worker's clean exit on every "
        "max_tasks_per_child recycle"
    )
    started[0].start.assert_called_once()


def test_parse_error_is_not_picklable() -> None:
    """Pins the trap `_docling_to_markdown`'s (ok, payload) contract exists to route around:
    `Exception.__reduce__` replays the single formatted `args` string against ParseError's
    two-argument `__init__`, so a ParseError cannot survive a pickle round-trip.

    Split out from the worker-contract test below (C1-B-14) so a future change to
    `ParseError.__reduce__` (e.g. adding one to fix this) fails only this narrowly-scoped
    test, not a combined test that is also asserting unrelated worker behaviour.
    """
    unpicklable = ParseError(Path("/tmp/x.png"), RuntimeError("boom"))
    with pytest.raises(TypeError):
        pickle.loads(pickle.dumps(unpicklable))


def test_parse_worker_failure_result_survives_the_process_boundary() -> None:
    """A failure inside the worker must come back as picklable data, not as an exception.

    Because `ParseError` cannot be unpickled (see `test_parse_error_is_not_picklable`), and a
    docling exception could only be unpickled by importing docling in the caller, the worker
    must return `(ok, payload)` — plain, always-picklable data — instead of letting either
    exception cross the process boundary. Either would turn a parse failure into a broken pool
    or a 40 s import in the server process.
    """
    unpicklable = ParseError(Path("/tmp/x.png"), RuntimeError("boom"))
    mock_converter = MagicMock()
    mock_converter.return_value.convert.side_effect = unpicklable
    mock_docling = MagicMock()
    mock_docling.DocumentConverter = mock_converter

    with patch.dict("sys.modules", dict.fromkeys(_DOCLING_MODULES, mock_docling)):
        result = archon_parser._docling_to_markdown("/tmp/x.png")

    ok, payload = pickle.loads(pickle.dumps(result))  # round-trips, unlike the exception itself
    assert ok is False
    assert "ParseError" in payload and "boom" in payload, (
        f"the worker dropped the original failure information: {payload!r}"
    )


@pytest.mark.asyncio
async def test_parse_worker_failure_surfaces_as_parse_error_with_path(tmp_path: Path) -> None:
    """The caller still gets a ParseError naming the file and the original failure."""
    f = tmp_path / "image.png"
    f.write_bytes(b"fake png")

    mock_converter = MagicMock()
    mock_converter.return_value.convert.side_effect = ParseError(f, RuntimeError("boom"))
    mock_docling = MagicMock()
    mock_docling.DocumentConverter = mock_converter

    with _docling_stubbed(mock_docling):
        with pytest.raises(ParseError) as exc_info:
            await DocumentParser().parse(f)

    assert exc_info.value.path == f
    assert "boom" in str(exc_info.value.cause)


class _BrokenThenOkExecutor:
    """Stands in for ProcessPoolExecutor: the first constructed instance's submit() raises
    BrokenProcessPool; every later instance runs the submitted task inline. Used to exercise
    DocumentParser's broken-pool recovery path (C1-B-7 / C1-I-6).

    No `shutdown()` tracking: `_terminate_broken` (concurrent/futures/process.py) has already
    terminated and joined the real executor's workers before BrokenProcessPool reaches the
    caller, so `_parse_with_docling` does not call `shutdown()` on it — only drops the reference.
    """

    instances: list["_BrokenThenOkExecutor"] = []

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        # Capture position at construction. Branching on len(instances) instead would mean
        # "no pool has been constructed after me", not "I am the first" — so the broken
        # instance would start succeeding the moment a replacement existed (C2-T-12).
        self._index = len(type(self).instances)
        type(self).instances.append(self)

    def submit(self, fn, /, *args: Any, **kwargs: Any) -> concurrent.futures.Future:
        future: concurrent.futures.Future = concurrent.futures.Future()
        if self._index == 0:
            future.set_exception(BrokenProcessPool("worker died (e.g. killed by the OOM killer)"))
        else:
            try:
                future.set_result(fn(*args, **kwargs))
            except BaseException as exc:  # noqa: BLE001 — mirror the real executor's contract
                future.set_exception(exc)
        return future

    def shutdown(self, *args: Any, **kwargs: Any) -> None:
        return None


@pytest.mark.asyncio
async def test_parser_recovers_after_broken_process_pool(tmp_path: Path) -> None:
    """A BrokenProcessPool must drop the broken pool, and the next parse must get a fresh one
    rather than reusing (or leaking) the dead one."""
    f = tmp_path / "image.png"
    f.write_bytes(b"fake png")

    mock_docling = MagicMock()
    mock_docling.DocumentConverter.return_value.convert.return_value.document.export_to_markdown.return_value = "ok"

    _BrokenThenOkExecutor.instances = []
    with patch.dict("sys.modules", dict.fromkeys(_DOCLING_MODULES, mock_docling)):
        with patch("archon_search.parser.ProcessPoolExecutor", _BrokenThenOkExecutor):
            parser = DocumentParser()

            with pytest.raises(ParseError) as exc_info:
                await parser.parse(f)
            assert exc_info.value.path == f, "the first failure must surface as a ParseError carrying the path"
            # The raw BrokenProcessPool text must NOT reach ParseError.cause (it flows into
            # IngestResult.error, which is MCP-visible) — a sanitized detail replaces it, while
            # the original exception is still chained onto __cause__ for logs.
            assert isinstance(exc_info.value.cause, RuntimeError) and not isinstance(
                exc_info.value.cause, BrokenProcessPool
            ), f"expected a sanitized RuntimeError cause, got {type(exc_info.value.cause).__name__}"
            assert str(exc_info.value.cause) == archon_parser._PARSE_WORKER_LOST_DETAIL
            assert isinstance(exc_info.value.__cause__, BrokenProcessPool), (
                "the original BrokenProcessPool must still be chained onto __cause__ for logs"
            )
            assert len(_BrokenThenOkExecutor.instances) == 1, (
                f"expected exactly one pool constructed so far, "
                f"got {len(_BrokenThenOkExecutor.instances)}"
            )

            text = await parser.parse(f)
            assert text == "ok"
            assert len(_BrokenThenOkExecutor.instances) == 2, (
                "expected a fresh pool to be constructed for the next parse after recovery "
                f"(construction count 1 -> 2), got {len(_BrokenThenOkExecutor.instances)}"
            )


@pytest.mark.integration
@pytest.mark.docling
@pytest.mark.xdist_group("docling")
def test_parse_with_docling_emits_page_marker(substantial_three_page_pdf: Path) -> None:
    """Integration: parser output from a substantial three-page PDF contains at least one PAGE_BREAK_MARKER.

    Uses the substantial_three_page_pdf fixture (paragraph-rich content per page).
    docling is a hard dependency (pyproject.toml:19); a broken docling install fails this
    test loudly instead of skipping (C1-I-4) — no env-var opt-in.

    NOTE: Docling may emit 1 or 2 page break markers depending on its segmentation
    heuristics. We assert >= 1 (at least one page boundary detected) rather than == 2.
    """
    import concurrent.futures

    from archon_search.enricher import PAGE_BREAK_MARKER
    from archon_search.parser import ParseError

    parser = DocumentParser()
    executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    future = executor.submit(parser._parse_with_docling, substantial_three_page_pdf)
    try:
        result = future.result(timeout=_DOCLING_OCR_TIMEOUT_S)
    except concurrent.futures.TimeoutError:
        executor.shutdown(wait=False, cancel_futures=True)
        pytest.fail(
            f"docling OCR exceeded {_DOCLING_OCR_TIMEOUT_S}s. If this is a first run, the "
            "RapidOCR/ONNX weights are still downloading — re-run once the model cache is warm."
        )
    except ParseError as exc:
        executor.shutdown(wait=False)
        pytest.fail(f"docling not functional in this environment: {exc}")
    executor.shutdown(wait=False)

    marker_count = result.count(PAGE_BREAK_MARKER)
    assert marker_count >= 1, (
        f"Expected at least 1 occurrence of PAGE_BREAK_MARKER in parsed output "
        f"from three-page PDF, got {marker_count}.\nParsed output:\n{result[:500]}"
    )


_OCR_FIXTURE_WORDS = ("ARCHON", "SEARCH")


@pytest.mark.asyncio
@pytest.mark.integration
@pytest.mark.docling
@pytest.mark.xdist_group("docling")
async def test_image_ocr_still_reads_text_at_scale_one(tmp_path: Path) -> None:
    """OCR text is unchanged on a text-bearing image now that docling no longer upscales 3x.

    Brief Verification (line 117-118): dropping OcrOptions.scale to 1.0 bounds memory, and it
    must not cost recognition on a normal screenshot. The font is sized relative to the image
    so the fixture carries text a screenshot would carry — PIL's default bitmap font is a few
    pixels tall at 512px and OCRs to nothing regardless of scale.

    docling is a hard dependency and PIL/Pillow is pulled in transitively by docling-core /
    docling-ibm-models / docling-parse (uv.lock), so both are always present in any correctly
    installed environment; a broken install fails this test loudly instead of skipping
    (C1-I-4) — no env-var opt-in.
    """
    from PIL import Image, ImageDraw, ImageFont

    size = 512
    image = tmp_path / "screenshot.png"
    canvas = Image.new("RGB", (size, size), (255, 255, 255))
    draw = ImageDraw.Draw(canvas)
    draw.text(
        (size // 12, size // 3),
        " ".join(_OCR_FIXTURE_WORDS),
        fill=(0, 0, 0),
        font=ImageFont.load_default(size=size // 10),
    )
    canvas.save(image)

    parser = DocumentParser()
    try:
        text = await asyncio.wait_for(parser.parse(image), timeout=_DOCLING_OCR_TIMEOUT_S)
    except TimeoutError:
        pytest.fail(
            f"docling OCR exceeded {_DOCLING_OCR_TIMEOUT_S}s. If this is a first run, the "
            "RapidOCR/ONNX weights are still downloading — re-run once the model cache is warm."
        )
    except ParseError as exc:
        pytest.fail(f"docling not functional in this environment: {exc}")

    recognised = text.upper()
    for word in _OCR_FIXTURE_WORDS:
        assert word in recognised, (
            f"OCR at scale {archon_parser._OCR_SCALE} lost {word!r} from a text-bearing "
            f"image; recognised text was {text!r}"
        )


@pytest.mark.asyncio
@pytest.mark.integration
@pytest.mark.docling
@pytest.mark.xdist_group("docling")
async def test_pdf_ocr_reads_text_from_scanned_page(scanned_pdf: Path) -> None:
    """A scanned (image-only) PDF page must still yield its text via docling's OCR path.

    `test_parse_with_docling_emits_page_marker` above uses `substantial_three_page_pdf`, which
    has a real PDF text layer docling extracts directly — OCR contributes nothing to that test.
    This is the PDF-side counterpart to `test_image_ocr_still_reads_text_at_scale_one`: the
    `scanned_pdf` fixture (`tests/_pdf_fixture.py::generate_scanned_pdf`) embeds only a rendered
    bitmap with no text layer, so text can only come back if docling actually ran OCR over the
    PDF page. Note this does NOT guard the PDF OCR scale: the fixture's glyphs render ~66 px
    even at scale 1.0, so it passes either way. The PDF-scale guard is
    test_build_docling_converter_effective_scale_is_one_image_only_default_pdf.

    docling is a hard dependency; a broken install fails this test loudly instead of skipping
    (C1-I-4) — no env-var opt-in. One caveat, so this claim is not read wider than it is: the
    `scanned_pdf` fixture itself still `importorskip`s reportlab/PIL, matching its sibling PDF
    fixtures. That guards *fixture generation* — without reportlab there is nothing to OCR —
    which is a different situation from docling half-working, the case C1-I-4 was about.
    """
    parser = DocumentParser()
    try:
        text = await asyncio.wait_for(parser.parse(scanned_pdf), timeout=_DOCLING_OCR_TIMEOUT_S)
    except TimeoutError:
        pytest.fail(
            f"docling OCR exceeded {_DOCLING_OCR_TIMEOUT_S}s. If this is a first run, the "
            "RapidOCR/ONNX weights are still downloading — re-run once the model cache is warm."
        )
    except ParseError as exc:
        pytest.fail(f"docling not functional in this environment: {exc}")

    recognised = text.upper()
    for word in _OCR_FIXTURE_WORDS:
        assert word in recognised, (
            f"PDF OCR lost {word!r} from a scanned (image-only) page; "
            f"recognised text was {text!r}"
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("ext", [".xls", ".rtf", ".epub", ".eml", ".msg"])
async def test_parser_office_new_extensions_routed(ext: str, tmp_path: Path) -> None:
    """All supported new Office extensions must route to _parse_office and return content."""
    f = tmp_path / f"doc{ext}"
    f.write_bytes(b"fake office doc")
    parser = DocumentParser()

    mock_md_instance = MagicMock()
    mock_md_instance.convert.return_value.text_content = "office content"
    mock_md_cls = MagicMock(return_value=mock_md_instance)
    mock_markitdown = MagicMock()
    mock_markitdown.MarkItDown = mock_md_cls

    with patch.dict("sys.modules", {"markitdown": mock_markitdown}):
        with patch.object(parser, "_parse_office", wraps=parser._parse_office) as mock_office:
            result = await parser.parse(f)

    mock_office.assert_called_once_with(f)
    mock_md_instance.convert.assert_called_once()
    assert result == "office content"


@pytest.mark.asyncio
async def test_parser_tsv_routed_to_plain(tmp_path: Path) -> None:
    """TSV files route to _parse_plain; no markitdown call."""
    f = tmp_path / "data.tsv"
    f.write_text("col1\tcol2\nval1\tval2")
    parser = DocumentParser()

    with patch.object(parser, "_parse_plain", wraps=parser._parse_plain) as mock_plain:
        result = await parser.parse(f)

    mock_plain.assert_called_once_with(f)
    assert "col1" in result
    assert "col2" in result


@pytest.mark.asyncio
async def test_parser_office_import_error_surfaces_message(tmp_path: Path) -> None:
    """When markitdown is absent, ParseError.cause must mention 'markitdown'."""
    f = tmp_path / "doc.docx"
    f.write_bytes(b"fake docx")
    parser = DocumentParser()

    with patch.dict("sys.modules", {"markitdown": None}):
        with pytest.raises(ParseError) as exc_info:
            await parser.parse(f)

    assert "markitdown" in str(exc_info.value.cause).lower()


@pytest.mark.asyncio
async def test_parser_office_malformed_file_raises_parse_error(tmp_path: Path) -> None:
    """If MarkItDown().convert() raises, ParseError is raised (not ImportError)."""
    f = tmp_path / "doc.docx"
    f.write_bytes(b"truncated")
    parser = DocumentParser()

    mock_md_instance = MagicMock()
    mock_md_instance.convert.side_effect = RuntimeError("conversion failed")
    mock_md_cls = MagicMock(return_value=mock_md_instance)
    mock_markitdown = MagicMock()
    mock_markitdown.MarkItDown = mock_md_cls

    with patch.dict("sys.modules", {"markitdown": mock_markitdown}):
        with pytest.raises(ParseError) as exc_info:
            await parser.parse(f)

    assert exc_info.value.path == f
    assert isinstance(exc_info.value.cause, RuntimeError)


@pytest.mark.asyncio
async def test_parser_office_none_content_returns_empty_string(tmp_path: Path) -> None:
    """When markitdown text_content is None, parse() must return '' not raise TypeError."""
    f = tmp_path / "doc.docx"
    f.write_bytes(b"fake docx")
    parser = DocumentParser()

    mock_md_instance = MagicMock()
    mock_md_instance.convert.return_value.text_content = None
    mock_md_cls = MagicMock(return_value=mock_md_instance)
    mock_markitdown = MagicMock()
    mock_markitdown.MarkItDown = mock_md_cls

    with patch.dict("sys.modules", {"markitdown": mock_markitdown}):
        result = await parser.parse(f)

    assert result == ""


@pytest.mark.asyncio
async def test_parser_parse_error_has_path_and_cause(tmp_path: Path) -> None:
    f = tmp_path / "doc.pdf"
    f.write_bytes(b"bad pdf")
    parser = DocumentParser()

    mock_converter = MagicMock()
    mock_converter.return_value.convert.side_effect = RuntimeError("corrupt pdf")
    mock_docling = MagicMock()
    mock_docling.DocumentConverter = mock_converter

    with _docling_stubbed(mock_docling):
        with pytest.raises(ParseError) as exc_info:
            await parser.parse(f)
    assert exc_info.value.path == f
    assert "corrupt pdf" in str(exc_info.value.cause)
