"""Best-effort ``/status`` lookups must not exit when no API key is available, and the shared helpers stay single-sourced (FE-2)."""
from __future__ import annotations

import importlib

import pytest

from archon_search.cli import _helpers, backup_cmd, maintenance_cmd
from archon_search.cli._helpers import _DEFAULT_API_URL

_SHARED_HELPER_MODULES = [
    "backup_cmd",
    "collection",
    "export_cmd",
    "graph_cmd",
    "ingest",
    "jobs_cmd",
    "key_cmd",
    "maintenance_cmd",
    "status",
    "sync",
]


@pytest.mark.parametrize("module", [backup_cmd, maintenance_cmd], ids=["backup", "maintenance"])
def test_best_effort_key_lookup_returns_none_without_key(module, monkeypatch: pytest.MonkeyPatch) -> None:
    """No key means ``None``, not ``SystemExit`` — the offline fallback to local state stays reachable."""
    monkeypatch.delenv("ARCHON_SEARCH_API_KEY", raising=False)
    monkeypatch.setattr(module, "load_key", lambda: None)
    monkeypatch.setattr(_helpers, "load_key", lambda: None)
    assert module._fetch_server_status(_DEFAULT_API_URL, None) is None


@pytest.mark.parametrize("error", [OSError("unreadable"), ValueError("malformed")], ids=["oserror", "valueerror"])
@pytest.mark.parametrize("module", [backup_cmd, maintenance_cmd], ids=["backup", "maintenance"])
def test_best_effort_key_lookup_returns_none_on_key_file_error(
    module, error: Exception, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _raise() -> str:
        raise error

    monkeypatch.setattr(module, "load_key", _raise)
    assert module._fetch_server_status(_DEFAULT_API_URL, None) is None


@pytest.mark.parametrize("name", _SHARED_HELPER_MODULES)
def test_cli_modules_share_the_helpers_names(name: str) -> None:
    """No module re-grows a local ``_DEFAULT_API_URL`` or ``_resolve_api_key``."""
    module = importlib.import_module(f"archon_search.cli.{name}")
    for attr in ("_DEFAULT_API_URL", "_resolve_api_key"):
        if hasattr(module, attr):
            assert getattr(module, attr) is getattr(_helpers, attr), f"{name}.{attr} is a local copy"
