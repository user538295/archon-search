"""CI guard: no new ``str(exc)`` of a caught exception in ``server/``, ``jobs/`` or a wire-feeding module.

CLAUDE.md hard invariant: never put ``str(exc)`` in a wire-facing ``detail`` or job
``error`` field for a caught exception whose message embeds internals. Inside an
``except ... as <name>:`` body, :func:`scan_source` reports every ``str(<name>)``,
``repr(<name>)`` and f-string interpolation of ``<name>`` that is not an argument of a
``logger.*`` call — wherever it goes: an ``McpErrorResponse``, an ``HTTPException`` /
``JSONResponse`` detail, a job ``error=``, or a local / helper that later reaches one.
In every guarded module the log is the only sanctioned destination for raw exception text.

:data:`_GUARDED_FILES` extends the scan to the modules outside ``server/`` and ``jobs/``
whose text is copied verbatim onto the wire: ``sync.py`` (``SyncResult.errors`` -> ``POST /sync``
job result; ``CollectionProgress.error`` -> ``GET /status`` / ``GET /indexing-state``),
``pipeline.py`` and ``parser.py`` (``IngestResult.error``, built from ``ParseError.detail`` ->
MCP ``ingest_file`` / ``ingest_directory``), ``model_validation.py`` (``provider_warnings`` ->
``GET /status``), and ``defref_extractor.py``, ``graph_extractor.py`` and ``acl.py``
(``GraphExtractionResult.warnings`` / ``fatal_error`` and ACL warnings -> ``IngestResult`` ->
MCP / ingest job result). ``prose_extraction_backend.py`` is not listed: its exceptions reach
the wire only through ``graph_extractor.py``'s sanitized handlers.

A deliberately user-facing exception (its message is text archon-search built itself)
is allowlisted in :data:`_ALLOWLIST` by (file, enclosing function, ``except`` clause
type, ordinal) — the ordinal numbers the stringifying handlers of that type within the
function, so each entry covers exactly one handler and a second handler of an allowlisted
type in the same function is a new site. No line numbers, so unrelated edits never churn
it. A new site fails ``test_no_new_str_exc_of_a_caught_exception``; a stale entry fails
``test_every_allowlist_entry_still_matches``, so the list only ever shrinks to the truth.

``traceback.format_exc()`` inside a handler and ``traceback.format_exception[_only]()`` of
a caught exception are reported like ``str()``. Calls on ``logger`` / ``_logger`` / ``log``
/ ``logging`` and on ``[logging.]getLogger(...)`` count as logging.

Accepted blind spots (this is a syntactic scan, not a taint analysis):
the exception passed whole (``detail=exc``), ``exc.args`` / attributes, ``%`` / ``.format()``,
an exception bound to another name first, a logger reached through any other name
(``self.log``), other ``traceback`` helpers (``format_tb``, ``TracebackException``), and a
helper that stringifies an exception it receives as a parameter
(``export_archive.manifest_error_detail``).
"""
from __future__ import annotations

import ast
import textwrap
from dataclasses import dataclass
from pathlib import Path
from typing import NamedTuple

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent
_GUARDED_DIRS = ("archon_search/server", "archon_search/jobs")
_GUARDED_FILES = (
    "archon_search/sync.py",
    "archon_search/pipeline.py",
    "archon_search/parser.py",
    "archon_search/model_validation.py",
    "archon_search/defref_extractor.py",
    "archon_search/graph_extractor.py",
    "archon_search/acl.py",
)
_LOGGER_NAMES = frozenset({"logger", "_logger", "log", "logging"})
_GET_LOGGER = "getLogger"
_STRINGIFIERS = frozenset({"str", "repr"})
_FORMAT_CURRENT_EXCEPTION = "format_exc"
_FORMAT_EXCEPTION = frozenset({"format_exception", "format_exception_only"})


class Finding(NamedTuple):
    file: str
    function: str
    exc_type: str  # the ``except`` clause's type, as written (``ast.unparse``)
    ordinal: int  # 1-based, among this function's stringifying handlers of ``exc_type``
    lineno: int

    @property
    def key(self) -> tuple[str, str, str, int]:
        return self.file, self.function, self.exc_type, self.ordinal


@dataclass
class _Handler:
    name: str | None  # the ``as`` binding; None for a bare ``except T:``
    exc_type: str
    ordinal: int | None = None  # assigned on the handler's first finding


