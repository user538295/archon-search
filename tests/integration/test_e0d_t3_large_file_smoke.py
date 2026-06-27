"""E0d T-3 — large-file acceptance smoke test.

Proves that pipeline.ingest_file() with max_file_mb=0 (no limit) completes
without error for a file substantially larger than the default chunk size.

The 500-page / 100 MB benchmark and RSS measurement are deferred to D4
(memory reduction scope). This test covers the guard-disabled ingest path (S1).

Plan: Documentation/Backlog/e0d-pdf-large-file-support-team-plan.md Task T-3
"""
from __future__ import annotations

import asyncio

import pytest

from tests.integration.conftest import make_real_pipeline


@pytest.mark.integration
def test_large_file_ingests_without_error_when_guard_disabled(tmp_path, monkeypatch) -> None:
    """S1: max_file_mb=0 (default) — a multi-MB file ingests successfully."""
    large_file = tmp_path / "large_doc.md"
    # ~2 MB of markdown text — well above chunk size, tests multi-chunk ingest path
    large_file.write_text("# Large Document\n\n" + ("word " * 100 + "\n\n") * 2_000)

    async def _run() -> None:
        store, pipeline = await make_real_pipeline(tmp_path, monkeypatch)
        # ponytail: max_file_mb defaults to 0; no need to pass it explicitly
        result = await pipeline.ingest_file(
            large_file,
            "smoke",
            embedder=pipeline._global_embedder,
        )
        assert result.status == "ok", f"ingest failed: {result.error}"
        assert result.chunks_created > 0
        assert result.code is None

    asyncio.run(_run())
