"""BE-13 (S15, S76) — CI guard: no query, prompt or traceback may reach a logging call of the shared LLM client.

Covers every module of ``archon_search/llm_client/`` (the five provider wrappers, the budget, the token counter and
the helpers), not a hand-kept list: a new module is scanned the day it is added. Mirrors
``tests/test_no_query_log_in_hyde.py`` with three differences:

* the wrappers receive a prompt (a request) rather than the raw query, so ``prompt``, a whole ``request`` object (its
  ``repr`` carries the prompt), the text derived from either, and a raw exception (an SDK error's message may echo the
  prompt) are as forbidden in a logging call as ``query``;
* ``exc_info=``, ``.exception(`` and the ``traceback`` printers are forbidden, because a traceback carries those
  messages too; failures reach the caller as ``LlmCallFailure`` carrying bounded, prompt-masked text, and the caller
  decides what to log;
* ``warn_once(...)`` and ``print(...)`` count as logging calls, whatever the logger is called.

The guard reads the modules with ``ast`` and never imports them. It looks at the call, not at its text, so quoted
parentheses, string prefixes, multi-line calls and aliased or chained loggers cannot hide a violation.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

_LLM_CLIENT_DIR = Path(__file__).parent.parent / "archon_search" / "llm_client"
_WRAPPER_MODULES = (
    "anthropic_client.py",
    "claude_cli_client.py",
    "llama_cpp_client.py",
    "ollama_client.py",
    "openai_client.py",
)

#: Method names of a logger, whatever the receiver is called (``_logger``, ``log``, ``logging.getLogger(...)``).
_LOG_METHODS = frozenset({"debug", "info", "warning", "warn", "error", "critical", "fatal", "log"})
#: Functions that write like a logger, called by name or through a module (``once_per_process.warn_once``).
_LOG_FUNCTIONS = frozenset({"warn_once", "print"})
#: Calls that write a traceback, or the text of one, on any receiver.
_TRACEBACK_PRINTERS = frozenset({"print_exc", "print_exception", "format_exc", "format_exception"})
#: ``logger.exception(...)`` writes a traceback; ``task.exception()`` (asyncio) does not, so the receiver must look like a logger.
_EXCEPTION_METHOD = "exception"
_LOGGER_RECEIVER_HINT = "log"
#: Calls whose result says nothing about their argument's content: a fingerprint, a size, a type.
_NEUTRAL_CALLS = frozenset({"_query_fingerprint", "len", "type", "bool"})
#: Names that hold a query, a prompt, text derived from one, server or subprocess output, or an exception that may echo
#: the prompt. Every name bound by ``except ... as name`` in the scanned module counts too.
_SENSITIVE_NAMES = frozenset(
    {"query", "prompt", "request", "text", "template_text", "payload", "exc", "exception", "stdout", "stderr", "detail", "reply", "body"}
)
#: Attributes that read a prompt, a query, server text or an exception's message out of an object
#: (``request.prompt``, ``response.text``, ``exc.args``). ``exc.reason`` (bounded, prompt-masked) is deliberately not here.
_SENSITIVE_ATTRIBUTES = frozenset({"prompt", "query", "text", "content", "args", "message", "body"})
_EXC_INFO_KEYWORD = "exc_info"


def _called_name(call: ast.Call) -> str | None:
    if isinstance(call.func, ast.Attribute):
        return call.func.attr
    if isinstance(call.func, ast.Name):
        return call.func.id
    return None


def _sensitive_use(node: ast.AST, names: frozenset[str]) -> str | None:
    """The first query-, prompt- or exception-carrying value read inside *node*, named for the report."""
    if isinstance(node, ast.Call) and _called_name(node) in _NEUTRAL_CALLS:
        return None
    if isinstance(node, ast.Attribute):
        if node.attr in _SENSITIVE_ATTRIBUTES:
            return f"`.{node.attr}`"
        # The base of ``request.model`` is the object, not a value read from it: judge what is read.
        return None if isinstance(node.value, ast.Name) else _sensitive_use(node.value, names)
    if isinstance(node, ast.Name):
        return f"`{node.id}`" if node.id in names else None
    for child in ast.iter_child_nodes(node):
        if found := _sensitive_use(child, names):
            return found
    return None


def _is_logging_call(call: ast.Call) -> bool:
    name = _called_name(call)
    if name in _LOG_FUNCTIONS:
        return True
    if not isinstance(call.func, ast.Attribute):
        return False
    if name == _EXCEPTION_METHOD:
        return _LOGGER_RECEIVER_HINT in ast.unparse(call.func.value).lower()
    return name in _LOG_METHODS or name in _TRACEBACK_PRINTERS


def _logging_calls(tree: ast.AST) -> list[ast.Call]:
    return [node for node in ast.walk(tree) if isinstance(node, ast.Call) and _is_logging_call(node)]


def count_logging_calls(source: str) -> int:
    """How many calls of *source* the guard recognises as logging calls."""
    return len(_logging_calls(ast.parse(source)))


def log_violations(source: str) -> list[tuple[int, str, str]]:
    """Return ``(lineno, stripped_line, reason)`` for every logging call that carries a query, a prompt, a whole
    request, a raw exception or server output, or a traceback.

    Limits, by design: a logger reached through ``getattr`` or an alias (``log = _logger.warning``), a prompt copied
    into a local with another name, and ``%``-formatting with ``locals()`` are not followed. Code review catches those;
    the guard catches the direct forms that a careless edit produces.
    """
    tree = ast.parse(source)
    lines = source.splitlines()
    names = _SENSITIVE_NAMES | {
        handler.name for handler in ast.walk(tree) if isinstance(handler, ast.ExceptHandler) and handler.name
    }
    violations: list[tuple[int, str, str]] = []
    for call in _logging_calls(tree):
        name = _called_name(call)
        if name == _EXCEPTION_METHOD or name in _TRACEBACK_PRINTERS:
            reason = f"`{name}` writes the traceback"
        elif any(keyword.arg == _EXC_INFO_KEYWORD for keyword in call.keywords):
            reason = "exc_info writes the traceback"
        else:
            arguments = [*call.args, *(keyword.value for keyword in call.keywords)]
            found = next((use for argument in arguments if (use := _sensitive_use(argument, names))), None)
            reason = f"{found} reaches the log call" if found else ""
        if reason:
            violations.append((call.lineno, lines[call.lineno - 1].strip(), reason))
    return sorted(violations)


# ---------------------------------------------------------------------------
# Meta-tests: verify the guard fires correctly and does not over-match
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "content",
    [
        '_logger.warning(f"q={query}")\n',
        'logger.warning(f"q={query}")\n',
        '_logger.warning(f"sent {prompt}")\n',
        '_logger.warning(f"sent {request.prompt}")\n',
        '_logger.warning(f"sent {request}")\n',
        '_logger.warning(rf"sent {prompt}")\n',
        '_logger.warning(F"sent {prompt}")\n',
        '_logger.warning("raw query: %s", query)\n',
        '_logger.warning("fp=%s, q=%s", _query_fingerprint(query), query)\n',
        '_logger.warning("sent %s", prompt)\n',
        '_logger.warning("sent %s", request.prompt)\n',
        '_logger.warning("sent %s", request)\n',
        '_logger.warning("sent %s", prompt[:10])\n',
        '_logger.warning("sent %s", str(prompt))\n',
        '_logger.warning("step 1) sent %s", prompt)\n',
        '_logger.warning("sent %s", template_text)\n',
        '_logger.warning("sent %s", payload)\n',
        '_logger.warning("server said %s", response.text)\n',
        '_logger.warning("failed: %s", exc)\n',
        '_logger.warning("failed", extra={"prompt": prompt})\n',
        'log.warning("sent %s", prompt)\n',
        'self._log.info("sent %s", prompt)\n',
        'logging.getLogger(__name__).warning(f"sent {prompt}")\n',
        'logging.warning("sent %s", prompt)\n',
        'print(prompt)\n',
        'warn_once(_logger, "key", "sent %s", prompt)\n',
        '_logger.debug("failed", exc_info=True)\n',
        '_logger.warning("failed", exc_info=exc)\n',
        '_logger.exception("failed")\n',
        'logging.exception("failed")\n',
        "traceback.print_exc()\n",
        "text = traceback.format_exc()\n",
        '_logger.warning("failed: %s", exc.args)\n',
        '_logger.warning("failed: %s", exc.body)\n',
        '_logger.warning("failed: %s", exc.message)\n',
        '_logger.warning("process said %s", stderr)\n',
        '_logger.warning("process said %s", stdout)\n',
        '_logger.warning("server said %s", body)\n',
        '_logger.warning("server said %s", reply)\n',
        '_logger.warning("server said %s", detail)\n',
        'once_per_process.warn_once(_logger, "key", "sent %s", prompt)\n',
        "builtins.print(prompt)\n",
        'self._logger.exception("failed")\n',
        'try:\n    pass\nexcept Exception as failure:\n    _logger.warning("failed: %s", failure)\n',
    ],
)
def test_guard_detects_a_forbidden_logging_call(content: str) -> None:
    assert log_violations(content), f"guard missed: {content!r}"


def test_guard_detects_multiline_logging_call() -> None:
    content = (
        "_logger.warning(\n"
        '    "sent %s",\n'
        "    request.prompt\n"
        ")\n"
    )

    assert log_violations(content)


def test_guard_names_the_violating_line_and_reason() -> None:
    content = 'x = 1\n_logger.warning("sent %s", prompt)\n'

    assert log_violations(content) == [(2, '_logger.warning("sent %s", prompt)', "`prompt` reaches the log call")]


def test_guard_reports_the_line_the_call_starts_on() -> None:
    content = '_logger.warning(\n    "sent %s",\n    prompt,\n)\n'

    assert [lineno for lineno, _, _ in log_violations(content)] == [1]


@pytest.mark.parametrize(
    "content",
    [
        '_logger.warning("llama_cpp error (fp=%s)", _query_fingerprint(query))\n',
        '_logger.warning(f"fp={_query_fingerprint(query)}")\n',
        '_logger.warning("collecting %s", truncated_query)\n',
        '_logger.warning("sent %d chars", len(prompt))\n',
        '_logger.warning("failed with %s", type(exc).__name__)\n',
        '_logger.warning("model %s", request.model)\n',
        '_logger.warning(f"model {request.model} timed out after {request.timeout_seconds}s")\n',
        '_logger.warning("because %s", exc.reason)\n',
        '_logger.warning("sent the prompt (it is not logged)")\n',
        '_logger.warning("request failed")\n',
        'warn_once(_logger, "key", "cannot read the window from %s", server)\n',
        "async def warn_once(logger, key, message, *args): ...\n",
        'truncated_query = query[:2000]\nprompt = TEMPLATE.format(query=truncated_query)\nawait client.call(request)\n',
        '_logger.warning(\n    "timeout (fp=%s)",\n    _query_fingerprint(query),\n)\n',
        "total = math.log(size)\n",
        "error = task.exception()\n",
        "if future.exception() is not None:\n    pass\n",
    ],
)
def test_guard_ignores_a_harmless_call(content: str) -> None:
    assert not log_violations(content), f"guard over-matched: {content!r}"


# ---------------------------------------------------------------------------
# Real guard — every module of archon_search/llm_client/
# ---------------------------------------------------------------------------


def _scanned_modules() -> list[Path]:
    return sorted(_LLM_CLIENT_DIR.rglob("*.py"))


def test_the_scan_covers_the_provider_wrappers() -> None:
    """Positive control: a moved package or a changed glob must not turn the real guard into a vacuous pass."""
    scanned = {path.name for path in _scanned_modules()}

    assert set(_WRAPPER_MODULES) <= scanned


def test_the_scan_recognises_the_real_logging_calls_of_the_package() -> None:
    """Positive control: the guard sees the log sites the package really has (``warn_once`` and its ``logger.warning``)."""
    recognised = sum(count_logging_calls(module.read_text("utf-8")) for module in _scanned_modules())

    assert recognised >= 2


@pytest.mark.parametrize("module", _scanned_modules(), ids=lambda path: path.name)
def test_no_query_text_logged_in_llm_client_wrappers(module: Path) -> None:
    """No module of ``archon_search/llm_client/`` passes a query, a prompt, a request or a traceback to a log call.

    On failure the assertion names the matching lines for quick triage.
    """
    violations = [
        f"  line {lineno}: {snippet}  ({reason})" for lineno, snippet, reason in log_violations(module.read_text("utf-8"))
    ]

    assert not violations, (
        f"A query, prompt or traceback reaches a logging call in {module.relative_to(_LLM_CLIENT_DIR.parent)}:\n"
        + "\n".join(violations)
        + "\nLog counts, hosts, models and failure kinds only; failures travel as LlmCallFailure."
    )
