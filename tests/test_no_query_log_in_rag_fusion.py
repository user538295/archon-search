"""C5 Task 6.1 — CI guard: raw query/variant strings must never appear in logging calls in rag_fusion.py.

Analogous to ``tests/test_no_query_log_in_hyde.py``.  Reads ``archon_search/rag_fusion.py``
as a plain string and asserts that no logging call receives the ``query``
variable (or known variant-text variables) directly without going through
``_query_fingerprint``.

The guard also checks that the same ``_query_fingerprint`` from
``archon_search._privacy`` is imported in both ``hyde.py`` and ``rag_fusion.py``,
rather than local duplicate implementations.

The guard does NOT import the module — it is purely static text analysis.

Detection strategy (two complementary patterns):

1. **f-string pattern** — catches ``_logger.*(f"...{query}...")``:
   Looks for a logging call (``logging.``, ``_logger.``, or ``logger.``) that
   opens with an f-string containing a bare ``{<banned_var>}`` expression.

2. **bare-arg full-file scan** — catches ``_logger.*(..., query)`` etc.:
   Uses ``re.DOTALL`` to scan the full source for logging-call-like blocks that
   contain a bare banned variable identifier as an argument, excluding occurrences
   inside ``_query_fingerprint(...)``.

Both scans cover all three logger-call forms used in the project:
``logging.``, ``_logger.`` (project convention), and ``logger.``.

Banned variable names checked (in addition to ``query``):
  - ``variants`` — the list of all generated variant strings
  - ``variant`` — a single variant string during iteration
  - ``all_queries`` — the combined [query] + variants list
  - ``truncated_query`` — the 2000-char-truncated query fed to the LLM (the cut is gone; the name stays banned)
  - ``sent_query`` — the query as it reaches the provider: the whole query for a cloud provider, the head that fits the window for a local one
  - ``prompt`` and ``raw_text`` — the prompt wrapping the query and the model's raw answer (BE-12)
  - ``failure`` — the provider's failure: its text may echo the query, so only the masked ``reason`` is logged

Log calls are the logger methods AND ``warn_once(...)`` (RAG Fusion's once-per-process WARNING route, S15).

NOTE: ``text`` and ``line`` are too generic for reliable static analysis and
are NOT checked.  The ``rag_fusion.py`` implementation MUST NOT use bare local
variable names containing user-derived text as logging arguments.  The f-string regex cannot see a quote before the
placeholder, so ``test_rag_fusion_py_logs_with_no_f_string`` bans f-strings in log calls outright (AST based).
"""
from __future__ import annotations

import ast
import re
import textwrap
from pathlib import Path

# ---------------------------------------------------------------------------
# Logger-call prefix — shared alternation used in both patterns.
# Covers: logging.X(  _logger.X(  logger.X(
# The alternation is ordered longest-first to avoid partial matches.
# ---------------------------------------------------------------------------
_LOG_PREFIX = r"(?:logging\.|_logger\.|(?<![_\w])logger\.)"
# A logging call: a logger method, or ``warn_once(...)`` — RAG Fusion's once-per-process WARNING route (S15).
_LOG_CALL = rf"(?:{_LOG_PREFIX}\w+|(?<![_\w])warn_once)"

#: The log calls of rag_fusion.py: the rate-limit WARNING and the four ``warn_once`` calls (no room for the query,
#: failure, no valid variant, variant shortfall).
_EXPECTED_RAG_FUSION_LOG_CALLS = 5

# ---------------------------------------------------------------------------
# Banned variable names that must never appear as raw log arguments.
# ---------------------------------------------------------------------------
_BANNED_VAR_NAMES: tuple[str, ...] = (
    "query",
    "variants",
    "variant",
    "all_queries",
    "truncated_query",
    "sent_query",
    "prompt",
    "raw_text",
    "failure",
)

