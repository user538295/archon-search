"""BE-22 (S76) — CI guard: no excerpt, prompt, answer or description text may reach a logging call of the description modules.

Covers ``description_generator.py`` (reads excerpts, sends the prompt, receives the answer), ``description_writer.py`` and
``description_write.py`` (handle the description text and its vector). Like ``tests/test_no_query_log_in_llm_client.py``
the guard reads the modules with ``ast`` and never imports them, looks at the call and not at its text, and counts
``warn_once(...)`` and ``print(...)`` as logging calls. A traceback is as forbidden as the text: a store or provider error's
message can carry a chunk text, so failures log a collection name, a counter or ``type(exc).__name__`` and travel as
values (``DescriptionFailed.detail``), which never hold document text.
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

_PACKAGE_DIR = Path(__file__).parent.parent / "archon_search"
_DESCRIPTION_MODULES = ("description_generator.py", "description_writer.py", "description_write.py")

_LOG_METHODS = frozenset({"debug", "info", "warning", "warn", "error", "critical", "fatal", "log"})
_LOG_FUNCTIONS = frozenset({"warn_once", "print"})
_TRACEBACK_PRINTERS = frozenset({"print_exc", "print_exception", "format_exc", "format_exception"})
_EXCEPTION_METHOD = "exception"
_LOGGER_RECEIVER_HINT = "log"
#: Calls whose result says nothing about their argument's content: a size, a type, a truth value.
_NEUTRAL_CALLS = frozenset({"len", "type", "bool", "sum"})
#: Constructors whose ``detail`` text reaches a WARNING and the failed status: treated like a logging call.
_TEXT_SINK_CALLS = frozenset({"_GenerationStopped", "DescriptionFailed"})
#: A module's own logging helpers (``_warn_kept_without_vector``): their arguments reach a log line.
_LOGGING_HELPER_PREFIXES = ("_warn", "_log")
#: Names that hold an excerpt, the prompt, the model's answer or a description, or an object that carries them (a
#: result, an outcome, a change, a row). Every name bound by ``except ... as`` counts too: a raw exception's message
#: may echo a chunk text.
_SENSITIVE_NAMES = frozenset(
    {
        "excerpt", "excerpts", "sample", "samples", "text", "prompt", "request", "answer", "reply", "raw",
        "description", "new_description", "old_description", "template_text", "joined", "body", "payload",
        "result", "outcome", "change", "fields", "meta", "row",
    }
)
#: Attributes that read text, an exception's message or its cause out of an object (``result.text``, ``exc.args``,
#: ``OSError.strerror``, ``exc.__cause__``).
_SENSITIVE_ATTRIBUTES = frozenset(
    {"text", "prompt", "description", "excerpts", "args", "message", "msg", "strerror", "body", "content", "__cause__", "__context__"}
)
_STREAM_WRITE_METHOD = "write"
_STREAM_HINTS = ("stderr", "stdout")
_EXC_INFO_KEYWORD = "exc_info"


def _called_name(call: ast.Call) -> str | None:
    if isinstance(call.func, ast.Attribute):
        return call.func.attr
    if isinstance(call.func, ast.Name):
        return call.func.id
    return None


def _is_logging_call(call: ast.Call) -> bool:
    name = _called_name(call)
    if name in _LOG_FUNCTIONS or name in _TEXT_SINK_CALLS:
        return True
    if name is not None and name.startswith(_LOGGING_HELPER_PREFIXES):  # the module's own ``_warn_*`` / ``_log_*``
        return True
    if isinstance(call.func, ast.Name):  # ``from logging import warning``, ``from traceback import print_exc``
        return name in _LOG_METHODS or name in _TRACEBACK_PRINTERS
    if not isinstance(call.func, ast.Attribute):
        return False
    receiver = ast.unparse(call.func.value).lower()
    if name == _EXCEPTION_METHOD:
        return _LOGGER_RECEIVER_HINT in receiver
    if name == _STREAM_WRITE_METHOD:  # ``sys.stderr.write(text)``
        return any(hint in receiver for hint in _STREAM_HINTS)
    return name in _LOG_METHODS or name in _TRACEBACK_PRINTERS


def _logging_calls(tree: ast.AST) -> list[ast.Call]:
    return [node for node in ast.walk(tree) if isinstance(node, ast.Call) and _is_logging_call(node)]


def _sensitive_use(node: ast.AST, names: frozenset[str]) -> str | None:
    """The first excerpt-, prompt-, answer- or exception-carrying value read inside *node*, named for the report."""
    if isinstance(node, ast.Call) and _called_name(node) in _NEUTRAL_CALLS:
        return None
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
        # ``answer.strip()``, ``excerpts.copy()``: what a method returns derives from the value it is called on.
        receiver = node.func.value
        while isinstance(receiver, ast.Attribute):
            receiver = receiver.value
        if isinstance(receiver, ast.Name) and receiver.id in names:
            return f"`{receiver.id}`"
    if isinstance(node, ast.Attribute):
        # ``result.text``, ``exc.args``, and the private spellings of a sensitive name (``self._excerpts``).
        if node.attr in _SENSITIVE_ATTRIBUTES or node.attr.lstrip("_") in names:
            return f"`.{node.attr}`"
        return None if isinstance(node.value, ast.Name) else _sensitive_use(node.value, names)
    if isinstance(node, ast.Name):
        return f"`{node.id}`" if node.id in names else None
    for child in ast.iter_child_nodes(node):
        if found := _sensitive_use(child, names):
            return found
    return None


def count_logging_calls(source: str) -> int:
    return len(_logging_calls(ast.parse(source)))


def log_violations(source: str) -> list[tuple[int, str, str]]:
    """``(lineno, stripped_line, reason)`` for every logging call that carries an excerpt, a prompt, an answer, a
    description, a raw exception or a traceback.

    Limits, by design: a logger reached through an alias and a value copied into a local with another name are not followed.
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


