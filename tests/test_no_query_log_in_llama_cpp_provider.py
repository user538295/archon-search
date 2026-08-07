"""BE-2 — CI guard: raw query string must never appear in logging calls in
``archon_search/providers/llama_cpp_provider.py``.

Mirrors ``tests/test_no_query_log_in_hyde.py`` (same regex strategy, same
meta-tests) but targets the llama.cpp query-expansion provider module.

The guard does NOT import the module — it is purely static text analysis.
"""
from __future__ import annotations

import re
import textwrap
from pathlib import Path

_LOG_PREFIX = r"(?:logging\.|_logger\.|(?<![_\w])logger\.)"

_FSTRING_QUERY_IN_LOG = re.compile(
    _LOG_PREFIX + r"""\w+\s*\([^)]*f['"][^'"]*\{query(?![_\w(])""",
    re.DOTALL,
)

_LOG_OPENER = re.compile(_LOG_PREFIX + r"\w+\s*\(", re.DOTALL)
_FINGERPRINT_CALL = re.compile(r"_query_fingerprint\s*\([^)]*\)")
_STRING_LITERAL = re.compile(
    r'"[^"\\]*(?:\\.[^"\\]*)*"|\'[^\'\\]*(?:\\.[^\'\\]*)*\'',
    re.DOTALL,
)
_BARE_QUERY_TOKEN = re.compile(r"(?<!\w)query(?!\w)")


def _extract_call_args(source: str, open_paren_pos: int) -> str:
    """Return the text between the opening paren at ``open_paren_pos`` and its
    matching closing paren (exclusive).  Handles nested parens.  Returns an
    empty string if the paren is unmatched (e.g., end-of-file)."""
    depth = 1
    i = open_paren_pos + 1
    while i < len(source) and depth > 0:
        c = source[i]
        if c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
        i += 1
    return source[open_paren_pos + 1 : i - 1] if depth == 0 else ""


def _bare_query_in_log_violations(source: str) -> list[tuple[int, str]]:
    """Return (lineno, stripped_line) pairs where a bare ``query`` exists as a
    logging-call argument."""
    violations: list[tuple[int, str]] = []
    for m in _LOG_OPENER.finditer(source):
        open_pos = m.end() - 1
        args_text = _extract_call_args(source, open_pos)
        sanitised = _STRING_LITERAL.sub("__STR__", args_text)
        sanitised = _FINGERPRINT_CALL.sub("__FP__", sanitised)
        if _BARE_QUERY_TOKEN.search(sanitised):
            lineno = source.count("\n", 0, m.start()) + 1
            snippet = source.splitlines()[lineno - 1].strip()
            violations.append((lineno, snippet))
    return violations


# ---------------------------------------------------------------------------
# Meta-tests: verify guard regexes fire correctly and do not over-match
# ---------------------------------------------------------------------------


def test_guard_detects_fstring_query_in_log() -> None:
    content = '_logger.warning(f"q={query}")\n'
    assert _FSTRING_QUERY_IN_LOG.search(content), (
        "Pattern failed to detect f-string with {query} in a _logger logging call"
    )


def test_guard_detects_fstring_query_logger_variant() -> None:
    content = 'logger.warning(f"q={query}")\n'
    assert _FSTRING_QUERY_IN_LOG.search(content), (
        "Pattern failed to detect f-string with {query} in a logger. logging call"
    )


def test_guard_detects_bare_query_arg() -> None:
    content = '_logger.warning("raw query: %s", query)\n'
    assert _bare_query_in_log_violations(content), (
        "Guard failed to detect bare 'query' positional arg in a logging call"
    )


def test_guard_detects_bare_query_last_arg() -> None:
    content = '_logger.warning("fp=%s, q=%s", _query_fingerprint(query), query)\n'
    assert _bare_query_in_log_violations(content), (
        "Guard failed to detect bare 'query' as a trailing positional arg"
    )


def test_guard_detects_bare_query_logger_variant() -> None:
    content = 'logger.warning("q=%s", query)\n'
    assert _bare_query_in_log_violations(content), (
        "Guard failed to detect bare 'query' in a logger. logging call"
    )


def test_guard_detects_multiline_logging_call() -> None:
    content = textwrap.dedent(
        """\
        _logger.warning(
            "raw query: %s",
            query
        )
        """
    )
    assert _bare_query_in_log_violations(content), (
        "Guard failed to detect bare 'query' in a multiline logging call"
    )


def test_guard_ignores_fingerprint_only() -> None:
    content = '_logger.warning("llama_cpp error (fp=%s)", _query_fingerprint(query))\n'
    assert not _FSTRING_QUERY_IN_LOG.search(content), (
        f"f-string pattern falsely matched fingerprint-only logging call: {content!r}"
    )
    assert not _bare_query_in_log_violations(content), (
        "bare-arg guard falsely matched fingerprint-only logging call"
    )


def test_guard_ignores_query_in_variable_name() -> None:
    content = '_logger.warning("collecting %s", truncated_query)\n'
    assert not _FSTRING_QUERY_IN_LOG.search(content), (
        "f-string pattern falsely matched 'truncated_query' identifier"
    )
    assert not _bare_query_in_log_violations(content), (
        "bare-arg guard falsely matched 'truncated_query' identifier"
    )


def test_guard_ignores_non_log_calls() -> None:
    content = textwrap.dedent(
        """\
        truncated_query = query[:2000]
        prompt = _HYDE_PROMPT_TEMPLATE.format(query=truncated_query)
        vector = await generator.generate(query)
        """
    )
    assert not _FSTRING_QUERY_IN_LOG.search(content), (
        "f-string pattern falsely matched non-logging code"
    )
    assert not _bare_query_in_log_violations(content), (
        "bare-arg guard falsely matched non-logging code"
    )


def test_guard_ignores_multiline_fingerprint_only() -> None:
    content = textwrap.dedent(
        """\
        _logger.warning(
            "llama_cpp timeout (fp=%s)",
            _query_fingerprint(query),
        )
        """
    )
    assert not _bare_query_in_log_violations(content), (
        "bare-arg guard falsely matched multiline fingerprint-only call"
    )


# ---------------------------------------------------------------------------
# Real guard — reads llama_cpp_provider.py as a single string and asserts
# zero violations.
# ---------------------------------------------------------------------------


def test_no_raw_query_in_llama_cpp_provider_logging() -> None:
    """llama_cpp_provider.py must not pass the raw query variable to any logging call.

    All log messages that need per-request correlation must go through
    ``_query_fingerprint(query)``.  On failure the assertion names the
    matching lines for quick triage.
    """
    provider_path = (
        Path(__file__).parent.parent
        / "archon_search"
        / "providers"
        / "llama_cpp_provider.py"
    )
    source = provider_path.read_text(encoding="utf-8")

    violations: list[str] = []

    for m in _FSTRING_QUERY_IN_LOG.finditer(source):
        lineno = source.count("\n", 0, m.start()) + 1
        snippet = source.splitlines()[lineno - 1].strip()
        violations.append(f"  line {lineno} (f-string): {snippet}")

    for lineno, snippet in _bare_query_in_log_violations(source):
        violations.append(f"  line {lineno} (bare arg): {snippet}")

    assert not violations, (
        "Raw query string passed directly to a logging call in "
        "archon_search/providers/llama_cpp_provider.py:\n"
        + "\n".join(violations)
        + "\nUse _query_fingerprint(query) in all logging calls."
    )
