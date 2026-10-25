"""Parser hardening (SecReview #7b, #7c, #3): a bounded docling parse that recovers, zip bounds, regular files only."""
from __future__ import annotations

import asyncio
import os
import time
import zipfile
from concurrent.futures import Future
from pathlib import Path

import pytest

from archon_search import parser as parser_module
from archon_search.config import IngestConfig
from archon_search.parser import (
    NOT_REGULAR_FILE_DETAIL,
    PARSE_TIMEOUT_DETAIL,
    ZIP_LIMIT_DETAIL,
    DocumentParser,
    ParseError,
)

_TIMEOUT_S = 0.3
_WAIT_BOUND_S = 10.0
_MIB = 1024 * 1024
_PARSED_TEXT = "parsed text"


def _echo_worker(path: str) -> tuple[bool, str]:
    """Module level so a spawned worker can import it."""
    return True, f"echo:{Path(path).name}"


def _block_forever_worker(path: str) -> tuple[bool, str]:
    time.sleep(3600)
    return True, path


class _FakeProcess:
    def __init__(self) -> None:
        self.terminated = False

    def terminate(self) -> None:
        self.terminated = True


class _FakePool:
    """A stand-in for ``ProcessPoolExecutor``: ``submit`` returns a future that stays pending unless it is told to."""

    def __init__(self, *, result: tuple[bool, str] | None = None) -> None:
        self._result = result
        self._processes = {1: _FakeProcess()}
        self.shutdown_calls: list[dict[str, object]] = []

    def submit(self, _fn: object, _arg: str) -> Future:
        future: Future = Future()
        if self._result is not None:
            future.set_result(self._result)
        return future

    def shutdown(self, **kwargs: object) -> None:
        self.shutdown_calls.append(kwargs)


def _pdf(tmp_path: Path) -> Path:
    path = tmp_path / "doc.pdf"
    path.write_bytes(b"%PDF-1.4 fake")
    return path


# ---------------------------------------------------------------------------
# #7c — a hung docling parse fails that one document and the pool recovers
# ---------------------------------------------------------------------------


def test_a_hung_parse_times_out_with_a_sanitized_error_and_drops_the_pool(tmp_path: Path) -> None:
    parser = DocumentParser(parse_timeout_seconds=_TIMEOUT_S)
    hung = _FakePool()
    parser._pool = hung  # type: ignore[assignment]
    started = time.monotonic()

    with pytest.raises(ParseError) as raised:
        parser._parse_with_docling(_pdf(tmp_path))

    assert time.monotonic() - started < _WAIT_BOUND_S
    assert raised.value.detail == PARSE_TIMEOUT_DETAIL
    assert str(tmp_path) not in raised.value.detail
    assert parser._pool is None
    assert hung.shutdown_calls == [{"wait": False, "cancel_futures": True}]
    assert hung._processes[1].terminated, "the wedged worker process must be killed, not left running"


