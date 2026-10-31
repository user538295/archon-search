"""BE-16 (LCD C12, S4, S15) — CI guard: raw chunk/community content must never appear in a logging call of the graph
client path.

The graph path is every module that sees community text: ``archon_search/community_builder.py`` (the consumer, which
renders the summary prompt from ``chunk_texts`` and logs the failures) and every module of
``archon_search/llm_client/`` (the wrappers that carry the prompt to the model). It replaces the guard of the removed
``archon_search/enrichment/`` adapters, which had the same job.

Analogous to ``tests/test_no_query_log_in_hyde.py`` (S15a) and ``tests/test_no_query_log_in_llm_client.py``,
with three content-bearing identifiers:

- ``chunk_text``  — guarded defensively; it was the per-chunk text passed to the former ``label_relationships``
  path (removed by BE-17), kept in the guard so a future re-introduction cannot leak it.
- ``chunk_texts`` — the per-community list of chunk texts the community builder renders into the summary prompt.
- ``community_text`` — included for forward-compatibility with the literal name used in the plan wording.

``warn_once(...)`` counts as a logging call (the community builder and the wrappers log through it).

The guard does NOT import the modules — it is purely static text analysis, same as the hyde guard.

Entity names (e.g. ``item.get("source_entity")``) are explicitly OUT of scope: they are already-abstracted graph
metadata extracted from the raw text, not raw input content.

Detection strategy (two complementary patterns, mirroring the hyde guard):

1. **f-string pattern** — catches ``_logger.*(f"...{chunk_text}...")``.
2. **bare-arg full-file scan** — catches ``_logger.*(..., chunk_text)`` etc.,
   using balanced-paren matching so multiline calls are handled correctly.

Unlike the hyde guard, there is no legitimate "wrapped" usage to exclude
(no ``_content_fingerprint`` helper exists for chunk/community text) — any
bare occurrence of one of the three identifiers as a logging-call argument
is a violation.
"""
from __future__ import annotations

import re
import textwrap
from pathlib import Path

_ARCHON_DIR = Path(__file__).parent.parent / "archon_search"
_COMMUNITY_BUILDER = _ARCHON_DIR / "community_builder.py"
_LLM_CLIENT_DIR = _ARCHON_DIR / "llm_client"

# ---------------------------------------------------------------------------
# Logger-call prefix — shared alternation used in both patterns.
# Covers: logging.X(  _logger.X(  logger.X(
# ---------------------------------------------------------------------------
_LOG_PREFIX = r"(?:logging\.|_logger\.|(?<![_\w])logger\.)"
# A ``warn_once(...)`` call writes like a logger, called by name or through a module.
_LOG_CALL = r"(?:" + _LOG_PREFIX + r"\w+|(?<![\w])(?:\w+\.)?warn_once)"

# ---------------------------------------------------------------------------
# Content-variable alternation. Order does not affect correctness (each
# alternative is boundary-checked independently), but longest-first keeps the
# intent readable.
# ---------------------------------------------------------------------------
_CONTENT_VAR_ALT = r"(?:chunk_texts|chunk_text|community_text)"

# ---------------------------------------------------------------------------
# Pattern 1 — f-string with a bare {content_var} inside a logging call.
# ---------------------------------------------------------------------------
_FSTRING_CONTENT_IN_LOG = re.compile(
    _LOG_CALL + r"""\s*\([^)]*f['"][^'"]*\{""" + _CONTENT_VAR_ALT + r"(?![_\w(])",
    re.DOTALL,
)

# ---------------------------------------------------------------------------
# Pattern 2 — bare content-var positional arg in a logging call (full-file
# scan, balanced-paren matching for multiline calls).
# ---------------------------------------------------------------------------
_LOG_OPENER = re.compile(_LOG_CALL + r"\s*\(", re.DOTALL)
_STRING_LITERAL = re.compile(
    r'"[^"\\]*(?:\\.[^"\\]*)*"|\'[^\'\\]*(?:\\.[^\'\\]*)*\'',
    re.DOTALL,
)
_BARE_CONTENT_TOKEN = re.compile(r"(?<!\w)" + _CONTENT_VAR_ALT + r"(?!\w)")


