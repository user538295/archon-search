"""Integration test for IngestConfig round-trip via make_real_app.

Plan: Documentation/Backlog/e0d-pdf-large-file-support-team-plan.md Task BE-2.
"""

from __future__ import annotations

import pytest

from tests.integration.conftest import make_real_app


@pytest.mark.integration
def test_ingest_config_round_trip_via_make_real_app(tmp_path, monkeypatch) -> None:
    """TOML with [ingest] produces correct SearchConfig.ingest values in a real app."""
    toml_content = "[ingest]\nmax_file_mb = 42\n"
    with make_real_app(tmp_path, monkeypatch, toml_content=toml_content) as (client, cfg, _api_key):
        assert cfg.ingest.max_file_mb == 42
