"""A5b Task 2.4 — CI guard: no f-string SQL in the store layer.

A parametrized default-tier guard test reads each file in :data:`_GUARDED_FILES`
(``store.py``, ``graph_store.py``) as text and asserts
:func:`_scan_for_fstring_sql` finds zero violations. The seven patterns in
:data:`_PATTERNS` are pinned by ``test_pattern_id_set_is_pinned``, which is the
authoritative count. The meta-tests below exercise the same scan function the
real guard uses — positively (it fires on synthetic violations, with the correct
pattern id and line number), negatively (it stays silent on the sanctioned
helper forms), and structurally (a deleted pattern fails loudly).

Accepted blind spots — this is a text scan, not a taint analysis. It cannot see:

- an f-string bound to a local and passed later (``pred = f"x = '{v}'"`` then
  ``table.where(pred)``) — an idiom ``store.py`` itself uses;
- ``+`` concatenation (``"x = '" + v + "'"``), ``%`` formatting, ``.format()``;
- any predicate assembled by a helper the scan does not know about;
- anything in ``store_filters.py``, which is therefore deliberately NOT in
  :data:`_GUARDED_FILES`. It is a pure builder with no LanceDB call site at all —
  no ``.where(`` / ``where=`` / ``filter=`` / ``.delete(`` / ``.count_rows(``
  anywhere — so every pattern here is structurally unable to fire on it, and
  listing it would claim coverage that does not exist. Its one predicate
  f-string (``_where_list_has_or_null``) is a ``return f"…"`` factory, textually
  indistinguishable from the *sanctioned* ``_where_eq`` / ``_where_in`` factories
  at ``store.py:155`` and ``graph_store.py:139``; a ``return``-based pattern would
  flag all three and could only be made to pass via the exemption mechanism this
  guard deliberately does not have. ``store_filters.py`` is covered instead by
  its own unit tests of ``_sql_quote_str`` / ``build_where``.
  (Supersedes the conditional note in
  ``Documentation/Completed/e2a-ttl-scoping-team-plan.md`` BE-9: the guard is not
  extended to that file, so the helper needs neither an exemption nor a
  concatenation rewrite.);
- **implicit string concatenation** — ``table.where("a = 1 " f"AND b='{y}'")``.
  The plain literal sits between the paren and the f-string, and :data:`_GAP`
  deliberately does not span string literals: doing so would make every
  ``where(...)`` call swallow arbitrary text up to the next f-string.

Closing those requires an AST taint walk from user input to the LanceDB call,
deliberately not done here: the guard's job is to stop the *inline* f-string
predicate from reappearing.

Accepted false positives — flagged even though harmless, accepted rather than
papered over with an exemption mechanism:

- a comment or docstring that merely quotes ``where=f"…"``;
- a non-SQL local that happens to be named ``where`` or ``filter``
  (``where = f"{a}"``, ``self.filter = f"{n}"``). No guarded file has one.
"""
from __future__ import annotations

import re
import textwrap
from pathlib import Path
from typing import NamedTuple

import pytest

# ---------------------------------------------------------------------------
# Patterns the guard watches for — a named mapping so meta-tests can assert
# WHICH pattern fired, and so a deleted pattern fails the membership test.
# ---------------------------------------------------------------------------

# Every f-string prefix spelling Python accepts, immediately followed by the
# opening quote. Requiring the quote is what keeps ``.format(`` and
# ``filter=fn(...)`` from matching.
_FS = r"(?:[rR][fF]|[fF][rR]?)[\"']"

# What may sit between a call's opening paren (or a ``kwarg=``) and the f-string
# and still leave the f-string as that argument's value: whitespace (including
# newlines, so multiline call sites are caught), redundant wrapping parens, and
# comments.
_GAP = r"(?:[\s(]|#[^\n]*)*"

# ``updates_sql`` / ``add_columns`` values are RAW SQL expressions held in a dict
# literal, so the f-string is nested rather than adjacent. Scanning forward over
# "anything but a closing paren" keeps the match inside the enclosing call while
# crossing any depth of nested braces — a brace-counting ``[^}]*`` is bypassed by
# the first nested dict.
_IN_CALL = r"[^)]*?"