@pytest.mark.parametrize(
    "content",
    [
        '_logger.warning("sent %s", excerpts)\n',
        'logger.warning(f"sample {sample}")\n',
        'logger.warning("description %s", description)\n',
        'logger.warning("description %s", new_description)\n',
        'logger.warning("answer %s", answer)\n',
        'logger.warning("answer %s", result.text)\n',
        'logger.warning("sent %s", request.prompt)\n',
        'logger.warning("sent %s", request)\n',
        'logger.warning("text %s", text[:20])\n',
        'logger.warning("text %s", str(text))\n',
        'logger.warning("change %s", change.text)\n',
        'warn_once(logger, "key", "got %s", answer)\n',
        'print(excerpts)\n',
        'logger.warning("failed", exc_info=True)\n',
        'logger.exception("failed")\n',
        "traceback.print_exc()\n",
        'try:\n    pass\nexcept Exception as failure:\n    logger.warning("failed: %s", failure)\n',
        'logger.warning("failed: %s", exc.args)\n',
        'logger.warning("answer %s", answer.strip())\n',
        'logger.warning("description %s", description.lower())\n',
        'logger.warning("sample %s", excerpts.copy())\n',
        'logger.warning(f"text {text.lower()}")\n',
        'logger.warning("failed: %s", error.strerror)\n',
        'logger.warning("failed: %s", error.msg)\n',
        'logger.warning("failed: %s", failure.__cause__)\n',
        'from logging import warning\nwarning("sent %s", excerpts)\n',
        'from traceback import print_exc\nprint_exc()\n',
        "sys.stderr.write(text)\n",
        "sys.stdout.write(answer)\n",
        '_GenerationStopped(reason, f"bad {answer}")\n',
        'raise _GenerationStopped(reason, f"bad {answer.strip()}")\n',
        'DescriptionFailed(reason, text)\n',
        'try:\n    pass\nexcept Exception as failure:\n    _GenerationStopped(reason, str(failure))\n',
        'logger.warning("outcome %s", outcome)\n',
        'logger.warning("result %s", result)\n',
        'logger.warning("change %s", change)\n',
        'logger.warning("row %s", row)\n',
        'logger.warning("sent %s", self._prompt)\n',
        'logger.warning("sent %s", self._excerpts)\n',
        'try:\n    pass\nexcept Exception as exc:\n    self._warn_kept(collection, model, str(exc))\n',
        'self._warn_kept(collection, model, answer)\n',
    ],
)
def test_guard_detects_a_forbidden_logging_call(content: str) -> None:
    assert log_violations(content), f"guard missed: {content!r}"


