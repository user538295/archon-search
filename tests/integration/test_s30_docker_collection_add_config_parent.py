"""Regression: `collection add` inside the Docker container 500s (S30/S40/S31).

Original root cause: in the container image ``HOME=/data`` and
``ARCHON_SEARCH_DATA_DIR=/data`` but ``ARCHON_SEARCH_CONFIG`` was unset, and
``get_default_config_path()`` then resolved to ``/data/.archon-search/archon-search.toml``
— a path whose parent directory was never created. ``POST /collections/`` calls
``_maybe_save_config`` → ``save_config``, which wrote without creating the parent,
raising ``FileNotFoundError``; the route returned a 500 and every downstream
S30/S31/S40 search 404'd because the collection was never registered.

The default now follows the data dir (``<data dir>/archon-search.toml``), but an
``ARCHON_SEARCH_CONFIG`` / ``--config`` parent is still not guaranteed to exist, so
``save_config`` must keep creating it. This test reproduces the scenario at the code
level (no Docker required) by pointing ``app.state.config_path`` at a config file
whose parent directory does not yet exist.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from tests.integration.conftest import make_real_app


def test_collection_add_creates_missing_config_parent_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """POST /collections/ must not 500 when the config file's parent dir is missing."""
    corpus = tmp_path / "testdocs"
    corpus.mkdir()
    (corpus / "alpha.md").write_text("# Alpha\nThe quick brown fox.\n", encoding="utf-8")
    (corpus / "beta.md").write_text("# Beta\nPython language.\n", encoding="utf-8")

    # A config path whose parent directory nothing has created yet.
    missing_parent_config = tmp_path / ".archon-search" / "archon-search.toml"
    assert not missing_parent_config.parent.exists()

    with make_real_app(tmp_path, monkeypatch) as (client, _cfg, api_key):
        client.app.state.config_path = str(missing_parent_config)

        resp = client.post(
            "/collections/",
            json={"path": str(corpus)},
            headers={"Authorization": f"Bearer {api_key}"},
        )

    assert resp.status_code == 202, (
        f"collection add returned {resp.status_code} (expected 202): {resp.text}"
    )
    # The config must have been persisted (parent dir created), which is what
    # makes the collection survive a container restart (S31).
    assert missing_parent_config.exists(), "save_config did not create the config file"
    assert str(corpus) in missing_parent_config.read_text(encoding="utf-8")
