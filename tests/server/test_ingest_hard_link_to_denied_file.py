"""S844: a symlink, a hard link or a different letter case does not bypass the ingest deny list.

SecurityGuide/06_hardening_checklist.md:92 — "Matching is by device and inode, so a symlink, a hard link or a
different letter case does not bypass it". Drives the real ASGI app with real files and links.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from archon_search.config import SearchConfig
from archon_search.jobs.store import JobStore
from archon_search.server.app import create_app
from tests._collection_access_stub import allow_collection_access

_AUTH = {"Authorization": f"Bearer {'0' * 64}"}  # tests/conftest.py ARCHON_SEARCH_API_KEY
_BAD_REQUEST = 400
_ACCEPTED = 202
_DENIED_DETAIL = "path is unsafe: denied_location"


@pytest.fixture
def data_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    directory = tmp_path / "data"
    directory.mkdir()
    monkeypatch.setenv("ARCHON_SEARCH_DATA_DIR", str(directory))
    return directory


@pytest.fixture
def client(tmp_path: Path, data_dir: Path) -> TestClient:
    config = SearchConfig()
    config.db_path = str(data_dir / "search")
    return TestClient(allow_collection_access(create_app(config, JobStore(path=data_dir / "jobs.json"))))


def _ingest(client: TestClient, path: Path):
    return client.post("/ingest", json={"collection": "c", "path": str(path)}, headers=_AUTH)


def _assert_denied(response) -> None:
    assert response.status_code == _BAD_REQUEST, response.text
    assert response.json() == {"detail": _DENIED_DETAIL}


@pytest.mark.parametrize("state_file", [".search.env", "archon-search.toml", "keys.json", ".secrets.env", "archon-search-jobs.json"])
def test_hard_link_to_a_server_state_file_is_refused(client: TestClient, tmp_path: Path, data_dir: Path, state_file: str) -> None:
    secret = data_dir / state_file
    secret.write_text("ARCHON_SEARCH_API_KEY=" + "c" * 64, encoding="utf-8")
    hard = tmp_path / "docs" / "hard.md"
    hard.parent.mkdir()
    os.link(secret, hard)

    _assert_denied(_ingest(client, hard))


def test_symlink_to_a_credential_file_is_refused(client: TestClient, tmp_path: Path) -> None:
    secret = tmp_path / ".env"
    secret.write_text("TOKEN=abc", encoding="utf-8")
    (tmp_path / "sym.md").symlink_to(secret)

    _assert_denied(_ingest(client, tmp_path / "sym.md"))


def test_different_letter_case_of_a_credential_file_is_refused(client: TestClient, tmp_path: Path) -> None:
    (tmp_path / ".env").write_text("TOKEN=abc", encoding="utf-8")

    _assert_denied(_ingest(client, tmp_path / ".ENV"))


def test_hard_link_to_a_credential_named_file_is_refused(client: TestClient, tmp_path: Path) -> None:
    """The exact S844 steps: ``<tmp>/hard.md`` is a hard link to ``<tmp>/.env``."""
    secret = tmp_path / ".env"
    secret.write_text("TOKEN=abc", encoding="utf-8")
    os.link(secret, tmp_path / "hard.md")

    _assert_denied(_ingest(client, tmp_path / "hard.md"))


def test_hard_link_next_to_an_unrelated_credential_file_is_accepted(client: TestClient, tmp_path: Path) -> None:
    (tmp_path / ".env").write_text("TOKEN=abc", encoding="utf-8")
    (tmp_path / "a.md").write_text("hello", encoding="utf-8")
    os.link(tmp_path / "a.md", tmp_path / "b.md")

    assert _ingest(client, tmp_path / "b.md").status_code == _ACCEPTED


def test_hard_link_to_a_pem_key_file_is_refused(client: TestClient, tmp_path: Path) -> None:
    (tmp_path / "server.key").write_text("-----BEGIN PRIVATE KEY-----\nx\n", encoding="utf-8")
    os.link(tmp_path / "server.key", tmp_path / "hard.md")

    _assert_denied(_ingest(client, tmp_path / "hard.md"))


def test_hard_link_to_a_keynote_key_file_is_accepted(client: TestClient, tmp_path: Path) -> None:
    (tmp_path / "deck.key").write_text("not pem", encoding="utf-8")
    os.link(tmp_path / "deck.key", tmp_path / "hard.md")

    assert _ingest(client, tmp_path / "hard.md").status_code == _ACCEPTED


def test_directory_walk_lists_a_directory_once_however_many_hard_links_it_holds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from archon_search import _path_safety
    from archon_search.pipeline import _discover_ingest_files

    (tmp_path / ".env").write_text("TOKEN=abc", encoding="utf-8")
    (tmp_path / "src.md").write_text("hello", encoding="utf-8")
    link_count = 20
    for index in range(link_count):
        os.link(tmp_path / "src.md", tmp_path / f"l{index}.md")
    os.link(tmp_path / ".env", tmp_path / "leak.md")
    listings: list[Path] = []
    real_scandir = os.scandir

    def counting_scandir(path):
        listings.append(Path(path))
        return real_scandir(path)

    monkeypatch.setattr(_path_safety.os, "scandir", counting_scandir)

    files = _discover_ingest_files(tmp_path, "*.md", _path_safety.IngestPathPolicy(), 1000)

    assert tmp_path / "leak.md" not in files
    assert len(files) == link_count + 1
    assert listings.count(tmp_path) == 2  # os.walk's own listing plus the one shared policy listing
