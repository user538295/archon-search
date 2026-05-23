"""A5b contract test: verifies _sql_quote_str exists in store_filters (A2 canary).

If A2 renames _sql_quote_str or changes its signature, this test fails with
a clear ImportError at collection time, naming A2 as the blocker before any
A5b implementation code is touched.
"""
from __future__ import annotations

import inspect

# Top-level import: if A2 renames or removes _sql_quote_str, pytest collection
# fails here with ModuleNotFoundError / ImportError naming the A2 symbol.
from archon_search.store_filters import _sql_quote_str


def test_sql_quote_str_importable() -> None:
    """_sql_quote_str is importable from archon_search.store_filters (implicit via module import)."""
    assert callable(_sql_quote_str)


def test_sql_quote_str_signature() -> None:
    """_sql_quote_str takes exactly one parameter (str) and returns str."""
    sig = inspect.signature(_sql_quote_str)
    params = list(sig.parameters.values())
    assert len(params) == 1, f"Expected 1 parameter, got {len(params)}: {params}"
    # Return annotation must be str (or the string 'str' with __future__ annotations)
    ret = sig.return_annotation
    if ret is not inspect.Parameter.empty:
        assert ret is str or ret == "str", (
            f"Return annotation should be str, got {ret!r}"
        )


def test_sql_quote_str_doubles_single_quote() -> None:
    """_sql_quote_str doubles embedded single quotes (SQL-standard quoting convention)."""
    result = _sql_quote_str("foo'bar")
    assert result == "'foo''bar'", (
        f"Expected \"'foo''bar'\", got {result!r} — "
        "A5b relies on the doubling-quote convention"
    )
