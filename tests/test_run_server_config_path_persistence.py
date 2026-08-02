"""Regression tests for S07 / S252 — the shared config-path-persistence bug.

Root cause (already diagnosed): production ``run_server(config)``
(``archon_search/server/app.py``) builds the app via
``create_app(config, job_store, scheduler=scheduler)`` WITHOUT threading the
config file path through. ``create_app`` therefore sets
``app.state.config_path = None``, which makes ``_maybe_save_config`` in
``routes_collections.py`` a silent no-op. Net effect: ``POST /collections/``
appends the path to the in-memory ``config.collections`` but never writes the
TOML — so on restart the path is lost and ``GET /collections/{name}`` /
``GET /status`` report ``"path": ""``.

Both tests build the app through the REAL production seam (``run_server`` with
``uvicorn.run`` patched to a no-op) so the wiring defect is exercised exactly as
it ships. ``ARCHON_SEARCH_CONFIG`` is pointed at ``tmp_path`` so the fix (which
resolves the config path via ``get_default_config_path()`` regardless of its
final shape) lands the TOML in a temp dir — the real ``~/.archon-search`` is
never touched.
"""
from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import archon_search.server.app as app_module
from archon_search.config import SearchConfig, load_config

TEST_API_KEY = "0" * 64  # matches tests/conftest.py ARCHON_SEARCH_API_KEY


def _make_config(tmp_path: Path) -> SearchConfig:
    cfg = SearchConfig()
    cfg.host = "127.0.0.1"
    cfg.port = 0
    # Isolate the store to tmp so the test never reads/writes the real
    # ~/.archon-search — a raw SearchConfig() otherwise defaults db_path there.
    cfg.db_path = str(tmp_path / "search")
    # Keep the app lean/deterministic for the TestClient lifespan.
    cfg.mcp.enabled = False
    return cfg


def _build_app_via_run_server(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, toml_path: Path
):
    """Run the production ``run_server`` path with uvicorn stubbed, returning
    the FastAPI app it hands to ``uvicorn.run`` (captured, never served)."""
    monkeypatch.setenv("ARCHON_SEARCH_CONFIG", str(toml_path))

    captured: dict[str, object] = {}

    def _fake_uvicorn_run(app, **kwargs):  # noqa: ANN001, ANN003
        captured["app"] = app

    monkeypatch.setattr(app_module.uvicorn, "run", _fake_uvicorn_run)

    app_module.run_server(_make_config(tmp_path))
    assert "app" in captured, "run_server never called uvicorn.run"
    return captured["app"]


def test_run_server_threads_config_path_into_app(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The app built by ``run_server`` must carry a non-None ``config_path``
    pointing at the resolved TOML — otherwise ``_maybe_save_config`` no-ops and
    ``collection add`` never persists (S07 / S252 root cause)."""
    toml_path = tmp_path / "archon-search.toml"

    app = _build_app_via_run_server(monkeypatch, tmp_path, toml_path)

    assert app.state.config_path is not None, (
        "run_server built the app with config_path=None — _maybe_save_config "
        "will silently no-op and collection paths are never persisted (S07/S252)"
    )
    assert Path(app.state.config_path).resolve() == toml_path.resolve()


def test_add_collection_persists_path_to_toml_via_production_build(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """End-to-end: driving ``POST /collections/`` on an app built the way
    production builds it must write the added path into the on-disk TOML.

    Fails today because ``config_path is None`` → ``_maybe_save_config`` no-ops
    → the TOML is never written (the S07 user-visible symptom)."""
    toml_path = tmp_path / "archon-search.toml"
    collection_dir = tmp_path / "my_docs"
    collection_dir.mkdir()

    app = _build_app_via_run_server(monkeypatch, tmp_path, toml_path)

    with TestClient(app) as client:
        resp = client.post(
            "/collections/",
            json={"path": str(collection_dir)},
            headers={"Authorization": f"Bearer {TEST_API_KEY}"},
        )
    assert resp.status_code == 202, resp.text

    assert toml_path.exists(), (
        "TOML was never written — config_path was None so _maybe_save_config "
        "no-opped (S07). On restart the collection path is lost."
    )
    # Prove the actual restart symptom is fixed: reloading the persisted TOML
    # (what a server restart does) must yield the added collection in
    # config.collections — a raw substring check would pass even if the path
    # landed under a wrong key or a comment and never round-tripped.
    reloaded = load_config(toml_path)
    assert str(collection_dir.resolve()) in reloaded.collections, (
        "Added collection path did not round-trip through the persisted TOML — "
        "on restart the collection would be missing (S07/S252)."
    )
