"""The search-support modules moved to ``archon_search.services``; no alias stays behind."""
from __future__ import annotations

import importlib

import pytest


@pytest.mark.parametrize("module", ["_search_budget", "_hyde", "_validators"])
def test_old_server_search_support_modules_do_not_import(module: str) -> None:
    """A stale import path must fail loudly rather than resolve through a shim."""
    with pytest.raises(ModuleNotFoundError) as exc:
        importlib.import_module(f"archon_search.server.{module}")
    assert exc.value.name == f"archon_search.server.{module}"
