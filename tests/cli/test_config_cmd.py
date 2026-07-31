"""TDD tests for archon-search CLI subcommands: install, uninstall, ingest, sync, collection, config."""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import tomlkit
from click.testing import CliRunner

from archon_search.cli.main import main


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


@pytest.fixture
def mock_service() -> MagicMock:
    return MagicMock()


# ---------------------------------------------------------------------------
# config subcommand
# ---------------------------------------------------------------------------

def test_config_show_prints_toml(runner: CliRunner, tmp_path: Path) -> None:
    config_file = tmp_path / "archon-search.toml"
    config_file.write_text("[server]\nport = 8765\nhost = \"127.0.0.1\"\n")
    result = runner.invoke(main, ["config", "show", "--config", str(config_file)])
    assert result.exit_code == 0, result.output
    assert "[server]" in result.output


def test_config_show_defaults_when_no_file(runner: CliRunner, tmp_path: Path) -> None:
    missing = tmp_path / "nonexistent.toml"
    result = runner.invoke(main, ["config", "show", "--config", str(missing)])
    assert result.exit_code == 0, result.output
    # Should still show something (default values)
    assert result.output.strip() != ""


def test_config_get_port(runner: CliRunner, tmp_path: Path) -> None:
    config_file = tmp_path / "archon-search.toml"
    config_file.write_text("[server]\nport = 8765\n")
    result = runner.invoke(main, ["config", "get", "server.port", "--config", str(config_file)])
    assert result.exit_code == 0, result.output
    assert "8765" in result.output


def test_config_get_missing_key_exits_nonzero(runner: CliRunner, tmp_path: Path) -> None:
    config_file = tmp_path / "archon-search.toml"
    config_file.write_text("[server]\nport = 8765\n")
    result = runner.invoke(main, ["config", "get", "server.nonexistent", "--config", str(config_file)])
    assert result.exit_code != 0


def test_config_set_updates_file(runner: CliRunner, tmp_path: Path) -> None:
    config_file = tmp_path / "archon-search.toml"
    config_file.write_text("[server]\nport = 8765\n")
    result = runner.invoke(main, ["config", "set", "server.port", "9000", "--config", str(config_file)])
    assert result.exit_code == 0, result.output
    doc = tomlkit.parse(config_file.read_text())
    assert doc["server"]["port"] == 9000


def test_config_set_creates_file_if_absent(runner: CliRunner, tmp_path: Path) -> None:
    config_file = tmp_path / "new-config.toml"
    assert not config_file.exists()
    result = runner.invoke(main, ["config", "set", "server.port", "9001", "--config", str(config_file)])
    assert result.exit_code == 0, result.output
    assert config_file.exists()
    doc = tomlkit.parse(config_file.read_text())
    assert doc["server"]["port"] == 9001


def test_config_set_boolean_true(runner: CliRunner, tmp_path: Path) -> None:
    config_file = tmp_path / "archon-search.toml"
    config_file.write_text("[database]\nauto_reindex_on_chunk_size_change = true\n")
    result = runner.invoke(main, ["config", "set", "database.auto_reindex_on_chunk_size_change", "false", "--config", str(config_file)])
    assert result.exit_code == 0, result.output
    doc = tomlkit.parse(config_file.read_text())
    assert doc["database"]["auto_reindex_on_chunk_size_change"] is False  # must be bool, not string


def test_config_set_boolean_false(runner: CliRunner, tmp_path: Path) -> None:
    config_file = tmp_path / "archon-search.toml"
    config_file.write_text("[database]\nauto_reindex_on_chunk_size_change = false\n")
    result = runner.invoke(main, ["config", "set", "database.auto_reindex_on_chunk_size_change", "true", "--config", str(config_file)])
    assert result.exit_code == 0, result.output
    doc = tomlkit.parse(config_file.read_text())
    assert doc["database"]["auto_reindex_on_chunk_size_change"] is True  # must be bool, not string


def test_config_get_routing_key_with_no_file(runner: CliRunner, tmp_path: Path) -> None:
    missing = tmp_path / "nonexistent.toml"
    result = runner.invoke(main, ["config", "get", "routing.routing_shortlist_size", "--config", str(missing)])
    assert result.exit_code == 0, result.output
    assert "8" in result.output  # default value


