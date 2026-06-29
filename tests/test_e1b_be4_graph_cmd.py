"""Unit tests for ``archon-search graph build-communities`` CLI — E1b BE-4.

Tests:
- test_build_communities_cli_success: mocked CommunityBuilder; exit 0; output has count
- test_build_communities_cli_no_graph_exits_nonzero: build() raises ValueError; exit 1; stderr has message (S6)
- test_build_communities_cli_leidenalg_absent_exits_nonzero: build() raises ImportError; exit 1; hint in output (S13)
- test_build_communities_cli_config_error_exits_nonzero: load_config raises ConfigError; exit 1; message in output
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from click.testing import CliRunner

from archon_search.cli.graph_cmd import graph_cmd
from archon_search.config import ConfigError
from archon_search.graph_types import Community


def _make_community(idx: int) -> Community:
    return Community(
        community_id=f"community-{idx}",
        entity_ids=[f"entity-{idx}"],
        representative_chunk_ids=[f"chunk-{idx}"],
        built_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        summary_text=None,
    )


def test_build_communities_cli_success(tmp_path: Path) -> None:
    """CliRunner with mocked CommunityBuilder.build(); exit 0; stdout contains community count."""
    runner = CliRunner()

    fake_communities = [_make_community(i) for i in range(3)]

    with (
        patch("archon_search.cli.graph_cmd.load_config") as mock_load_config,
        patch("archon_search.cli.graph_cmd.GraphStore") as mock_gs_cls,
        patch("archon_search.cli.graph_cmd.SearchStore") as mock_ss_cls,
        patch("archon_search.cli.graph_cmd.CommunityBuilder") as mock_cb_cls,
    ):
        mock_cfg = MagicMock()
        mock_cfg.db_path = str(tmp_path / "db")
        mock_cfg.graph.enabled = True
        mock_load_config.return_value = mock_cfg

        mock_gs = AsyncMock()
        mock_gs_cls.return_value = mock_gs
        mock_ss = AsyncMock()
        mock_ss_cls.return_value = mock_ss

        mock_builder = AsyncMock()
        mock_builder.build = AsyncMock(return_value=fake_communities)
        mock_cb_cls.return_value = mock_builder

        result = runner.invoke(graph_cmd, ["build-communities", "my-collection"])

    assert result.exit_code == 0, f"Unexpected exit code: {result.exit_code}\n{result.output}"
    assert "Built 3 communities" in result.output, (
        f"Community count not found in output: {result.output!r}"
    )
    mock_builder.build.assert_awaited_once_with("my-collection")
    mock_gs.disconnect.assert_awaited_once()
    mock_ss.disconnect.assert_awaited_once()


def test_build_communities_cli_no_graph_exits_nonzero(tmp_path: Path) -> None:
    """When CommunityBuilder.build() raises ValueError (no graph nodes), exit 1; message present (S6)."""
    runner = CliRunner()

    with (
        patch("archon_search.cli.graph_cmd.load_config") as mock_load_config,
        patch("archon_search.cli.graph_cmd.GraphStore") as mock_gs_cls,
        patch("archon_search.cli.graph_cmd.SearchStore") as mock_ss_cls,
        patch("archon_search.cli.graph_cmd.CommunityBuilder") as mock_cb_cls,
    ):
        mock_cfg = MagicMock()
        mock_cfg.db_path = str(tmp_path / "db")
        mock_cfg.graph.enabled = True
        mock_load_config.return_value = mock_cfg

        mock_gs = AsyncMock()
        mock_gs_cls.return_value = mock_gs
        mock_ss = AsyncMock()
        mock_ss_cls.return_value = mock_ss

        mock_builder = AsyncMock()
        mock_builder.build = AsyncMock(
            side_effect=ValueError(
                "No entity graph nodes found for collection 'my-collection'. "
                "Run ingest with graph.enabled=true first."
            )
        )
        mock_cb_cls.return_value = mock_builder

        result = runner.invoke(graph_cmd, ["build-communities", "my-collection"])

    assert result.exit_code == 1, (
        f"Expected exit code 1 on ValueError, got {result.exit_code}"
    )
    # CliRunner mixes stderr into output by default (mix_stderr=True)
    assert "entity graph" in result.output or "graph.enabled" in result.output, (
        f"Expected actionable error message in output: {result.output!r}"
    )


def test_build_communities_cli_leidenalg_absent_exits_nonzero(tmp_path: Path) -> None:
    """When leidenalg is absent (ImportError), exit 1; install hint in output (S13)."""
    runner = CliRunner()

    with (
        patch("archon_search.cli.graph_cmd.load_config") as mock_load_config,
        patch("archon_search.cli.graph_cmd.GraphStore") as mock_gs_cls,
        patch("archon_search.cli.graph_cmd.SearchStore") as mock_ss_cls,
        patch("archon_search.cli.graph_cmd.CommunityBuilder") as mock_cb_cls,
    ):
        mock_cfg = MagicMock()
        mock_cfg.db_path = str(tmp_path / "db")
        mock_cfg.graph.enabled = True
        mock_load_config.return_value = mock_cfg

        mock_gs = AsyncMock()
        mock_gs_cls.return_value = mock_gs
        mock_ss = AsyncMock()
        mock_ss_cls.return_value = mock_ss

        mock_builder = AsyncMock()
        mock_builder.build = AsyncMock(
            side_effect=ImportError(
                "leidenalg is required for community detection. "
                "Install it with: pip install archon-search[graph]"
            )
        )
        mock_cb_cls.return_value = mock_builder

        result = runner.invoke(graph_cmd, ["build-communities", "my-collection"])

    assert result.exit_code == 1, (
        f"Expected exit code 1 when leidenalg absent, got {result.exit_code}"
    )
    assert "archon-search[graph]" in result.output or "leidenalg" in result.output, (
        f"Expected install hint in output: {result.output!r}"
    )


def test_build_communities_cli_config_error_exits_nonzero() -> None:
    """When load_config() raises ConfigError, exit 1; error message present."""
    runner = CliRunner()

    with patch("archon_search.cli.graph_cmd.load_config") as mock_load_config:
        mock_load_config.side_effect = ConfigError(
            "Invalid [graph].leiden_resolution: must be > 0"
        )
        result = runner.invoke(graph_cmd, ["build-communities", "my-collection"])

    assert result.exit_code == 1, (
        f"Expected exit code 1 on ConfigError, got {result.exit_code}"
    )
    assert "config" in result.output.lower() or "leiden_resolution" in result.output, (
        f"Expected config error message in output: {result.output!r}"
    )