def _extract_call_args(source: str, open_paren_pos: int) -> str:
    """Return the text between the opening paren at ``open_paren_pos`` and its
    matching closing paren (exclusive). Handles nested parens. Returns an
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


def _bare_content_in_log_violations(source: str) -> list[tuple[int, str]]:
    """Return (lineno, stripped_line) pairs where a bare content variable
    exists as a logging-call argument.

    Scans the full source using balanced-paren matching so multiline calls are
    handled correctly. String literals are stripped first so an English word
    match inside a log format-string can never trigger.
    """
    violations: list[tuple[int, str]] = []
    for m in _LOG_OPENER.finditer(source):
        open_pos = m.end() - 1  # position of the '(' character
        args_text = _extract_call_args(source, open_pos)
        sanitised = _STRING_LITERAL.sub("__STR__", args_text)
        if _BARE_CONTENT_TOKEN.search(sanitised):
            lineno = source.count("\n", 0, m.start()) + 1
            snippet = source.splitlines()[lineno - 1].strip()
            violations.append((lineno, snippet))
    return violations


# ---------------------------------------------------------------------------
# Meta-tests: verify guard regexes fire correctly and do not over-match
# ---------------------------------------------------------------------------


def test_guard_detects_fstring_chunk_text_in_log() -> None:
    """Guard must fire on a logger call that embeds {chunk_text} in an f-string."""
    content = '_logger.warning(f"text={chunk_text}")\n'
    assert _FSTRING_CONTENT_IN_LOG.search(content), (
        "Pattern failed to detect f-string with {chunk_text} in a _logger logging call"
    )


def test_guard_detects_fstring_content_logger_variant() -> None:
    """Guard must fire on logger. (no underscore) variant with f-string content."""
    content = 'logger.warning(f"summary={community_text}")\n'
    assert _FSTRING_CONTENT_IN_LOG.search(content), (
        "Pattern failed to detect f-string with {community_text} in a logger. logging call"
    )


def test_guard_detects_bare_chunk_text_arg() -> None:
    """Guard must fire on a logger call that passes chunk_text as a positional arg."""
    content = '_logger.warning("raw text: %s", chunk_text)\n'
    assert _bare_content_in_log_violations(content), (
        "Guard failed to detect bare 'chunk_text' positional arg in a logging call"
    )


def test_guard_detects_bare_chunk_texts_arg() -> None:
    """Guard must fire on the plural community-content parameter name too."""
    content = '_logger.warning("raw texts: %s", chunk_texts)\n'
    assert _bare_content_in_log_violations(content), (
        "Guard failed to detect bare 'chunk_texts' positional arg in a logging call"
    )


def test_guard_detects_bare_community_text_arg() -> None:
    """Guard must fire on the literal 'community_text' identifier."""
    content = '_logger.warning("raw community text: %s", community_text)\n'
    assert _bare_content_in_log_violations(content), (
        "Guard failed to detect bare 'community_text' positional arg in a logging call"
    )


def test_guard_detects_bare_content_logger_variant() -> None:
    """Guard must fire on logger. (no underscore) variant with bare content arg."""
    content = 'logger.warning("t=%s", chunk_text)\n'
    assert _bare_content_in_log_violations(content), (
        "Guard failed to detect bare 'chunk_text' in a logger. logging call"
    )


def test_guard_detects_multiline_logging_call() -> None:
    """Guard must fire on a multiline logging call with bare content arg."""
    content = textwrap.dedent(
        """\
        _logger.warning(
            "raw chunk: %s",
            chunk_text
        )
        """
    )
    assert _bare_content_in_log_violations(content), (
        "Guard failed to detect bare 'chunk_text' in a multiline logging call"
    )


def test_guard_ignores_entity_names() -> None:
    """Guard must NOT fire on already-abstracted entity-name arguments.

    Entity names (e.g. ``item.get("source_entity")``) are explicitly excluded
    from this guard — they are graph metadata, not raw input content, and
    the community builder intentionally logs them.
    """
    content = (
        '_logger.warning('
        '"LLM returned unknown relationship_type %r; skipping pair (%r, %r)", '
        'rel_type, item.get("source_entity"), item.get("target_entity"))\n'
    )
    assert not _FSTRING_CONTENT_IN_LOG.search(content), (
        "f-string pattern falsely matched an entity-name-only logging call"
    )
    assert not _bare_content_in_log_violations(content), (
        "bare-arg guard falsely matched an entity-name-only logging call"
    )


def test_guard_ignores_content_var_as_substring() -> None:
    """Guard must NOT fire on identifiers that contain a content var as a substring."""
    content = '_logger.warning("len=%s", truncated_chunk_text_preview)\n'
    assert not _FSTRING_CONTENT_IN_LOG.search(content), (
        "f-string pattern falsely matched 'truncated_chunk_text_preview' identifier"
    )
    assert not _bare_content_in_log_violations(content), (
        "bare-arg guard falsely matched 'truncated_chunk_text_preview' identifier"
    )


def test_guard_ignores_format_template_usage() -> None:
    """Guard must NOT fire on .format(chunk_text=chunk_text) — not a logging call."""
    content = textwrap.dedent(
        """\
        prompt = _LABEL_PROMPT_TEMPLATE.format(
            chunk_text=chunk_text,
            entity_pairs=pairs_text,
        )
        """
    )
    assert not _FSTRING_CONTENT_IN_LOG.search(content), (
        "f-string pattern falsely matched non-logging .format() code"
    )
    assert not _bare_content_in_log_violations(content), (
        "bare-arg guard falsely matched non-logging .format() code"
    )


def test_guard_ignores_non_log_calls() -> None:
    """Guard must NOT fire when a content var appears in non-logging code."""
    content = textwrap.dedent(
        """\
        truncated = chunk_text[:2000]
        prompt = _PROMPT_TEMPLATE.format(chunk_text=truncated)
        result = await client.generate(chunk_texts)
        """
    )
    assert not _FSTRING_CONTENT_IN_LOG.search(content), (
        "f-string pattern falsely matched non-logging code"
    )
    assert not _bare_content_in_log_violations(content), (
        "bare-arg guard falsely matched non-logging code"
    )


def test_guard_detects_content_in_warn_once() -> None:
    """``warn_once(...)`` is a logging call: a bare or f-string content argument in it is a violation."""
    bare = 'once_per_process.warn_once(_logger, KEY, "x %s", chunk_text)\n'
    fstring = 'warn_once(_logger, KEY, f"x {chunk_text}")\n'
    assert _bare_content_in_log_violations(bare)
    assert _FSTRING_CONTENT_IN_LOG.search(fstring)


# ---------------------------------------------------------------------------
# Real guard — reads the community builder and every llm_client module and asserts zero violations.
# ---------------------------------------------------------------------------


def _graph_client_modules() -> list[Path]:
    return [_COMMUNITY_BUILDER, *sorted(_LLM_CLIENT_DIR.glob("*.py"))]


def test_graph_client_path_modules_exist() -> None:
    """A moved or renamed module must not turn the guard below into a scan of nothing."""
    assert _COMMUNITY_BUILDER.is_file(), _COMMUNITY_BUILDER
    names = {path.name for path in _graph_client_modules()}
    assert {"ollama_client.py", "openai_client.py", "anthropic_client.py", "llama_cpp_client.py", "claude_cli_client.py"} <= names


def test_no_chunk_text_logged_in_graph_client_modules() -> None:
    """No module of the graph client path (the community builder and ``archon_search/llm_client/*.py``) may pass raw
    chunk/community text directly to a logging call.

    Only already-abstracted metadata (entity names, counts, model names) may be logged. On failure the assertion names
    the offending file and line for quick triage.
    """
    violations: list[str] = []
    for path in _graph_client_modules():
        source = path.read_text(encoding="utf-8")

        for m in _FSTRING_CONTENT_IN_LOG.finditer(source):
            lineno = source.count("\n", 0, m.start()) + 1
            snippet = source.splitlines()[lineno - 1].strip()
            violations.append(f"  {path.name}:{lineno} (f-string): {snippet}")

        for lineno, snippet in _bare_content_in_log_violations(source):
            violations.append(f"  {path.name}:{lineno} (bare arg): {snippet}")

    assert not violations, (
        "Raw chunk/community text passed directly to a logging call in the graph client path:\n"
        + "\n".join(violations)
        + "\nLog only already-abstracted metadata (entity names, counts, model names) — "
        "never chunk_text, chunk_texts, or community_text."
    )