# ---------------------------------------------------------------------------
# Pattern 1 — f-string with bare {<banned_var>} inside a logging call
# ---------------------------------------------------------------------------
# Matches: _logger.warning(f"q={query}")
# Does NOT match: _logger.warning(f"q={_query_fingerprint(query)}")
#   — because _query_fingerprint( immediately precedes the variable, making it non-bare.
# The lookahead (?![_\w(]) after the variable ensures the expression is bare,
# not part of a longer name or a function call like {query_fingerprint(...)}.


def _build_fstring_pattern(var_name: str) -> re.Pattern[str]:
    """Build the f-string pattern for a specific banned variable name."""
    return re.compile(
        _LOG_CALL + r"\s*\([^)]*f['\"][^'\"]*\{" + re.escape(var_name) + r"(?![_\w(])",
        re.DOTALL,
    )


_FSTRING_PATTERNS: dict[str, re.Pattern[str]] = {
    name: _build_fstring_pattern(name) for name in _BANNED_VAR_NAMES
}

# Keep the original ``query`` pattern available under the legacy name used by
# many meta-tests below.
_FSTRING_QUERY_IN_LOG = _FSTRING_PATTERNS["query"]

# ---------------------------------------------------------------------------
# Pattern 2 — bare banned-var positional arg in a logging call (full-file scan)
# ---------------------------------------------------------------------------
_LOG_OPENER = re.compile(_LOG_CALL + r"\s*\(", re.DOTALL)
_FINGERPRINT_CALL = re.compile(r"_query_fingerprint\s*\([^)]*\)")
# Matches single- or double-quoted string literals (with escape sequences).
# Stripping these prevents the word "query" inside a log format-string from
# triggering the bare-query check.
_STRING_LITERAL = re.compile(
    r'"[^"\\]*(?:\\.[^"\\]*)*"|\'[^\'\\]*(?:\\.[^\'\\]*)*\'',
    re.DOTALL,
)
# Matches wrapping calls that return a count/type, NOT the raw text content of the variable.
# Only ``len`` is whitelisted: ``len(variants)`` logs a count (safe); ``str(variants)`` would
# expose raw text content (NOT safe) and must NOT be stripped.
# Example: len(variants) → __SAFE_CALL__  (only the count is logged, not the text)
# This is built dynamically per banned var in _bare_vars_in_log_violations.


def _bare_token_re(var_name: str) -> re.Pattern[str]:
    return re.compile(r"(?<!\w)" + re.escape(var_name) + r"(?!\w)")


_BARE_TOKEN_PATTERNS: dict[str, re.Pattern[str]] = {
    name: _bare_token_re(name) for name in _BANNED_VAR_NAMES
}
# Legacy alias
_BARE_QUERY_TOKEN = _BARE_TOKEN_PATTERNS["query"]


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
    return _bare_vars_in_log_violations(source, ["query"])


def _build_safe_call_pattern(var_name: str) -> re.Pattern[str]:
    """Build a pattern matching ``len(<var>)`` — the only whitelisted wrapping call.

    ``len(variants)`` is safe because it logs only a count, not the raw text
    content.  ``str(variants)`` is NOT safe (exposes raw text) and is intentionally
    excluded from this whitelist.
    """
    return re.compile(r"len\s*\(\s*" + re.escape(var_name) + r"\s*\)")


_SAFE_CALL_PATTERNS: dict[str, re.Pattern[str]] = {
    name: _build_safe_call_pattern(name) for name in _BANNED_VAR_NAMES
}