def test_config_show_defaults_include_all_sections(runner: CliRunner, tmp_path: Path) -> None:
    missing = tmp_path / "nonexistent.toml"
    result = runner.invoke(main, ["config", "show", "--config", str(missing)])
    assert result.exit_code == 0, result.output
    assert "[server]" in result.output
    assert "[database]" in result.output
    assert "[routing]" in result.output
    assert "[collections]" in result.output
    assert "[logging]" in result.output


# ---------------------------------------------------------------------------
# install subcommand
# ---------------------------------------------------------------------------

def test_install_delegates_to_search_installer(runner: CliRunner, tmp_path: Path) -> None:
    """install command is a thin shim — verify run_register_and_start() is called."""
    config_path = tmp_path / "archon-search.toml"
    config_path.write_text("[database]\n")
    run_mock = MagicMock(return_value=0)
    with patch("archon_search.cli.install_cmd.create_installer") as installer_cls:
        installer_cls.return_value.run_register_and_start = run_mock
        result = runner.invoke(main, ["install", "--config", str(config_path)])
    assert result.exit_code == 0, result.output
    run_mock.assert_called_once()


def test_install_dry_run_passed_to_installer(runner: CliRunner, tmp_path: Path) -> None:
    """--dry-run flag is forwarded to SearchInstaller constructor."""
    config_path = tmp_path / "archon-search.toml"
    config_path.write_text("[database]\n")
    run_mock = MagicMock(return_value=0)
    with patch("archon_search.cli.install_cmd.create_installer") as installer_cls:
        installer_cls.return_value.run_register_and_start = run_mock
        result = runner.invoke(main, ["install", "--config", str(config_path), "--dry-run"])
    assert result.exit_code == 0, result.output
    _, kwargs = installer_cls.call_args
    assert kwargs.get("dry_run") is True


def test_install_migrates_legacy_service_definition(runner: CliRunner, mock_service: MagicMock, tmp_path: Path) -> None:
    """Legacy service migration is now the wizard's responsibility; install just registers."""
    config_path = tmp_path / "archon-search.toml"
    config_path.write_text("[database]\n")
    run_mock = MagicMock(return_value=0)
    with patch("archon_search.cli.install_cmd.create_installer") as installer_cls:
        installer_cls.return_value.run_register_and_start = run_mock
        result = runner.invoke(main, ["install", "--config", str(config_path)])
    assert result.exit_code == 0, result.output
    run_mock.assert_called_once()


# ---------------------------------------------------------------------------
# uninstall subcommand
# ---------------------------------------------------------------------------

def test_uninstall_unregisters_service(runner: CliRunner, mock_service: MagicMock) -> None:
    with patch("archon_search.cli.install_cmd._get_service", return_value=mock_service):
        result = runner.invoke(main, ["uninstall"])
    assert result.exit_code == 0, result.output
    mock_service.stop.assert_called_once()
    mock_service.unregister.assert_called_once()


def test_uninstall_delete_db_flag_preserved(runner: CliRunner, mock_service: MagicMock, tmp_path: Path) -> None:
    db_dir = tmp_path / "search_db"
    db_dir.mkdir()
    (db_dir / "data.lancedb").write_text("fake")

    with (
        patch("archon_search.cli.install_cmd._get_service", return_value=mock_service),
        patch("archon_search.cli.install_cmd._get_db_path", return_value=db_dir),
    ):
        result = runner.invoke(main, ["uninstall", "--delete-db"])
    assert result.exit_code == 0, result.output
    assert not db_dir.exists()


def test_uninstall_no_delete_db_preserves_directory(runner: CliRunner, mock_service: MagicMock, tmp_path: Path) -> None:
    db_dir = tmp_path / "search_db"
    db_dir.mkdir()

    with (
        patch("archon_search.cli.install_cmd._get_service", return_value=mock_service),
        patch("archon_search.cli.install_cmd._get_db_path", return_value=db_dir),
    ):
        result = runner.invoke(main, ["uninstall"])
    assert result.exit_code == 0, result.output
    assert db_dir.exists()


# ---------------------------------------------------------------------------
# sync subcommand
# ---------------------------------------------------------------------------

def test_sync_command_available(runner: CliRunner) -> None:
    result = runner.invoke(main, ["sync", "--help"])
    assert result.exit_code == 0, result.output


def test_sync_calls_collection_sync(runner: CliRunner, tmp_path: Path) -> None:
    # sync is now an httpx proxy (FE-6) — verify it POSTs to /sync.
    import httpx as _httpx

    resp_202 = MagicMock()
    resp_202.status_code = 202
    resp_202.json.return_value = {"job_id": "sync-test-id", "status": "RUNNING"}

    with patch("httpx.post", return_value=resp_202) as mock_post:
        result = runner.invoke(main, ["sync", "--api-key", "testkey"])
    assert result.exit_code == 0, result.output
    mock_post.assert_called_once()
    call_url = mock_post.call_args[0][0]
    assert "/sync" in call_url