def test_the_parser_serves_the_next_document_after_a_timeout(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    parser = DocumentParser(parse_timeout_seconds=_TIMEOUT_S)
    parser._pool = _FakePool()  # type: ignore[assignment]
    healthy = _FakePool(result=(True, _PARSED_TEXT))
    monkeypatch.setattr(parser_module, "ProcessPoolExecutor", lambda **_kwargs: healthy)

    with pytest.raises(ParseError):
        parser._parse_with_docling(_pdf(tmp_path))
    text = parser._parse_with_docling(_pdf(tmp_path))

    assert text == _PARSED_TEXT
    assert parser._pool is healthy


async def test_a_timed_out_pdf_does_not_wedge_concurrent_ingests(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The async entry point reports the timeout as a ParseError and parses the next file normally."""
    parser = DocumentParser(parse_timeout_seconds=_TIMEOUT_S)
    parser._pool = _FakePool()  # type: ignore[assignment]
    monkeypatch.setattr(parser_module, "ProcessPoolExecutor", lambda **_kwargs: _FakePool(result=(True, _PARSED_TEXT)))

    with pytest.raises(ParseError) as raised:
        await asyncio.wait_for(parser.parse(_pdf(tmp_path)), _WAIT_BOUND_S)
    assert raised.value.detail == PARSE_TIMEOUT_DETAIL

    assert await asyncio.wait_for(parser.parse(_pdf(tmp_path)), _WAIT_BOUND_S) == _PARSED_TEXT


def test_a_real_worker_process_that_hangs_is_killed_and_replaced(tmp_path: Path) -> None:
    """Real spawned worker: the hung task is abandoned, its process dies, and the next task runs in a fresh one."""
    parser = DocumentParser(parse_timeout_seconds=1.0)
    document = _pdf(tmp_path)
    try:
        # Warm the pool so the timeout below measures the hung task, not the spawn.
        assert parser._run_in_worker(_echo_worker, document) == f"echo:{document.name}"
        hung_pool = parser._pool
        assert hung_pool is not None
        workers = list(hung_pool._processes.values())

        with pytest.raises(ParseError) as raised:
            parser._run_in_worker(_block_forever_worker, document)

        assert raised.value.detail == PARSE_TIMEOUT_DETAIL
        assert parser._pool is None
        for worker in workers:
            worker.join(timeout=_WAIT_BOUND_S)
            assert not worker.is_alive()
        assert parser._run_in_worker(_echo_worker, document) == f"echo:{document.name}"
        assert parser._pool is not hung_pool
    finally:
        if parser._pool is not None:
            parser._pool.shutdown(wait=True, cancel_futures=True)


# ---------------------------------------------------------------------------
# #7b — zip-based Office formats are bounded on their central directory
# ---------------------------------------------------------------------------

_ZIP_SUFFIXES = [".docx", ".pptx", ".xlsx", ".epub"]


def _zip(path: Path, entries: dict[str, bytes]) -> Path:
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, payload in entries.items():
            archive.writestr(name, payload)
    return path


class _FakeMarkItDown:
    def convert(self, _path: str) -> object:
        return type("Converted", (), {"text_content": _PARSED_TEXT})()


@pytest.fixture
def fake_markitdown(monkeypatch: pytest.MonkeyPatch) -> None:
    import markitdown

    monkeypatch.setattr(markitdown, "MarkItDown", _FakeMarkItDown)


@pytest.mark.parametrize("suffix", _ZIP_SUFFIXES)
def test_a_zip_bomb_is_refused_before_markitdown_opens_it(tmp_path: Path, suffix: str, fake_markitdown: None) -> None:
    bomb = _zip(tmp_path / f"bomb{suffix}", {"word/document.xml": b"\0" * (4 * _MIB)})
    assert bomb.stat().st_size < _MIB, "the fixture must be a real bomb: tiny on disk, large when inflated"
    parser = DocumentParser(max_zip_uncompressed_mb=1)

    with pytest.raises(ParseError) as raised:
        parser._parse_office(bomb)

    assert raised.value.detail == ZIP_LIMIT_DETAIL


@pytest.mark.parametrize("suffix", _ZIP_SUFFIXES)
def test_a_zip_with_too_many_entries_is_refused(tmp_path: Path, suffix: str, fake_markitdown: None) -> None:
    many = _zip(tmp_path / f"many{suffix}", {f"part{i}.xml": b"x" for i in range(11)})
    parser = DocumentParser(max_zip_entries=10)

    with pytest.raises(ParseError) as raised:
        parser._parse_office(many)

    assert raised.value.detail == ZIP_LIMIT_DETAIL


def test_an_ordinary_office_zip_is_handed_to_markitdown(tmp_path: Path, fake_markitdown: None) -> None:
    document = _zip(tmp_path / "ok.docx", {"word/document.xml": b"<w:t>hello</w:t>" * 100, "[Content_Types].xml": b"<x/>"})

    assert DocumentParser(max_zip_entries=10, max_zip_uncompressed_mb=1)._parse_office(document) == _PARSED_TEXT


def test_a_non_zip_office_file_is_left_to_markitdown(tmp_path: Path, fake_markitdown: None) -> None:
    rtf = tmp_path / "letter.rtf"
    rtf.write_text(r"{\rtf1 hello}", encoding="utf-8")

    assert DocumentParser()._parse_office(rtf) == _PARSED_TEXT


def test_the_limits_come_from_the_ingest_config() -> None:
    config = IngestConfig(
        parse_timeout_seconds=12.5, parse_timeout_seconds_per_mb=3.5,
        max_zip_entries=7, max_zip_uncompressed_mb=3, max_tasks_per_child=4,
    )

    parser = DocumentParser.from_config(config)

    assert parser._parse_timeout_seconds == 12.5
    assert parser._parse_timeout_seconds_per_mb == 3.5
    assert parser._max_zip_entries == 7
    assert parser._max_zip_uncompressed_bytes == 3 * _MIB
    assert parser._max_tasks_per_child == 4


# ---------------------------------------------------------------------------
# M6 — the conversion bound grows with the file: a big scanned PDF is slow, not wedged
# ---------------------------------------------------------------------------

_BASE_TIMEOUT_S = 10.0
_PER_MIB_S = 2.0


class _TimeoutRecordingFuture(Future):
    """A completed future that records the ``timeout`` its caller waited with."""

    def __init__(self, result: tuple[bool, str], waits: list[float | None]) -> None:
        super().__init__()
        self.set_result(result)
        self._waits = waits

    def result(self, timeout: float | None = None) -> object:
        self._waits.append(timeout)
        return super().result(timeout)


class _TimeoutRecordingPool:
    def __init__(self) -> None:
        self.waits: list[float | None] = []

    def submit(self, _fn: object, _arg: str) -> Future:
        return _TimeoutRecordingFuture((True, _PARSED_TEXT), self.waits)


def _parser_with_recording_pool(per_mib: float) -> tuple[DocumentParser, _TimeoutRecordingPool]:
    parser = DocumentParser(parse_timeout_seconds=_BASE_TIMEOUT_S, parse_timeout_seconds_per_mb=per_mib)
    pool = _TimeoutRecordingPool()
    parser._pool = pool  # type: ignore[assignment]
    return parser, pool


def _pdf_of(tmp_path: Path, mib: int) -> Path:
    path = tmp_path / f"{mib}mib.pdf"
    with path.open("wb") as handle:
        handle.truncate(mib * _MIB)
    return path


@pytest.mark.parametrize("mib", [1, 3, 40])
def test_the_parse_timeout_is_the_base_plus_an_allowance_per_mib_of_the_file(tmp_path: Path, mib: int) -> None:
    parser, pool = _parser_with_recording_pool(_PER_MIB_S)

    parser._parse_with_docling(_pdf_of(tmp_path, mib))

    assert pool.waits == [_BASE_TIMEOUT_S + _PER_MIB_S * mib]


def test_a_zero_allowance_keeps_the_timeout_at_its_base(tmp_path: Path) -> None:
    parser, pool = _parser_with_recording_pool(0.0)

    parser._parse_with_docling(_pdf_of(tmp_path, 40))

    assert pool.waits == [_BASE_TIMEOUT_S]


def test_a_file_that_cannot_be_measured_gets_the_base_timeout(tmp_path: Path) -> None:
    parser, pool = _parser_with_recording_pool(_PER_MIB_S)

    parser._run_in_worker(lambda _path: (True, _PARSED_TEXT), tmp_path / "vanished.pdf")

    assert pool.waits == [_BASE_TIMEOUT_S]


def test_a_timed_out_big_parse_logs_the_bound_it_actually_had(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    parser = DocumentParser(parse_timeout_seconds=_TIMEOUT_S, parse_timeout_seconds_per_mb=_PER_MIB_S)
    parser._pool = _FakePool()  # type: ignore[assignment]
    big = tmp_path / "big.pdf"
    big.write_bytes(b"%PDF-1.4 fake")

    with caplog.at_level("WARNING", logger="archon_search.parser"), pytest.raises(ParseError):
        parser._parse_with_docling(big)

    assert any("exceeded" in record.getMessage() for record in caplog.records)


# ---------------------------------------------------------------------------
# #3 — only regular files are read: a FIFO or device cannot hang or leak the server
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("suffix", [".md", ".html", ".pdf", ".docx", ""])
async def test_a_fifo_is_refused_instead_of_blocking_the_parser(tmp_path: Path, suffix: str) -> None:
    fifo = tmp_path / f"pipe{suffix}"
    os.mkfifo(fifo)

    with pytest.raises(ParseError) as raised:
        await asyncio.wait_for(DocumentParser().parse(fifo), _WAIT_BOUND_S)

    assert raised.value.detail == NOT_REGULAR_FILE_DETAIL


async def test_a_device_file_is_refused() -> None:
    with pytest.raises(ParseError) as raised:
        await asyncio.wait_for(DocumentParser().parse(Path("/dev/zero")), _WAIT_BOUND_S)

    assert raised.value.detail == NOT_REGULAR_FILE_DETAIL


async def test_a_symlink_is_not_followed(tmp_path: Path) -> None:
    target = tmp_path / "target.md"
    target.write_text("secret", encoding="utf-8")
    link = tmp_path / "link.md"
    link.symlink_to(target)

    with pytest.raises(ParseError) as raised:
        await DocumentParser().parse(link)

    assert raised.value.detail == NOT_REGULAR_FILE_DETAIL


async def test_a_regular_text_file_still_parses(tmp_path: Path) -> None:
    note = tmp_path / "note.md"
    note.write_text("héllo", encoding="utf-8")

    assert await DocumentParser().parse(note) == "héllo"