def _bare_vars_in_log_violations(
    source: str, var_names: list[str] | None = None
) -> list[tuple[int, str]]:
    """Return (lineno, stripped_line) pairs where any banned variable exists as
    a raw logging-call argument.

    If ``var_names`` is None, checks all ``_BANNED_VAR_NAMES``.

    Sanitisation order:
    1. Strip string literals — prevents the English word inside a log format-string
       from triggering the check.
    2. Remove ``_query_fingerprint(...)`` calls — the only legitimate bare-var use.
    3. Remove ``len(<var>)`` calls — the only whitelisted wrapping: logs only a count,
       not the raw text content.  ``str(variants)`` is NOT stripped and would be caught.
    After sanitisation, any remaining bare banned-var token is a violation.
    """
    if var_names is None:
        var_names = list(_BANNED_VAR_NAMES)

    violations: list[tuple[int, str]] = []
    for m in _LOG_OPENER.finditer(source):
        open_pos = m.end() - 1  # position of the '(' character
        args_text = _extract_call_args(source, open_pos)
        # 1. Strip string literals first (removes banned words inside format strings)
        sanitised = _STRING_LITERAL.sub("__STR__", args_text)
        # 2. Remove _query_fingerprint(query) (the only legitimate bare-query use)
        sanitised = _FINGERPRINT_CALL.sub("__FP__", sanitised)
        # 3. Remove safe wrapping calls for each banned variable
        for name in var_names:
            sanitised = _SAFE_CALL_PATTERNS[name].sub("__SAFE__", sanitised)
        for name in var_names:
            if _BARE_TOKEN_PATTERNS[name].search(sanitised):
                lineno = source.count("\n", 0, m.start()) + 1
                snippet = source.splitlines()[lineno - 1].strip()
                violations.append((lineno, snippet))
                break  # only one violation per logging call
    return violations


