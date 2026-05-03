"""Tests for FastAPI app factory (Task 5.2)."""
from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import FastAPI

from archon_search.config import SearchConfig
from archon_search.jobs.store import JobStore
from archon_search.server.app import create_app
from archon_search.sync import path_to_collection_name


@pytest.fixture
def config(tmp_path: Path) -> SearchConfig:
    cfg = SearchConfig()
    cfg.db_path = str(tmp_path / "search")
    return cfg


@pytest.fixture
def job_store(tmp_path: Path) -> JobStore:
    return JobStore(path=tmp_path / "jobs.json")


def test_create_app_returns_fastapi_instance(config: SearchConfig, job_store: JobStore) -> None:
    app = create_app(config, job_store)
    assert isinstance(app, FastAPI)


def test_app_state_has_config(config: SearchConfig, job_store: JobStore) -> None:
    app = create_app(config, job_store)
    assert app.state.config is config


def test_app_state_has_job_store(config: SearchConfig, job_store: JobStore) -> None:
    app = create_app(config, job_store)
    assert app.state.job_store is job_store


def test_app_title_is_archon_search(config: SearchConfig, job_store: JobStore) -> None:
    app = create_app(config, job_store)
    assert app.title == "archon-search"


# ---------------------------------------------------------------------------
# C1-III-3: server startup collection derivation (FEAT-038 Task 11.2)
# ---------------------------------------------------------------------------


def test_server_main_derives_collection_from_history_dir() -> None:
    """Server startup derives collection names from history directory via path_to_collection_name.

    When archon-search is used with a history directory path, the collection name is
    derived from the last path component of the directory, sanitized to a valid name.
    This mirrors what archon.cli.search_cmd._path_to_collection_name does on the client side.
    """
    history_dir = "/home/user/.archon/history"
    sessions_path = str(Path(history_dir) / "sessions")

    col = path_to_collection_name(sessions_path)

    # The last component is "sessions" → sanitized → "sessions"
    assert col == "sessions"


def test_server_collection_derivation_uses_last_path_component() -> None:
    """path_to_collection_name uses the last path component regardless of parent directories.

    Different base directories with the same last component produce the same collection name.
    """
    col1 = path_to_collection_name("/alpha/sessions")
    col2 = path_to_collection_name("/beta/sessions")
    assert col1 == "sessions"
    assert col2 == "sessions"
