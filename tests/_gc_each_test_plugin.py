"""Opt-in pytest plugin that blames a leak on the test that made it: ``uv run pytest -p tests._gc_each_test_plugin``.

pytest reports an unraisable exception (a ``ResourceWarning`` from an unclosed transport, an error in a ``__del__``) in
the test that is running when the garbage collector happens to finalise the object, and it only forces a collection at
the end of the session. A leak from test A therefore fails test B, at a different place on every run, which is how a
flake that no loop of the failing test reproduces looks. This plugin collects twice after every test's teardown and
reports what that raised at once, against the test that leaked. A full run with it costs about five times the usual
wall-clock time: use it to hunt a flake, not routinely.
"""

from __future__ import annotations

import gc

import pytest
from _pytest.unraisableexception import collect_unraisable

_COLLECTIONS_PER_TEST = 2  # a finaliser may free a second object that only the first collection made unreachable


@pytest.hookimpl(hookwrapper=True, trylast=True)
def pytest_runtest_teardown(item: pytest.Item, nextitem: pytest.Item | None):
    yield
    for _ in range(_COLLECTIONS_PER_TEST):
        gc.collect()
    collect_unraisable(item.config)
