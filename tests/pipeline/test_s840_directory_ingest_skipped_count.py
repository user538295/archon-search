"""S840: a directory ingest logs exactly one INFO line with the count of files skipped in denied locations.

``Documentation/Architecture/150_security_and_privacy_architecture.md:172``: directory ingest "logs one INFO line with
the count skipped in denied locations". A denied location is an ``IngestPathPolicy.check_secrets`` refusal -- including
credential files and directories that are hidden by name (``.env``, ``.ssh/``, ``.aws/``). Files under the deliberately
unlisted ``.git`` / ``node_modules`` / ``venv`` / ``__pycache__`` and non-secret hidden files are not counted.
"""
from __future__ import annotations

import logging

import pytest

from .conftest import make_pipeline

_BODY = "# Note\n\nSome searchable text that is long enough to chunk.\n" * 3
_NORMAL = ["a.md", "b.md", "docs/c.md"]
_DENIED_VISIBLE = ["server.pem", "id_rsa", "docs/other.pem"]
_DENIED_HIDDEN = [".env", ".netrc", ".ssh/id_rsa", ".ssh/config", ".aws/credentials", ".gnupg/pubring.kbx"]
_NOT_COUNTED = [
    ".git/objects/x.md",
    "node_modules/p/f.md",
    "venv/lib/v.md",
    "__pycache__/m.pyc",
    ".hidden.md",
    ".config/other/b.md",
]
_EXPECTED_SKIPPED = len(_DENIED_VISIBLE) + len(_DENIED_HIDDEN)


@pytest.mark.asyncio
async def test_directory_ingest_logs_exactly_one_info_line_counting_every_denied_file(
    connected_store, col_name, tmp_path, caplog
):
    for relative in (*_NORMAL, *_DENIED_VISIBLE, *_DENIED_HIDDEN, *_NOT_COUNTED):
        target = tmp_path / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(_BODY, encoding="utf-8")
    pipeline = make_pipeline(connected_store)

    with caplog.at_level(logging.INFO, logger="archon_search"):
        results = await pipeline.ingest_directory(
            tmp_path, col_name, embedder=pipeline._global_embedder, rebuild_fts=False
        )

    assert [r.status for r in results] == ["ok"] * len(_NORMAL)
    skipped_lines = [
        r for r in caplog.records
        if r.name.startswith("archon_search") and r.levelno == logging.INFO and "skipped" in r.getMessage().lower()
    ]
    assert len(skipped_lines) == 1, [r.getMessage() for r in skipped_lines]
    message = skipped_lines[0].getMessage()
    assert f" {_EXPECTED_SKIPPED} " in f" {message} ", message


def test_planted_probe_symlink_never_makes_a_hidden_directory_indexable(tmp_path):
    from archon_search._path_safety import IngestPathPolicy
    from archon_search.pipeline import _discover_ingest_files

    (tmp_path / ".ssh").mkdir()
    (tmp_path / ".ssh" / "config").write_text("x", encoding="utf-8")
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "probe").symlink_to(tmp_path / ".ssh")
    (tmp_path / ".git" / "config").write_text("[remote]", encoding="utf-8")
    (tmp_path / ".env").mkdir()
    (tmp_path / ".env" / "lib.md").write_text("x", encoding="utf-8")
    (tmp_path / "a.md").write_text("x", encoding="utf-8")

    files = _discover_ingest_files(tmp_path, "**/*", IngestPathPolicy(), 100)

    assert files == [tmp_path / "a.md"]