# ---------------------------------------------------------------------------
# collection subgroup
# ---------------------------------------------------------------------------

def test_ingest_exits_with_error_when_no_path_given(runner: CliRunner) -> None:
    result = runner.invoke(main, ["ingest"])
    assert result.exit_code == 1
    assert "Error: --path is required." in result.output


def test_collection_group_available(runner: CliRunner) -> None:
    result = runner.invoke(main, ["collection", "--help"])
    assert result.exit_code == 0, result.output


def test_collection_list_available(runner: CliRunner) -> None:
    result = runner.invoke(main, ["collection", "list", "--help"])
    assert result.exit_code == 0, result.output


def test_collection_add_available(runner: CliRunner) -> None:
    result = runner.invoke(main, ["collection", "add", "--help"])
    assert result.exit_code == 0, result.output


def test_collection_remove_available(runner: CliRunner) -> None:
    result = runner.invoke(main, ["collection", "remove", "--help"])
    assert result.exit_code == 0, result.output


def test_collection_remove_proxies_delete_to_server(runner: CliRunner) -> None:
    """collection remove now proxies to DELETE /collections/{name} via httpx (FE-8)."""
    import httpx

    del_resp = MagicMock()
    del_resp.status_code = 200
    del_resp.json.return_value = {"name": "mycol", "deleted": True}

    with patch("archon_search.cli.collection.httpx.delete", return_value=del_resp) as mock_delete:
        result = runner.invoke(main, [
            "collection", "remove", "mycol",
            "--api-key", "test-key",
        ])

    assert result.exit_code == 0, result.output
    assert "mycol" in result.output
    call_url = mock_delete.call_args[0][0]
    assert "/collections/mycol" in call_url


def test_collection_remove_409_pinned_only(runner: CliRunner) -> None:
    """409 from server → pinned-only message, exit 1."""
    del_resp = MagicMock()
    del_resp.status_code = 409
    del_resp.json.return_value = {"detail": "pinned-only"}
    del_resp.text = "pinned-only"

    with patch("archon_search.cli.collection.httpx.delete", return_value=del_resp):
        result = runner.invoke(main, [
            "collection", "remove", "mycol",
            "--api-key", "test-key",
        ])

    assert result.exit_code == 1
    assert "Cannot remove 'mycol'" in result.output
    assert "Un-pin it first" in result.output


def test_collection_remove_force_no_longer_an_option(runner: CliRunner) -> None:
    """--force is not a valid option after conversion to httpx proxy."""
    del_resp = MagicMock()
    del_resp.status_code = 200

    with patch("archon_search.cli.collection.httpx.delete", return_value=del_resp):
        result = runner.invoke(main, [
            "collection", "remove", "mycol",
            "--force",
            "--api-key", "test-key",
        ])

    assert result.exit_code == 2  # Click unrecognized option


def test_collection_remove_dry_run_no_longer_an_option(runner: CliRunner) -> None:
    """--dry-run is not a valid option after conversion to httpx proxy."""
    del_resp = MagicMock()
    del_resp.status_code = 200

    with patch("archon_search.cli.collection.httpx.delete", return_value=del_resp):
        result = runner.invoke(main, [
            "collection", "remove", "mycol",
            "--dry-run",
            "--api-key", "test-key",
        ])

    assert result.exit_code == 2  # Click unrecognized option


def test_collection_add_submits_job_via_httpx(runner: CliRunner, tmp_path: Path) -> None:
    """collection add now proxies to POST /collections/ via httpx (FE-4)."""
    import httpx

    post_resp = MagicMock()
    post_resp.status_code = 202
    post_resp.json.return_value = {
        "job_id": "job-test-001",
        "status": "QUEUED",
        "collection": "my_path",
    }

    with patch("archon_search.cli.collection.httpx.post", return_value=post_resp) as mock_post:
        result = runner.invoke(main, [
            "collection", "add", str(tmp_path),
            "--api-key", "test-key",
        ])
    assert result.exit_code == 0, result.output
    mock_post.assert_called_once()
    call_url = mock_post.call_args[0][0]
    assert "/collections" in call_url
    assert mock_post.call_args[1]["json"]["path"] == str(tmp_path)