_PATTERNS: dict[str, re.Pattern[str]] = {
    # Positional predicate: table.where(f"..."), .delete(f"..."), .count_rows(f"...").
    "where_positional": re.compile(r"\.where\(" + _GAP + _FS),
    "delete_positional": re.compile(r"\.delete\(" + _GAP + _FS),
    "count_rows_positional": re.compile(r"\.count_rows\(" + _GAP + _FS),
    # Keyword forms: the f-string is not adjacent to the method's opening paren,
    # so the positional patterns above cannot see them.
    "where_kwarg": re.compile(r"\bwhere\s*=" + _GAP + _FS),
    # LanceDB spells the count_rows predicate kwarg ``filter``, not ``where``.
    "filter_kwarg": re.compile(r"\bfilter\s*=" + _GAP + _FS),
    # table.update(updates_sql={"col": f"..."}) takes RAW SQL expressions (unlike
    # ``updates``, which binds literals).
    "updates_sql_kwarg": re.compile(r"\bupdates_sql\s*=" + _IN_CALL + _FS),
    # table.add_columns({"col": "cast(0 as bigint)"}) — the dict values are raw
    # SQL expressions evaluated per row, exactly like ``updates_sql``.
    "add_columns_sql": re.compile(r"\.add_columns\(" + _IN_CALL + _FS),
}

_GUARDED_FILES = ("store.py", "graph_store.py")

_FSTRING_PREFIXES = ("f", "F", "rf", "rF", "fr", "fR", "Rf", "RF", "Fr", "FR")


class Violation(NamedTuple):
    pattern_id: str
    lineno: int
    snippet: str


def _scan_for_fstring_sql(source: str) -> list[Violation]:
    """Return every f-string SQL violation in *source*, with 1-based line numbers.

    The reported line is the f-string's own line, not the line of the enclosing
    ``.where(`` — that is what a contributor needs to edit. ``split("\\n")``
    rather than ``splitlines()`` keeps the index consistent with ``count("\\n")``
    (``splitlines`` also breaks on form feed, ``\\r`` and ``\\x85``).
    """
    lines = source.split("\n")
    violations: list[Violation] = []
    for pattern_id, pattern in _PATTERNS.items():
        for match in pattern.finditer(source):
            lineno = source.count("\n", 0, match.end()) + 1
            violations.append(Violation(pattern_id, lineno, lines[lineno - 1].strip()))
    return violations


# ---------------------------------------------------------------------------
# Meta-tests: verify the scan behaves correctly
# ---------------------------------------------------------------------------


def test_pattern_id_set_is_pinned() -> None:
    """Deleting or renaming a pattern must fail here, not silently weaken the guard."""
    assert set(_PATTERNS) == {
        "where_positional",
        "delete_positional",
        "count_rows_positional",
        "where_kwarg",
        "filter_kwarg",
        "updates_sql_kwarg",
        "add_columns_sql",
    }


def test_scan_returns_nothing_for_clean_source() -> None:
    source = textwrap.dedent(
        """\
        pred = _where_eq("chunk_id", chunk_id)
        await table.update(where=pred, updates=vals)
        count = await table.count_rows(_where_eq("doc_id", doc_id))
        """
    )
    assert _scan_for_fstring_sql(source) == []


def test_scan_reports_pattern_id_and_line_number() -> None:
    """The scan must name the offending pattern and the exact line."""
    source = textwrap.dedent(
        """\
        x = 1
        y = 2
        await table.delete(f"doc_id = '{doc_id}'")
        """
    )
    assert _scan_for_fstring_sql(source) == [
        Violation("delete_positional", 3, """await table.delete(f"doc_id = '{doc_id}'")"""),
    ]


