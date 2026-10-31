"""The eval runner opens its own ``SearchStore`` and writes metadata, so each opener runs the startup migrations.

``connect()`` stays free of side effects, so a store lacking the newer metadata columns would fail the runner's
first metadata write exactly like a server without the startup migrations would (Q39).
"""
from __future__ import annotations

from pathlib import Path

import lancedb
import pyarrow as pa
import pytest

from archon_search.eval.runner import (
    _build_code_lane_pipeline,
    _build_pipeline_with_eval_backends,
)
from archon_search.store import SearchStore

pytestmark = pytest.mark.integration

_META_TABLE = "_archon_collection_meta"
_NEW_COLUMNS = ("default_ttl_seconds", "description_source", "description_status")


async def _seed_old_store(db_path: Path) -> None:
    db = await lancedb.connect_async(str(db_path))
    old_schema = pa.schema([f for f in SearchStore._meta_schema() if f.name not in _NEW_COLUMNS])
    await db.create_table(_META_TABLE, schema=old_schema)


async def _meta_columns(db_path: Path) -> list[str]:
    db = await lancedb.connect_async(str(db_path))
    return (await (await db.open_table(_META_TABLE)).schema()).names


async def test_eval_runner_stores_run_startup_migrations(tmp_path: Path) -> None:
    deterministic_path = tmp_path / "deterministic"
    code_lane_path = tmp_path / "code-lane"
    await _seed_old_store(deterministic_path)
    await _seed_old_store(code_lane_path)

    pipeline = await _build_pipeline_with_eval_backends(deterministic_path)
    code_lane_pipeline, graph_store = await _build_code_lane_pipeline(code_lane_path)
    try:
        assert graph_store is None
        for path in (deterministic_path, code_lane_path):
            columns = await _meta_columns(path)
            for column in _NEW_COLUMNS:
                assert column in columns, f"{column} missing after the eval runner opened {path.name}"
    finally:
        await pipeline.store.disconnect()
        await code_lane_pipeline.store.disconnect()
