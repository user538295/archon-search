"""tests/_docling_stubs.py — shared docling-module stubbing helper for parser unit tests.

``archon_search.parser`` imports these modules only inside the recycled parse worker (see
``parser.py::_build_docling_converter``). Unit tests stub them all to the same ``MagicMock`` so
the docling seam never imports the real package, and swap the parse pool for an inline executor
that runs the submitted work in the calling process instead of spawning a worker.

Factored out of ``tests/test_parser.py`` and ``tests/test_parser_ocr_memory.py`` (C1-B-5 /
C1-I-13): both files carried their own copy of the module tuple, and the copies had already
drifted (one carried an extra, unimported module). Keep this file to a tuple plus a
contextmanager — if it needs to grow further, prefer duplication over a bigger shared module.
"""
from __future__ import annotations

import concurrent.futures
import contextlib
import sys
from collections.abc import Iterator
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from archon_search import parser as archon_parser

# The exact modules archon_search.parser imports to build a docling converter
# (parser.py::_build_docling_converter). Not docling.datamodel.base_models — parser.py never
# imports it.
# Single owner for the docling-lane timeout. Previously each test module declared its own 300
# and cited the OTHER as the source, so neither was the authority and neither could be changed
# with confidence (C2-B-7). Test-only concern, so it lives here rather than in the production
# archon_search.constants.
DOCLING_OCR_TIMEOUT_S = 300

DOCLING_MODULES = (
    "docling",
    "docling.document_converter",
    "docling.datamodel",
    "docling.datamodel.pipeline_options",
)


class InlineExecutor:
    """Stands in for ProcessPoolExecutor: records each construction's args/kwargs and runs
    submitted work inline, in the calling process — never spawns a real worker that would
    import real docling."""

    calls: list[tuple[tuple[Any, ...], dict[str, Any]]] = []

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        type(self).calls.append((args, kwargs))

    def submit(self, fn, /, *args: Any, **kwargs: Any) -> concurrent.futures.Future:
        future: concurrent.futures.Future = concurrent.futures.Future()
        try:
            future.set_result(fn(*args, **kwargs))
        except BaseException as exc:  # noqa: BLE001 — mirror the real executor's contract
            future.set_exception(exc)
        return future

    def shutdown(self, *args: Any, **kwargs: Any) -> None:
        return None

    def __enter__(self) -> InlineExecutor:
        return self

    def __exit__(self, *args: Any) -> bool:
        return False


@pytest.fixture(autouse=True)
def reset_parser_worker_globals() -> Iterator[None]:
    """Reset ``archon_search.parser``'s two worker-scoped module globals around every test.

    Import this into any module that drives the docling seam and pytest registers it as
    autouse there. It lives here, beside the stubbing it protects, so both consumer files get
    identical isolation from one mechanism (C1-B-12 / C2-B-5 / C2-I-6).

    Both globals are process-wide and mutated through the seam: ``_worker_converter`` by
    ``_docling_to_markdown`` whenever ``_in_parse_worker()`` is true, and ``_IS_PARSE_WORKER``
    by ``_worker_initializer``. Under ``-n 8`` a test that sets either would otherwise leak it
    into whichever test runs next in the same worker process — and a leaked
    ``_IS_PARSE_WORKER`` is the worse of the two, since it makes later in-process calls cache a
    ``MagicMock`` converter for the rest of that worker's session.
    """
    archon_parser._worker_converter = None
    archon_parser._IS_PARSE_WORKER = False
    yield
    archon_parser._worker_converter = None
    archon_parser._IS_PARSE_WORKER = False


@contextlib.contextmanager
def docling_stubbed(mock_docling: MagicMock) -> Iterator[None]:
    """Route the docling parse path through *mock_docling*, in-process.

    Resets ``InlineExecutor.calls`` on entry so each use starts from a clean slate.
    """
    InlineExecutor.calls = []
    with patch.dict(sys.modules, dict.fromkeys(DOCLING_MODULES, mock_docling)):
        with patch("archon_search.parser.ProcessPoolExecutor", InlineExecutor):
            yield
