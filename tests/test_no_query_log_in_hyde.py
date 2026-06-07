"""C4 Task 6.1 — CI guard: raw query string must never appear in logging calls in hyde.py.

Analogous to ``tests/test_no_fstring_sql.py``.  Reads ``archon_search/hyde.py``
as a plain string and asserts that no logging call receives the ``query``
variable directly (without going through ``_query_fingerprint``).

The guard does NOT import the module — it is purely static text analysis.

Detection strategy (two complementary patterns):

1. **f-string pattern** — catches ``_logger.*(f"...{query}...")``:
   Looks for a logging call (``logging.``, ``_logger.``, or ``logger.``) that
   opens with an f-string containing the bare ``{query}`` expression.

2. **bare-arg full-file scan** — catches ``_logger.*(..., query)`` etc.:
   Uses ``re.DOTALL`` to scan the full source for logging-call-like blocks that
   contain a bare ``query`` identifier as an argument, excluding occurrences
   inside ``_query_fingerprint(query)``.

Both scans cover all three logger-call forms used in the project:
``logging.``, ``_logger.`` (project convention), and ``logger.``.
"""
from __future__ import annotations

import re
import textwrap
from pathlib import Path

# ---------------------------------------------------------------------------
# Logger-call prefix — shared alternation used in both patterns.
# Covers: logging.X(  _logger.X(  logger.X(
# The alternation is ordered longest-first to avoid partial matches.
# ---------------------------------------------------------------------------
_LOG_PREFIX = r"(?:logging\.|_logger\.|(?<![_\w])logger\.)"

# ---------------------------------------------------------------------------
# Pattern 1 — f-string with bare {query} inside a logging call
# ---------------------------------------------------------------------------
# Matches: _logger.warning(f"q={query}")
# Does NOT match: _logger.warning(f"q={_query_fingerprint(query)}")
#   — because _query_fingerprint( immediately precedes query, making it non-bare.
# The lookahead (?![_\w(]) after query ensures {query} is a bare expression,
# not part of a longer name or a function call like {query_fingerprint(...)}.
_FSTRING_QUERY_IN_LOG = re.compile(
    _LOG_PREFIX + r"""\w+\s*\([^)]*f['"][^'"]*\{query(?![_\w(])""",
    re.DOTALL,
)

# ---------------------------------------------------------------------------
# Pattern 2 — bare ``query`` positional arg in a logging call (full-file scan)
# ---------------------------------------------------------------------------
# Strategy: match a logging call whose argument span (up to closing paren at
# the same nesting depth) contains a bare ``query`` token not inside
# _query_fingerprint(...).
#
# We use a two-step approach on each match of a logging call opener:
#   a. Find the start of the logging call.
#   b. Walk forward character-by-character to find the balanced closing paren,
#      collecting the argument text.
#   c. Strip _query_fingerprint(...) occurrences from the argument text.
#   d. Check if a bare ``query`` token remains.
#
# This handles multiline logging calls correctly.