@pytest.mark.parametrize(
    ("pattern_id", "source"),
    [
        ("where_positional", """table.where(f"x = '{y}'")\n"""),
        ("delete_positional", """table.delete(f"x = '{y}'")\n"""),
        ("count_rows_positional", """table.count_rows(f"x = '{y}'")\n"""),
        ("where_kwarg", """table.update(where=f"chunk_id = '{cid}'", updates=vals)\n"""),
        ("filter_kwarg", """table.count_rows(filter=f"src = '{s}'")\n"""),
        ("updates_sql_kwarg", """table.update(where=w, updates_sql={"n": f"n + {d}"})\n"""),
        ("add_columns_sql", """table.add_columns({"ns": f"'{DEFAULT_NAMESPACE}'"})\n"""),
        # Nested dict before the f-string: a brace-scanning [^}]* pattern is
        # disarmed by the first `}`, so these must stay caught.
        (
            "updates_sql_kwarg",
            """table.update(where=w, updates_sql={"meta": {"a": 1}, "n": f"n + {d}"})\n""",
        ),
        (
            "updates_sql_kwarg",
            'table.update(\n    where=w,\n    updates_sql={\n        "cnt": {"z": 0},\n'
            '        "n": f"n+{d}",\n    },\n)\n',
        ),
        (
            "add_columns_sql",
            """table.add_columns({"meta": {"a": 1}, "ns": f"'{ns}'"})\n""",
        ),
        # Redundant wrapping parens keep the f-string as the argument value.
        ("where_positional", """table.where((f"x = '{y}'"))\n"""),
        ("where_kwarg", """table.update(where=(f"x = '{y}'"))\n"""),
        # A comment between the paren and the f-string.
        ("where_positional", """q.where(  # predicate\n    f"x = '{y}'"\n)\n"""),
    ],
)
def test_each_pattern_fires_on_its_own_violation(pattern_id: str, source: str) -> None:
    fired = {v.pattern_id for v in _scan_for_fstring_sql(source)}
    assert pattern_id in fired, f"pattern {pattern_id!r} failed to fire on {source!r}"


@pytest.mark.parametrize("prefix", _FSTRING_PREFIXES)
def test_all_fstring_prefix_spellings_are_caught(prefix: str) -> None:
    """``rf"`` / ``F"`` / ``Rf"`` and friends must not slip past the guard."""
    source = f"""table.where({prefix}"x = '{{y}}'")\n"""
    assert _scan_for_fstring_sql(source), f"prefix {prefix!r} bypassed the guard"


def test_scan_detects_multiline_violation() -> None:
    r"""``.where(`` and the f-string on separate lines must still be caught.

    ``_GAP`` matches newlines, so no ``re.DOTALL`` is needed — DOTALL only
    affects ``.``, which no pattern here contains. The reported line is the
    f-string's line (2), not the ``.where(`` line (1).
    """
    source = textwrap.dedent(
        """\
        await table.where(
                f"x = '{y}'"
            )
        """
    )
    assert _scan_for_fstring_sql(source) == [
        Violation("where_positional", 2, 'f"x = \'{y}\'"'),
    ]


@pytest.mark.parametrize(
    "source",
    [
        'await table.update(where=_where_eq("chunk_id", chunk_id), updates=vals)\n',
        'await table.delete(_where_in("chunk_id", chunk_ids))\n',
        '@router.delete("/{name}")\n',
        'return f"{col} = {_sql_quote_str(value)}"\n',
        """await table.where("x = '{}'".format(y))\n""",
        "count = await table.count_rows(filter=build_predicate(ns))\n",
        'await table.update(where=pred, updates_sql={"n": bump_expr})\n',
        'await table.add_columns({"schema_version": "cast(0 as bigint)"})\n',
        'await table.add_columns(pa.field("source_path", pa.utf8(), nullable=True))\n',
    ],
)
def test_scan_ignores_sanctioned_forms(source: str) -> None:
    assert _scan_for_fstring_sql(source) == [], f"false positive on {source!r}"


# ---------------------------------------------------------------------------
# Real guard — reads each guarded file as a single string, asserts zero violations.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("filename", _GUARDED_FILES)
def test_no_fstring_sql_in_guarded_file(filename: str) -> None:
    """Guarded store-layer modules must contain zero inline f-string SQL predicates.

    On failure the message names the pattern and line number so a contributor
    sees exactly where the violation is.
    """
    path = Path(__file__).parent.parent / "archon_search" / filename
    violations = _scan_for_fstring_sql(path.read_text(encoding="utf-8"))

    assert not violations, (
        f"F-string SQL violations found in archon_search/{filename}:\n"
        + "\n".join(f"  {v.pattern_id} line {v.lineno}: {v.snippet}" for v in violations)
        + "\nReplace with _where_eq / _where_in helpers."
    )
