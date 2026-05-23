"""A5b Task 2.0 — Import contract assertions for _sql_quote_str.

Acts as a canary: if A2 renames _sql_quote_str or changes its signature,
this test fails before any A5b implementation code is touched.
"""
from __future__ import annotations

import inspect

from archon_search.store_filters import _sql_quote_str


def test_sql_quote_str_importable() -> None:
    """Implicit via module-level import; ImportError at collection names A2 as blocker."""
    assert callable(_sql_quote_str)


def test_sql_quote_str_signature() -> None:
    """Exactly one parameter, annotation str (or empty); return annotation str.

    Accepts both 'str' (string form from __future__ annotations) and str (class).
    """
    sig = inspect.signature(_sql_quote_str)
    params = list(sig.parameters.values())
    assert len(params) == 1, f"Expected 1 parameter, got {len(params)}"
    ann = params[0].annotation
    assert ann in (str, "str", inspect.Parameter.empty), (
        f"Expected str annotation on parameter, got {ann!r}"
    )
    ret = sig.return_annotation
    assert ret in (str, "str"), (
        f"Expected return annotation str, got {ret!r}"
    )


def test_sql_quote_str_doubles_single_quote() -> None:
    """Single-quote doubling convention that A5b relies on."""
    assert _sql_quote_str("foo'bar") == "'foo''bar'"