def _called_name(func: ast.expr) -> str | None:
    """``name`` for ``name(...)`` and ``<anything>.name(...)``."""
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return None


def _is_logging_call(func: ast.expr) -> bool:
    if not isinstance(func, ast.Attribute):
        return False
    target = func.value
    if isinstance(target, ast.Call):  # logging.getLogger(__name__).warning(...)
        return _called_name(target.func) == _GET_LOGGER
    return isinstance(target, ast.Name) and target.id in _LOGGER_NAMES


class _Scanner(ast.NodeVisitor):
    def __init__(self, file: str) -> None:
        self.file = file
        self.functions = ["<module>"]
        self.handlers: list[_Handler] = []  # the enclosing handlers, innermost last
        self.ordinals: dict[tuple[str, str], int] = {}  # (function, exc_type) -> last ordinal
        self.findings: list[Finding] = []

    def visit_FunctionDef(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        self.functions.append(node.name)
        self.generic_visit(node)
        self.functions.pop()

    visit_AsyncFunctionDef = visit_FunctionDef

    def visit_ExceptHandler(self, node: ast.ExceptHandler) -> None:
        self.handlers.append(_Handler(node.name, ast.unparse(node.type) if node.type else ""))
        self.generic_visit(node)
        self.handlers.pop()

    def visit_Call(self, node: ast.Call) -> None:
        func = node.func
        if _is_logging_call(func):
            return  # the log is where raw exception text belongs
        called = _called_name(func)
        if isinstance(func, ast.Name) and called in _STRINGIFIERS and len(node.args) == 1:
            self._report_name(node.args[0], node)
        elif called == _FORMAT_CURRENT_EXCEPTION and self.handlers:
            self._report(self.handlers[-1], node)
        elif called in _FORMAT_EXCEPTION:
            for arg in node.args:
                self._report_name(arg, node)
        self.generic_visit(node)

    def visit_FormattedValue(self, node: ast.FormattedValue) -> None:
        self._report_name(node.value, node)
        self.generic_visit(node)

    def _report_name(self, value: ast.expr, node: ast.expr) -> None:
        if not isinstance(value, ast.Name):
            return
        for handler in reversed(self.handlers):  # the innermost binding wins
            if handler.name == value.id:
                self._report(handler, node)
                return

    def _report(self, handler: _Handler, node: ast.expr) -> None:
        function = self.functions[-1]
        if handler.ordinal is None:
            counter = (function, handler.exc_type)
            handler.ordinal = self.ordinals[counter] = self.ordinals.get(counter, 0) + 1
        self.findings.append(Finding(self.file, function, handler.exc_type, handler.ordinal, node.lineno))


def scan_source(source: str, file: str = "<snippet>") -> list[Finding]:
    """Every stringification of a caught exception in *source* outside a logging call."""
    scanner = _Scanner(file)
    scanner.visit(ast.parse(source))
    return scanner.findings


def _guarded_paths() -> list[Path]:
    package_files = [path for directory in _GUARDED_DIRS for path in sorted((_REPO_ROOT / directory).rglob("*.py"))]
    return package_files + [_REPO_ROOT / file for file in _GUARDED_FILES]


def _scan_guarded() -> list[Finding]:
    return [
        finding
        for path in _guarded_paths()
        for finding in scan_source(path.read_text(encoding="utf-8"), path.relative_to(_REPO_ROOT).as_posix())
    ]


_SERVER = "archon_search/server"

# (file, enclosing function, except clause type, ordinal) of one handler whose message is
# curated, user-facing text.
_ALLOWLIST: frozenset[tuple[str, str, str, int]] = frozenset({
    # "No community representatives found for collection ..." — names the caller's collection.
    # search / explain each have a single-collection and a multi-collection handler.
    (f"{_SERVER}/mcp.py", "search", "GraphCommunitiesNotBuiltError", 1),
    (f"{_SERVER}/mcp.py", "search", "GraphCommunitiesNotBuiltError", 2),
    (f"{_SERVER}/mcp.py", "explain", "GraphCommunitiesNotBuiltError", 1),
    (f"{_SERVER}/mcp.py", "explain", "GraphCommunitiesNotBuiltError", 2),
    (f"{_SERVER}/routes_search.py", "search", "GraphCommunitiesNotBuiltError", 1),
    (f"{_SERVER}/routes_search.py", "search", "GraphCommunitiesNotBuiltError", 2),
    (f"{_SERVER}/routes_explain.py", "explain_endpoint", "GraphCommunitiesNotBuiltError", 1),
    (f"{_SERVER}/routes_explain.py", "explain_endpoint", "GraphCommunitiesNotBuiltError", 2),
    # Fixed text: multi-collection explain needs rerank.
    (f"{_SERVER}/mcp.py", "explain", "ExplainMultiCollectionNoRerankError", 1),
    # Retryable store-lock timeout; the message carries only the timeout.
    (f"{_SERVER}/mcp.py", "ingest_file", "StoreBusyError", 1),
    (f"{_SERVER}/mcp.py", "ingest_directory", "StoreBusyError", 1),
    (f"{_SERVER}/mcp.py", "delete_document", "StoreBusyError", 1),
    (f"{_SERVER}/mcp.py", "update_collection", "StoreBusyError", 1),
    # Embedding-model validation text written for the caller.
    (f"{_SERVER}/mcp.py", "update_collection", "ModelValidationError", 1),
    (f"{_SERVER}/routes_collections.py", "add_collection", "ModelValidationError", 1),
    (f"{_SERVER}/routes_collections.py", "patch_collection", "ModelValidationError", 1),
    # `unsupported model '<name>'; supported: ...` — the registry's own text (model_specs.UnsupportedModelError).
    ("archon_search/model_validation.py", "validate_embedding_model", "UnsupportedModelError", 1),
    # _validate_namespace's rule text (1); datetime.fromisoformat echoes only the caller's expires_at (2).
    (f"{_SERVER}/mcp.py", "create_key", "ValueError", 1),
    (f"{_SERVER}/mcp.py", "create_key", "ValueError", 2),
    (f"{_SERVER}/routes_keys.py", "create_key", "ValueError", 1),
    # Curated rotation errors: the server-held current key is blank, grace_seconds out of range.
    (f"{_SERVER}/mcp.py", "rotate_key", "BlankCurrentKeyError", 1),
    (f"{_SERVER}/mcp.py", "rotate_key", "KeyRotationInputError", 1),
    (f"{_SERVER}/routes_keys.py", "rotate_key", "(BlankCurrentKeyError, KeyRotationInputError)", 1),
    # Curated job errors: no pending REWRITE migration, no graph nodes, ingest path not found.
    (f"{_SERVER}/routes_collections.py", "_migration_task", "NoPendingRewriteMigrationError", 1),
    (f"{_SERVER}/routes_graph.py", "_community_rebuild_task", "NoGraphNodesError", 1),
    (f"{_SERVER}/routes_jobs.py", "_default_ingest_task", "IngestPathNotFoundError", 1),
    (f"{_SERVER}/routes_jobs.py", "_default_ingest_task_with_lock", "IngestPathNotFoundError", 1),
    # Archive rejection text / PathUnsafeError reason code archon-search built itself.
    (f"{_SERVER}/routes_export.py", "_import_task", "(ArchiveRejectedError, PathUnsafeError)", 1),
    # TelemetryReader.resolve_dates' date-range text, about the caller's since/until.
    (f"{_SERVER}/routes_telemetry.py", "get_telemetry_stats", "ValueError", 1),
    (f"{_SERVER}/routes_telemetry.py", "get_telemetry_entries", "ValueError", 1),
    # The docling worker's failure payload crosses the process boundary as text. It only ever
    # becomes ParseError's log-only cause; the wire gets ParseError.detail (PARSE_FAILED_DETAIL).
    ("archon_search/parser.py", "_docling_to_markdown", "Exception", 1),
})


# ---------------------------------------------------------------------------
# The guard
# ---------------------------------------------------------------------------


def test_no_new_str_exc_of_a_caught_exception() -> None:
    new = [f for f in _scan_guarded() if f.key not in _ALLOWLIST]
    assert not new, (
        "caught exception stringified outside a logging call — send a fixed DETAIL constant and log "
        "the exception, or allowlist a curated message in _ALLOWLIST:\n"
        + "\n".join(f"  {f.file}:{f.lineno} in {f.function}() except {f.exc_type} (handler #{f.ordinal})" for f in new)
    )


def test_every_allowlist_entry_still_matches() -> None:
    stale = _ALLOWLIST - {f.key for f in _scan_guarded()}
    assert not stale, f"allowlist entries no longer match any site — remove them: {sorted(stale)}"


# ---------------------------------------------------------------------------
# Meta-tests: the scanner itself
# ---------------------------------------------------------------------------


def _scan(snippet: str) -> list[tuple[str, str, int, int]]:
    return [(f.function, f.exc_type, f.ordinal, f.lineno) for f in scan_source(textwrap.dedent(snippet))]


@pytest.mark.parametrize(
    "sink",
    [
        "return McpErrorResponse(error=str(exc), code='internal_error')",
        "raise HTTPException(status_code=500, detail=str(exc))",
        "return JSONResponse({'detail': f'failed: {exc}'}, status_code=500)",
        "store.update(job_id, status='failed', error=repr(exc))",
        "failure_error = str(exc)",
        "_mark_failed(f'{exc!r}')",
    ],
)
def test_scan_reports_each_stringification_of_the_caught_exception(sink: str) -> None:
    source = f"""
    async def handler():
        try:
            await work()
        except RuntimeError as exc:
            {sink}
    """
    assert _scan(source) == [("handler", "RuntimeError", 1, 6)]


def test_scan_ignores_logging_calls() -> None:
    source = """
    def handler():
        try:
            work()
        except (OSError, ValueError) as exc:
            logger.warning("failed: %s", str(exc))
            logger.error(f"failed: {exc}")
            log.info("failed: %r", repr(exc))
            _logger.warning("failed: %s", str(exc))
            logging.getLogger(__name__).error(f"failed: {exc}")
            getLogger("archon").exception("failed: %s", traceback.format_exc())
            raise HTTPException(status_code=500, detail=DETAIL)
    """
    assert _scan(source) == []


def test_scan_ignores_names_that_are_not_a_caught_exception() -> None:
    source = """
    def handler(path):
        try:
            work()
        except Exception:
            return JSONResponse({"detail": f"missing {path}"}, status_code=404)
        return str(path)
    """
    assert _scan(source) == []


def test_scan_attributes_a_finding_to_the_innermost_handler_and_function() -> None:
    source = """
    def create_app():
        async def tool():
            try:
                work()
            except OSError as exc:
                try:
                    cleanup()
                except KeyError as exc:
                    return McpErrorResponse(error=str(exc))
    """
    assert _scan(source) == [("tool", "KeyError", 1, 10)]


def test_scan_reports_a_tuple_except_clause_as_written() -> None:
    source = """
    def task():
        try:
            work()
        except (ArchiveRejectedError, PathUnsafeError) as exc:
            store.update(job_id, error=str(exc))
    """
    assert _scan(source) == [("task", "(ArchiveRejectedError, PathUnsafeError)", 1, 6)]


def test_scan_numbers_each_stringifying_handler_of_a_type_within_its_function() -> None:
    """Two handlers of one type in one function are two keys, so one allowlist entry covers one handler."""
    source = """
    def tool():
        try:
            work()
        except ValueError:
            pass
        try:
            work()
        except ValueError as exc:
            return McpErrorResponse(error=str(exc), hint=f"{exc}")
        try:
            work()
        except ValueError as exc:
            return McpErrorResponse(error=str(exc))
    """
    assert _scan(source) == [("tool", "ValueError", 1, 10), ("tool", "ValueError", 1, 10), ("tool", "ValueError", 2, 14)]


@pytest.mark.parametrize(
    "sink",
    [
        "return McpErrorResponse(error=traceback.format_exc())",
        "raise HTTPException(status_code=500, detail=format_exc())",
        "store.update(job_id, error=''.join(traceback.format_exception(exc)))",
        "failure = traceback.format_exception_only(type(exc), exc)",
    ],
)
def test_scan_reports_traceback_formatting_of_the_caught_exception(sink: str) -> None:
    source = f"""
    def task():
        try:
            work()
        except OSError as exc:
            {sink}
    """
    assert _scan(source) == [("task", "OSError", 1, 6)]


def test_scan_reports_format_exc_in_an_unnamed_handler() -> None:
    source = """
    def task():
        try:
            work()
        except Exception:
            store.update(job_id, error=traceback.format_exc())
    """
    assert _scan(source) == [("task", "Exception", 1, 6)]
