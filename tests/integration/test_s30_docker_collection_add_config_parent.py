"""Regression: `collection add` inside the Docker container 500s (S30/S40/S31).

Root cause: in the container image ``HOME=/data`` and ``ARCHON_SEARCH_DATA_DIR=/data``
but ``ARCHON_SEARCH_CONFIG`` is unset, so ``get_default_config_path()`` resolves to
``/data/.archon-search/archon-search.toml`` — a path whose parent directory
(``/data/.archon-search/``) is never created (the runtime data dir is ``/data``
itself).  ``POST /collections/`` calls ``_maybe_save_config`` → ``save_config``,
which ``write_text``s without creating the parent, raising ``FileNotFoundError``.
The exception is unhandled in ``add_collection`` (line ~196, before the ingest job
is enqueued), so the route returns a bare Starlette 500 "Internal Server Error".
Every downstream S30/S31/S40 search then 404s because the collection was never
registered.

This test reproduces the server-side 500 at the code level (no Docker required)
by pointing ``app.state.config_path`` at a config file whose parent directory
does not yet exist, exactly as it is in the shipped container.
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

    # Mirror the container: config path lives under a `.archon-search/` subdir of
    # the data dir that nothing has created yet.
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