_LOG_OPENER = re.compile(_LOG_PREFIX + r"\w+\s*\(", re.DOTALL)
_FINGERPRINT_CALL = re.compile(r"_query_fingerprint\s*\([^)]*\)")
# Matches single- or double-quoted string literals (with escape sequences).
# Stripping these prevents the word "query" inside a log format-string from
# triggering the bare-query check.
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
    logging-call argument.

    Scans the full source using balanced-paren matching so multiline calls are
    handled correctly.

    Sanitisation order:
    1. Strip string literals — prevents the English word "query" inside a log
       format-string (e.g., "falling back to query embedding") from triggering.
    2. Remove ``_query_fingerprint(query)`` calls — the only legitimate use of
       the query variable in a logging argument list.
    After sanitisation, any remaining bare ``query`` token is a violation.
    """
    violations: list[tuple[int, str]] = []
    for m in _LOG_OPENER.finditer(source):
        open_pos = m.end() - 1  # position of the '(' character
        args_text = _extract_call_args(source, open_pos)
        # 1. Strip string literals first (removes "query" inside format strings)
        sanitised = _STRING_LITERAL.sub("__STR__", args_text)
        # 2. Remove _query_fingerprint(query) (the only legitimate bare-query use)
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
    """Guard must fire on a logger call that embeds {query} in an f-string."""
    content = '_logger.warning(f"q={query}")\n'
    assert _FSTRING_QUERY_IN_LOG.search(content), (
        "Pattern failed to detect f-string with {query} in a _logger logging call"
    )


def test_guard_detects_fstring_query_logger_variant() -> None:
    """Guard must fire on logger. (no underscore) variant with f-string query."""
    content = 'logger.warning(f"q={query}")\n'
    assert _FSTRING_QUERY_IN_LOG.search(content), (
        "Pattern failed to detect f-string with {query} in a logger. logging call"
    )


def test_guard_detects_bare_query_arg() -> None:
    """Guard must fire on a logger call that passes query as a positional arg."""
    content = '_logger.warning("raw query: %s", query)\n'
    assert _bare_query_in_log_violations(content), (
        "Guard failed to detect bare 'query' positional arg in a logging call"
    )


def test_guard_detects_bare_query_last_arg() -> None:
    """Guard must fire when query is the last positional arg, after a fingerprint."""
    content = '_logger.warning("fp=%s, q=%s", _query_fingerprint(query), query)\n'
    assert _bare_query_in_log_violations(content), (
        "Guard failed to detect bare 'query' as a trailing positional arg"
    )


def test_guard_detects_bare_query_logger_variant() -> None:
    """Guard must fire on logger. (no underscore) variant with bare query arg."""
    content = 'logger.warning("q=%s", query)\n'
    assert _bare_query_in_log_violations(content), (
        "Guard failed to detect bare 'query' in a logger. logging call"
    )


def test_guard_detects_multiline_logging_call() -> None:
    """Guard must fire on a multiline logging call with bare query arg."""
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
    """Guard must NOT fire when query only appears inside _query_fingerprint(...)."""
    content = '_logger.warning("HyDE error (fp=%s)", _query_fingerprint(query))\n'
    assert not _FSTRING_QUERY_IN_LOG.search(content), (
        f"f-string pattern falsely matched fingerprint-only logging call: {content!r}"
    )
    assert not _bare_query_in_log_violations(content), (
        "bare-arg guard falsely matched fingerprint-only logging call"
    )


def test_guard_ignores_query_in_variable_name() -> None:
    """Guard must NOT fire on identifiers that contain 'query' as a substring."""
    content = '_logger.warning("collecting %s", truncated_query)\n'
    assert not _FSTRING_QUERY_IN_LOG.search(content), (
        "f-string pattern falsely matched 'truncated_query' identifier"
    )
    assert not _bare_query_in_log_violations(content), (
        "bare-arg guard falsely matched 'truncated_query' identifier"
    )


def test_guard_ignores_non_log_calls() -> None:
    """Guard must NOT fire when query appears in non-logging code."""
    content = textwrap.dedent(
        """\
        truncated_query = query[:2000]
        prompt = _PROMPT_TEMPLATE.format(query=truncated_query)
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
    """Guard must NOT fire on a multiline call that only uses _query_fingerprint."""
    content = textwrap.dedent(
        """\
        _logger.warning(
            "HyDE timeout (fp=%s)",
            _query_fingerprint(query),
        )
        """
    )
    assert not _bare_query_in_log_violations(content), (
        "bare-arg guard falsely matched multiline fingerprint-only call"
    )


# ---------------------------------------------------------------------------
# Real guard — reads hyde.py as a single string and asserts zero violations.
# ---------------------------------------------------------------------------


def test_no_raw_query_in_hyde_logging() -> None:
    """hyde.py must not pass the raw query variable to any logging call.

    All log messages that need per-request correlation must go through
    ``_query_fingerprint(query)``.  On failure the assertion names the
    matching lines for quick triage.
    """
    hyde_path = Path(__file__).parent.parent / "archon_search" / "hyde.py"
    source = hyde_path.read_text(encoding="utf-8")

    violations: list[str] = []

    # Pattern 1: f-string with bare {query}
    for m in _FSTRING_QUERY_IN_LOG.finditer(source):
        lineno = source.count("\n", 0, m.start()) + 1
        snippet = source.splitlines()[lineno - 1].strip()
        violations.append(f"  line {lineno} (f-string): {snippet}")

    # Pattern 2: bare query arg (handles multiline calls)
    for lineno, snippet in _bare_query_in_log_violations(source):
        violations.append(f"  line {lineno} (bare arg): {snippet}")

    assert not violations, (
        "Raw query string passed directly to a logging call in archon_search/hyde.py:\n"
        + "\n".join(violations)
        + "\nUse _query_fingerprint(query) in all logging calls."
    )
