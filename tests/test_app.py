"""Tests for FastAPI app factory (Task 5.2)."""
from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import FastAPI

from archon_search.config import SearchConfig
from archon_search.jobs.store import JobStore
from archon_search.server.app import create_app


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