# ---------------------------------------------------------------------------
# Meta-tests: verify guard regexes fire correctly and do not over-match
# (query variable — mirrors test_no_query_log_in_hyde.py)
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
            "RAG Fusion timeout (fp=%s)",
            _query_fingerprint(query),
        )
        """
    )
    assert not _bare_query_in_log_violations(content), (
        "bare-arg guard falsely matched multiline fingerprint-only call"
    )


# ---------------------------------------------------------------------------
# Meta-tests: banned variable names (variants, variant, all_queries, truncated_query)
# ---------------------------------------------------------------------------


def test_guard_ignores_len_variants_in_log() -> None:
    """Guard must NOT fire when len(variants) is used as a logging arg (count only, not text)."""
    content = '_logger.warning("got %d variants (fp=%s)", len(variants), fp)\n'
    assert not _bare_vars_in_log_violations(content, ["variants"]), (
        "bare-arg guard falsely matched len(variants) — only the count is logged, not raw text"
    )


def test_guard_detects_str_variants_in_log() -> None:
    """Guard must fire when str(variants) is logged — str() exposes raw text content."""
    content = '_logger.warning("variants: %s", str(variants))\n'
    assert _bare_vars_in_log_violations(content, ["variants"]), (
        "Guard failed to detect str(variants) — str() of a list exposes raw variant text"
    )


def test_guard_detects_fstring_variants_in_log() -> None:
    """Guard must fire on a logger call that embeds {variants} in an f-string."""
    content = '_logger.warning(f"got {variants}")\n'
    assert _FSTRING_PATTERNS["variants"].search(content), (
        "Pattern failed to detect f-string with {variants} in a logging call"
    )


def test_guard_detects_bare_variants_arg() -> None:
    """Guard must fire on a logger call that passes variants as a positional arg."""
    content = '_logger.warning("generated: %s", variants)\n'
    assert _bare_vars_in_log_violations(content, ["variants"]), (
        "Guard failed to detect bare 'variants' positional arg in a logging call"
    )


def test_guard_ignores_variants_in_non_log_call() -> None:
    """Guard must NOT fire when variants appears only in non-logging code."""
    content = "results = [search(v) for v in variants]\n"
    assert not _FSTRING_PATTERNS["variants"].search(content), (
        "f-string pattern falsely matched non-logging 'variants' usage"
    )
    assert not _bare_vars_in_log_violations(content, ["variants"]), (
        "bare-arg guard falsely matched non-logging 'variants' usage"
    )


def test_guard_detects_fstring_variant_in_log() -> None:
    """Guard must fire on a logger call that embeds {variant} in an f-string."""
    content = '_logger.debug(f"checking {variant}")\n'
    assert _FSTRING_PATTERNS["variant"].search(content), (
        "Pattern failed to detect f-string with {variant} in a logging call"
    )


def test_guard_detects_bare_variant_arg() -> None:
    """Guard must fire on a logger call that passes variant as a positional arg."""
    content = '_logger.warning("dropped: %s", variant)\n'
    assert _bare_vars_in_log_violations(content, ["variant"]), (
        "Guard failed to detect bare 'variant' positional arg in a logging call"
    )


def test_guard_ignores_variant_in_non_log_call() -> None:
    """Guard must NOT fire when variant appears only in non-logging code."""
    content = "validated = self._validate_variant(variant)\n"
    assert not _FSTRING_PATTERNS["variant"].search(content), (
        "f-string pattern falsely matched non-logging 'variant' usage"
    )
    assert not _bare_vars_in_log_violations(content, ["variant"]), (
        "bare-arg guard falsely matched non-logging 'variant' usage"
    )


def test_guard_detects_fstring_all_queries_in_log() -> None:
    """Guard must fire on a logger call that embeds {all_queries} in an f-string."""
    content = '_logger.warning(f"sending {all_queries} to embed")\n'
    assert _FSTRING_PATTERNS["all_queries"].search(content), (
        "Pattern failed to detect f-string with {all_queries} in a logging call"
    )


def test_guard_detects_bare_all_queries_arg() -> None:
    """Guard must fire on a logger call that passes all_queries as a positional arg."""
    content = '_logger.warning("queries: %s", all_queries)\n'
    assert _bare_vars_in_log_violations(content, ["all_queries"]), (
        "Guard failed to detect bare 'all_queries' positional arg in a logging call"
    )


def test_guard_ignores_all_queries_in_non_log_call() -> None:
    """Guard must NOT fire when all_queries appears only in non-logging code."""
    content = "vectors = await asyncio.gather(*[embed(q) for q in all_queries])\n"
    assert not _FSTRING_PATTERNS["all_queries"].search(content), (
        "f-string pattern falsely matched non-logging 'all_queries' usage"
    )
    assert not _bare_vars_in_log_violations(content, ["all_queries"]), (
        "bare-arg guard falsely matched non-logging 'all_queries' usage"
    )


def test_guard_detects_fstring_truncated_query_in_log() -> None:
    """Guard must fire on a logger call that embeds {truncated_query} in an f-string."""
    content = '_logger.warning(f"sending {truncated_query} to LLM")\n'
    assert _FSTRING_PATTERNS["truncated_query"].search(content), (
        "Pattern failed to detect f-string with {truncated_query} in a logging call"
    )


def test_guard_detects_bare_truncated_query_arg() -> None:
    """Guard must fire on a logger call that passes truncated_query as a positional arg."""
    content = '_logger.warning("prompt query: %s", truncated_query)\n'
    assert _bare_vars_in_log_violations(content, ["truncated_query"]), (
        "Guard failed to detect bare 'truncated_query' positional arg in a logging call"
    )


def test_guard_detects_fstring_sent_query_in_log() -> None:
    """Guard must fire on a logger call that embeds {sent_query} (the query as it reaches the provider) in an f-string."""
    content = '_logger.warning(f"sending {sent_query} to LLM")\n'
    assert _FSTRING_PATTERNS["sent_query"].search(content), (
        "Pattern failed to detect f-string with {sent_query} in a logging call"
    )


def test_guard_detects_bare_sent_query_arg_in_warn_once() -> None:
    """Guard must fire on a ``warn_once(...)`` call that passes sent_query as a positional arg."""
    content = 'warn_once(_logger, KEY, "RAG Fusion: %s", sent_query)\n'
    assert _bare_vars_in_log_violations(content, ["sent_query"]), (
        "Guard failed to detect bare 'sent_query' positional arg in a warn_once call"
    )


def test_guard_ignores_truncated_query_in_non_log_call() -> None:
    """Guard must NOT fire when truncated_query appears only in non-logging code."""
    content = textwrap.dedent(
        """\
        truncated_query = query[:2000]
        prompt = _PROMPT_TEMPLATE.format(num_queries=2, query=truncated_query)
        """
    )
    assert not _FSTRING_PATTERNS["truncated_query"].search(content), (
        "f-string pattern falsely matched non-logging 'truncated_query' usage"
    )
    assert not _bare_vars_in_log_violations(content, ["truncated_query"]), (
        "bare-arg guard falsely matched non-logging 'truncated_query' usage"
    )


# ---------------------------------------------------------------------------
# Privacy.py import verification
# ---------------------------------------------------------------------------


def test_rag_fusion_imports_fingerprint_from_privacy() -> None:
    """rag_fusion.py must import _query_fingerprint from archon_search._privacy.

    This verifies there is no local duplicate of the fingerprint function that
    could diverge from the canonical implementation in _privacy.py.
    """
    rag_fusion_path = (
        Path(__file__).parent.parent / "archon_search" / "rag_fusion.py"
    )
    source = rag_fusion_path.read_text(encoding="utf-8")
    assert "from archon_search._privacy import _query_fingerprint" in source, (
        "rag_fusion.py must import _query_fingerprint from archon_search._privacy, "
        "not define it locally.  Found no matching import in rag_fusion.py."
    )


def test_hyde_imports_fingerprint_from_privacy() -> None:
    """hyde.py must import _query_fingerprint from archon_search._privacy.

    Verifies both modules share the same canonical fingerprint implementation.
    """
    hyde_path = Path(__file__).parent.parent / "archon_search" / "hyde.py"
    source = hyde_path.read_text(encoding="utf-8")
    assert "from archon_search._privacy import _query_fingerprint" in source, (
        "hyde.py must import _query_fingerprint from archon_search._privacy, "
        "not define it locally.  Found no matching import in hyde.py."
    )


# ---------------------------------------------------------------------------
# Real guard — reads rag_fusion.py as a single string and asserts zero violations.
# ---------------------------------------------------------------------------


def test_no_raw_query_in_rag_fusion_logging() -> None:
    """rag_fusion.py must not pass raw query/variant strings to any logging call.

    All log messages that need per-request correlation must go through
    ``_query_fingerprint(query)``.  Banned variable names that must not appear
    as raw logging arguments: query, variants, variant, all_queries,
    truncated_query.  On failure the assertion names the matching lines for
    quick triage.
    """
    rag_fusion_path = (
        Path(__file__).parent.parent / "archon_search" / "rag_fusion.py"
    )
    source = rag_fusion_path.read_text(encoding="utf-8")

    violations: list[str] = []

    for var_name, pattern in _FSTRING_PATTERNS.items():
        for m in pattern.finditer(source):
            lineno = source.count("\n", 0, m.start()) + 1
            snippet = source.splitlines()[lineno - 1].strip()
            violations.append(f"  line {lineno} (f-string, var={var_name!r}): {snippet}")

    for lineno, snippet in _bare_vars_in_log_violations(source):
        violations.append(f"  line {lineno} (bare arg): {snippet}")

    assert not violations, (
        "Raw query/variant string passed directly to a logging call in "
        "archon_search/rag_fusion.py:\n"
        + "\n".join(violations)
        + "\nUse _query_fingerprint(query) in all logging calls."
    )


# ---------------------------------------------------------------------------
# BE-12: the guard also sees ``warn_once`` calls, the prompt and the raw model answer (S15)
# ---------------------------------------------------------------------------


def test_guard_detects_bare_query_arg_in_warn_once() -> None:
    """Guard must fire on a ``warn_once(...)`` call (RAG Fusion's WARNING route) that passes query."""
    content = 'warn_once(_logger, "rag_fusion", "raw query: %s", query)\n'
    assert _bare_query_in_log_violations(content)


def test_guard_detects_fstring_query_in_warn_once() -> None:
    """Guard must fire on a ``warn_once(...)`` call whose message is an f-string holding {query}."""
    content = 'warn_once(_logger, "rag_fusion", f"q={query}")\n'
    assert _FSTRING_QUERY_IN_LOG.search(content)


def test_guard_ignores_fingerprint_only_warn_once() -> None:
    """Guard must NOT fire when a ``warn_once(...)`` call only carries the query fingerprint."""
    content = 'warn_once(_logger, "rag_fusion", "failed (fp=%s)", _query_fingerprint(query))\n'
    assert not _FSTRING_QUERY_IN_LOG.search(content)
    assert not _bare_query_in_log_violations(content)


def test_guard_detects_prompt_and_raw_answer_in_warn_once() -> None:
    """Guard must fire when a log call receives the prompt or the model's raw answer (S15: no log line holds them)."""
    for argument in ("request.prompt", "prompt", "raw_text"):
        content = f'warn_once(_logger, "rag_fusion", "bad: %s", {argument})\n'
        assert _bare_vars_in_log_violations(content), f"Guard failed to detect {argument} in a warn_once call"


def test_guard_ignores_masked_reason_count_and_fingerprint_in_warn_once() -> None:
    """Guard must NOT fire on the shapes rag_fusion.py uses: fingerprint plus masked failure text, or a variant count."""
    failure = 'warn_once(_logger, "rag_fusion", "failed (fp=%s): %s", _query_fingerprint(query), reason)\n'
    shortfall = 'warn_once(_logger, "rag_fusion", "%d of %d (fp=%s)", len(variants), num_queries, _query_fingerprint(query))\n'
    assert not _bare_vars_in_log_violations(failure)
    assert not _bare_vars_in_log_violations(shortfall)


def test_guard_scans_every_logging_call_of_rag_fusion_py() -> None:
    """The real guard is not vacuous: rag_fusion.py's four log calls are found and their arguments parsed."""
    source = (Path(__file__).parent.parent / "archon_search" / "rag_fusion.py").read_text(encoding="utf-8")

    openers = [m for m in _LOG_OPENER.finditer(source) if "getLogger" not in m.group()]

    assert len(openers) == _EXPECTED_RAG_FUSION_LOG_CALLS
    assert all(_extract_call_args(source, opener.end() - 1) for opener in openers)


def _log_calls(tree: ast.AST) -> list[ast.Call]:
    """Every ``warn_once(...)`` or logger-method call of a module."""
    calls: list[ast.Call] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        is_warn_once = isinstance(func, ast.Name) and func.id == "warn_once"
        is_logger_call = (
            isinstance(func, ast.Attribute)
            and isinstance(func.value, ast.Name)
            and func.value.id in {"_logger", "logger", "logging"}
            and func.attr != "getLogger"
        )
        if is_warn_once or is_logger_call:
            calls.append(node)
    return calls


def test_rag_fusion_py_logs_with_no_f_string() -> None:
    """No log call of rag_fusion.py is given an f-string: placeholders are the only way a value reaches a log line.

    The regex guard above cannot see a quote before a ``{query}`` placeholder, so this AST check closes that gap.
    """
    source = (Path(__file__).parent.parent / "archon_search" / "rag_fusion.py").read_text(encoding="utf-8")

    calls = _log_calls(ast.parse(source))

    assert len(calls) == _EXPECTED_RAG_FUSION_LOG_CALLS
    f_string_calls = [call.lineno for call in calls if any(isinstance(arg, ast.JoinedStr) for arg in call.args)]
    assert not f_string_calls, f"f-string passed to a log call at lines {f_string_calls}"


def test_rag_fusion_py_masks_the_failure_text_before_logging_it() -> None:
    """The failure text reaches ``warn_once`` only through ``mask_query_echo`` (a provider may echo the query back)."""
    source = (Path(__file__).parent.parent / "archon_search" / "rag_fusion.py").read_text(encoding="utf-8")
    tree = ast.parse(source)

    handlers = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.ExceptHandler) and isinstance(node.type, ast.Name) and node.type.id == "LlmCallFailure"
    ]

    [handler] = handlers
    masked_calls = [
        call
        for call in ast.walk(handler)
        if isinstance(call, ast.Call) and isinstance(call.func, ast.Name) and call.func.id == "mask_query_echo"
    ]
    assert len(masked_calls) == 1
    assert any(
        isinstance(arg, ast.Call) and isinstance(arg.func, ast.Name) and arg.func.id == "str"
        for arg in masked_calls[0].args
    ), "mask_query_echo must be given str(failure)"