def test_guard_names_the_violating_line_and_reason() -> None:
    content = 'x = 1\nlogger.warning("sent %s", excerpts)\n'

    assert log_violations(content) == [(2, 'logger.warning("sent %s", excerpts)', "`excerpts` reaches the log call")]


@pytest.mark.parametrize(
    "content",
    [
        'logger.warning("collection %r failed (%s)", collection, failed.reason.value)\n',
        'logger.warning("collection %r: %s", collection, failed.detail)\n',
        'logger.warning("sampled %d excerpts", len(excerpts))\n',
        'try:\n    pass\nexcept Exception as exc:\n    logger.warning("failed with %s", type(exc).__name__)\n',
        'logger.warning("because %s", failure.reason)\n',
        'logger.warning("model %s", request.model)\n',
        'warn_once(logger, "key", "failed for %r", collection)\n',
        'logger.warning("collection %s", collection.upper())\n',
        "sys.stderr.write(collection)\n",
        'try:\n    pass\nexcept Exception as unexpected:\n    stop = _GenerationStopped(reason, f"unexpected {type(unexpected).__name__}")\n',
        'DescriptionFailed(reason, "the model returned an empty answer")\n',
        'self._warn_kept_without_vector(collection, model, "unusable vector")\n',
        'try:\n    pass\nexcept Exception as exc:\n    self._warn_kept(collection, model, type(exc).__name__)\n',
        'logger.warning("sampled %d", sum(len(e) for e in excerpts))\n',
        'logger.warning("window %s", self._window.size)\n',
        "error = task.exception()\n",
        "total = math.log(size)\n",
    ],
)
def test_guard_ignores_a_harmless_call(content: str) -> None:
    assert not log_violations(content), f"guard over-matched: {content!r}"


def test_the_scan_covers_the_three_description_modules() -> None:
    """Positive control: a renamed module must not turn the real guard into a vacuous pass."""
    for module in _DESCRIPTION_MODULES:
        assert (_PACKAGE_DIR / module).is_file(), f"{module} moved; update the guard"


def test_the_scan_recognises_the_real_logging_calls_of_the_modules() -> None:
    """Positive control: the guard sees the log sites the generator and the writer really have."""
    for module in ("description_generator.py", "description_writer.py"):
        assert count_logging_calls((_PACKAGE_DIR / module).read_text("utf-8")) >= 1, module


@pytest.mark.parametrize("module", _DESCRIPTION_MODULES)
def test_the_guard_flags_a_violating_call_added_to_each_module(module: str) -> None:
    """Mutation control: the same module source plus one forbidden call is reported, so a clean result of the real
    guard is not a vacuous pass (``description_write.py`` has no log call of its own)."""
    source = (_PACKAGE_DIR / module).read_text("utf-8") + '\nlogger.warning("leak %s", excerpts)\n'

    assert [reason for _, _, reason in log_violations(source)] == ["`excerpts` reaches the log call"]


@pytest.mark.parametrize("module", _DESCRIPTION_MODULES)
def test_no_excerpt_or_description_text_in_logs_of_description_modules(module: str) -> None:
    """No description module passes an excerpt, a prompt, an answer, a description or a traceback to a log call."""
    source = (_PACKAGE_DIR / module).read_text("utf-8")

    violations = [f"  line {lineno}: {snippet}  ({reason})" for lineno, snippet, reason in log_violations(source)]

    assert not violations, (
        f"Document or description text reaches a logging call in archon_search/{module}:\n"
        + "\n".join(violations)
        + "\nLog collection names, counts and failure kinds only."
    )
