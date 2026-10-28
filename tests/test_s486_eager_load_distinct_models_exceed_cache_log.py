"""S486: ``eager_load_embedders`` over a too-small ``embedder_cache_size`` logs the DOCUMENTED line.

OperatorGuide/80_capacity_and_performance.md:176 documents the startup log as
``eager_load_embedders: N distinct models exceed embedder_cache_size`` (``N`` = every distinct model
the warm-up covers, the pinned default included). ``server/app.py`` instead logs
``eager_load_embedders: 2 per-collection models other than the default exceed embedder_cache_size=1``,
so an operator grepping the documented text finds nothing.
"""
from __future__ import annotations

import logging
import re
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from click.testing import CliRunner
from starlette.testclient import TestClient

from archon_search.cli.serve import serve
from archon_search.collection_meta import CollectionMeta
from archon_search.config import SearchConfig
from archon_search.jobs.store import JobStore
from archon_search.server.app import create_app
from archon_search.store import SearchStore
from tests.test_app import wait_for_warmup

DEFAULT_MODEL = "BAAI/bge-small-en-v1.5"
NON_DEFAULT_MODELS = ("BAAI/bge-base-en-v1.5", "snowflake/snowflake-arctic-embed-xs")
CACHE_CAPACITY = 1
DOCUMENTED_LINE = re.compile(r"eager_load_embedders: (\d+) distinct models exceed embedder_cache_size")


@pytest.fixture
def job_store(tmp_path: Path) -> JobStore:
    return JobStore(path=tmp_path / "jobs.json")


def test_eager_load_logs_documented_distinct_models_line_and_warmup_completes(
    tmp_path: Path, job_store: JobStore, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    import sys
    import types

    anthropic_stub = types.ModuleType("anthropic")
    anthropic_stub.APIError = type("APIError", (Exception,), {})  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "anthropic", anthropic_stub)

    cfg = SearchConfig()
    cfg.db_path = str(tmp_path / "search")
    cfg.embedding_model = DEFAULT_MODEL
    cfg.eager_load_embedders = True
    cfg.embedder_cache_size = CACHE_CAPACITY
    metas = [CollectionMeta(name=f"col{i}", active_embedding_model=m) for i, m in enumerate(NON_DEFAULT_MODELS)]
    distinct = 1 + len(NON_DEFAULT_MODELS)

    with (
        patch.object(SearchStore, "connect", new=AsyncMock()),
        patch.object(SearchStore, "_run_startup_migrations", new=AsyncMock()),
        patch.object(SearchStore, "disconnect", new=AsyncMock()),
        patch.object(SearchStore, "get_all_collections_meta", new=AsyncMock(return_value=metas)),
        caplog.at_level(logging.INFO),
    ):
        app = create_app(cfg, job_store)
        with TestClient(app):
            wait_for_warmup(app)

    assert f"model warm-up complete ({distinct} embedding models" in caplog.text
    assert "model warm-up failed" not in caplog.text
    match = DOCUMENTED_LINE.search(caplog.text)
    assert match is not None, "documented line missing; eager_load_embedders lines: " + "\n".join(
        r.getMessage() for r in caplog.records if "eager_load_embedders" in r.getMessage()
    )
    assert int(match.group(1)) == distinct
    assert int(match.group(1)) > CACHE_CAPACITY


def test_embedder_cache_size_zero_is_rejected_at_startup(tmp_path: Path) -> None:
    config_path = tmp_path / "archon-search.toml"
    config_path.write_text("[database]\nembedder_cache_size = 0\n")

    result = CliRunner().invoke(serve, ["--config", str(config_path)])

    assert result.exit_code == 1
    assert re.search(r"^Error: .*embedder_cache_size", result.output, re.MULTILINE)
