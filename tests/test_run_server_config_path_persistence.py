"""Regression tests for S07 / S252 — the shared config-path-persistence bug.

Root cause (fixed): production ``run_server(config)``
(``archon_search/server/app.py``) built the app via
``create_app(config, job_store, scheduler=scheduler)`` WITHOUT threading the
config file path through. ``create_app`` therefore set
``app.state.config_path = None``, which made ``_maybe_save_config`` in
``routes_collections.py`` a silent no-op. Net effect: ``POST /collections/``
appended the path to the in-memory ``config.collections`` but never wrote the
TOML — so on restart the path was lost and ``GET /collections/{name}`` /
``GET /status`` reported ``"path": ""``.

The tests build the app through the REAL production seam (``run_server`` with
``uvicorn.run`` patched to a no-op) so the wiring is exercised exactly as it
ships. ``ARCHON_SEARCH_CONFIG`` / ``ARCHON_SEARCH_DATA_DIR`` point at
``tmp_path``, so the TOML ``run_server`` resolves via ``get_default_config_path()``
lands in a temp dir — the real ``~/.archon-search`` is never touched.
"""
from __future__ import annotations

import logging
from collections.abc import Iterator
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

    Failed before the fix because ``config_path`` was ``None`` → ``_maybe_save_config``
    no-oped → the TOML was never written (the S07 user-visible symptom)."""
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


@pytest.fixture
def _restore_archon_logger() -> Iterator[None]:
    """``run_server`` reconfigures the ``archon_search`` logger; put its handlers back afterwards."""
    logger = logging.getLogger("archon_search")
    saved = (logger.handlers[:], logger.level, logger.propagate)
    yield
    for handler in logger.handlers[:]:
        logger.removeHandler(handler)
        if handler not in saved[0]:
            handler.close()
    for handler in saved[0]:
        logger.addHandler(handler)
    logger.setLevel(saved[1])
    logger.propagate = saved[2]


@pytest.mark.usefixtures("_restore_archon_logger")
@pytest.mark.parametrize("explicit", [False, True], ids=["default-config", "explicit-config"])
def test_run_server_logs_legacy_config_warning_after_logging_is_configured(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, explicit: bool
) -> None:
    """The ignored-legacy-config WARNING goes through the configured pipeline (lands in ``log_file``),
    and is skipped when an explicit config path was given."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("ARCHON_SEARCH_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.delenv("ARCHON_SEARCH_CONFIG", raising=False)
    legacy = tmp_path / ".archon-search" / "archon-search.toml"
    legacy.parent.mkdir()
    legacy.write_text("[server]\n", encoding="utf-8")
    log_path = tmp_path / "logs" / "server.log"
    cfg = _make_config(tmp_path)
    cfg.log_file = str(log_path)
    monkeypatch.setattr(app_module.uvicorn, "run", lambda app, **kwargs: None)

    app_module.run_server(cfg, tmp_path / "explicit.toml" if explicit else None)

    for handler in logging.getLogger("archon_search").handlers:
        handler.flush()
    assert (str(legacy) in log_path.read_text(encoding="utf-8")) is not explicit


def test_run_server_defaults_config_path_under_data_dir(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """No config path + ARCHON_SEARCH_DATA_DIR set (container /data volume) → the app persists to
    ``<data-dir>/archon-search.toml``."""
    data_dir = tmp_path / "data"
    monkeypatch.setenv("ARCHON_SEARCH_DATA_DIR", str(data_dir))
    monkeypatch.delenv("ARCHON_SEARCH_CONFIG", raising=False)
    captured: dict[str, object] = {}
    monkeypatch.setattr(app_module.uvicorn, "run", lambda app, **kwargs: captured.setdefault("app", app))

    app_module.run_server(_make_config(tmp_path))

    assert Path(captured["app"].state.config_path) == data_dir / "archon-search.toml"
