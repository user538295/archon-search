"""Tests for _execute_force_reinstall() in archon_search/install.py."""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from archon_search.install import _execute_force_reinstall
from archon_search.profiles import get_profile


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_config(tmp_path: Path, content: str = "[database]\nfoo = 'bar'\n") -> Path:
    cfg = tmp_path / "archon-search.toml"
    cfg.write_text(content)
    return cfg


def _make_db(tmp_path: Path) -> Path:
    db = tmp_path / "lancedb"
    db.mkdir()
    (db / "some_table.lance").write_text("data")
    return db


def _profile_and_name():
    return get_profile("balanced", multilingual=False), "balanced"


# ---------------------------------------------------------------------------
# Test 1: backs up config
# ---------------------------------------------------------------------------

def test_force_reinstall_backs_up_config(tmp_path):
    config_path = _make_config(tmp_path, content="[database]\nfoo = 'bar'\n")
    db_path = _make_db(tmp_path)
    profile, profile_name = _profile_and_name()
    original_content = config_path.read_text()

    with (
        patch("archon_search.install.prewarm.get_search_service") as mock_svc,
        patch("archon_search.install.prewarm._write_profile_config"),
        patch("archon_search.install.shutil.rmtree"),
    ):
        mock_svc.return_value.stop.return_value = None
        _execute_force_reinstall(
            config_path=config_path,
            db_path=db_path,
            profile=profile,
            profile_name=profile_name,
            multilingual=False,
            non_interactive=True,
        )

    bak = config_path.with_suffix(".toml.bak")
    assert bak.exists(), "Backup file should be created"
    assert bak.read_text() == original_content


# ---------------------------------------------------------------------------
# Test 2: "no" confirmation aborts, restores backup, does not rmtree
# ---------------------------------------------------------------------------

def test_force_reinstall_confirms_before_delete(tmp_path, capsys):
    config_path = _make_config(tmp_path, content="[database]\nfoo = 'bar'\n")
    db_path = _make_db(tmp_path)
    profile, profile_name = _profile_and_name()
    original_content = config_path.read_text()

    with (
        patch("builtins.input", return_value="no") as mock_input,
        patch("archon_search.install.prewarm.get_search_service") as mock_svc,
        patch("archon_search.install.prewarm._write_profile_config") as mock_write,
        patch("archon_search.install.shutil.rmtree") as mock_rmtree,
    ):
        mock_svc.return_value.stop.return_value = None
        with pytest.raises(SystemExit) as exc_info:
            _execute_force_reinstall(
                config_path=config_path,
                db_path=db_path,
                profile=profile,
                profile_name=profile_name,
                multilingual=False,
                non_interactive=False,
            )

    assert exc_info.value.code == 1
    mock_rmtree.assert_not_called()
    mock_write.assert_not_called()
    # Verify "Aborted." printed to stdout
    assert "Aborted." in capsys.readouterr().out
    # Backup restored: config_path has original content
    assert config_path.read_text() == original_content


# ---------------------------------------------------------------------------
# Test 3: "yes" confirmation proceeds
# ---------------------------------------------------------------------------

def test_force_reinstall_yes_confirmation_proceeds(tmp_path):
    config_path = _make_config(tmp_path)
    db_path = _make_db(tmp_path)
    profile, profile_name = _profile_and_name()

    with (
        patch("builtins.input", return_value="yes"),
        patch("archon_search.install.prewarm.get_search_service") as mock_svc,
        patch("archon_search.install.prewarm._write_profile_config") as mock_write,
        patch("archon_search.install.shutil.rmtree") as mock_rmtree,
    ):
        mock_svc.return_value.stop.return_value = None
        _execute_force_reinstall(
            config_path=config_path,
            db_path=db_path,
            profile=profile,
            profile_name=profile_name,
            multilingual=False,
            non_interactive=False,
        )

    mock_rmtree.assert_called_once_with(db_path)
    mock_write.assert_called_once()


# ---------------------------------------------------------------------------
# Test 3b: EOF at the confirmation prompt is treated as a declined confirmation
# ---------------------------------------------------------------------------

def test_force_reinstall_eof_aborts_and_restores_backup(tmp_path, capsys):
    config_path = _make_config(tmp_path, content="[database]\nfoo = 'bar'\n")
    db_path = _make_db(tmp_path)
    profile, profile_name = _profile_and_name()
    original_content = config_path.read_text()

    with (
        patch("builtins.input", side_effect=EOFError),
        patch("archon_search.install.prewarm.get_search_service") as mock_svc,
        patch("archon_search.install.prewarm._write_profile_config") as mock_write,
        patch("archon_search.install.shutil.rmtree") as mock_rmtree,
    ):
        mock_svc.return_value.stop.return_value = None
        with pytest.raises(SystemExit) as exc_info:
            _execute_force_reinstall(
                config_path=config_path,
                db_path=db_path,
                profile=profile,
                profile_name=profile_name,
                multilingual=False,
                non_interactive=False,
            )

    assert exc_info.value.code == 1
    mock_rmtree.assert_not_called()
    mock_write.assert_not_called()
    assert "Aborted." in capsys.readouterr().out
    assert config_path.read_text() == original_content


# ---------------------------------------------------------------------------
# Test 4: non_interactive=True skips input()
# ---------------------------------------------------------------------------

def test_force_reinstall_skips_confirm_when_non_interactive(tmp_path):
    config_path = _make_config(tmp_path)
    db_path = _make_db(tmp_path)
    profile, profile_name = _profile_and_name()

    with (
        patch("builtins.input") as mock_input,
        patch("archon_search.install.prewarm.get_search_service") as mock_svc,
        patch("archon_search.install.prewarm._write_profile_config"),
        patch("archon_search.install.shutil.rmtree"),
    ):
        mock_svc.return_value.stop.return_value = None
        _execute_force_reinstall(
            config_path=config_path,
            db_path=db_path,
            profile=profile,
            profile_name=profile_name,
            multilingual=False,
            non_interactive=True,
        )

    mock_input.assert_not_called()


# ---------------------------------------------------------------------------
# Test 5: deletes db directory with correct path
# ---------------------------------------------------------------------------

def test_force_reinstall_deletes_db_directory(tmp_path):
    config_path = _make_config(tmp_path)
    db_path = _make_db(tmp_path)
    profile, profile_name = _profile_and_name()

    with (
        patch("archon_search.install.prewarm.get_search_service") as mock_svc,
        patch("archon_search.install.prewarm._write_profile_config"),
        patch("archon_search.install.shutil.rmtree") as mock_rmtree,
    ):
        mock_svc.return_value.stop.return_value = None
        _execute_force_reinstall(
            config_path=config_path,
            db_path=db_path,
            profile=profile,
            profile_name=profile_name,
            multilingual=False,
            non_interactive=True,
        )

    mock_rmtree.assert_called_once_with(db_path)


# ---------------------------------------------------------------------------
# Test 6: non-RuntimeError from stop() restores backup and re-raises
# ---------------------------------------------------------------------------

def test_force_reinstall_restores_backup_on_stop_failure(tmp_path):
    config_path = _make_config(tmp_path, content="[database]\noriginal = true\n")
    db_path = _make_db(tmp_path)
    profile, profile_name = _profile_and_name()
    original_content = config_path.read_text()

    class BoomError(Exception):
        pass

    with (
        patch("archon_search.install.prewarm.get_search_service") as mock_svc,
        patch("archon_search.install.prewarm._write_profile_config") as mock_write,
        patch("archon_search.install.shutil.rmtree") as mock_rmtree,
    ):
        mock_svc.return_value.stop.side_effect = BoomError("kaboom")
        with pytest.raises(BoomError):
            _execute_force_reinstall(
                config_path=config_path,
                db_path=db_path,
                profile=profile,
                profile_name=profile_name,
                multilingual=False,
                non_interactive=True,
            )

    mock_rmtree.assert_not_called()
    mock_write.assert_not_called()
    # Backup should have been restored back to config_path
    assert config_path.read_text() == original_content


# ---------------------------------------------------------------------------
# Test 7: write failure after DB deletion: backup preserved, message to stderr, SystemExit(1)
# ---------------------------------------------------------------------------

def test_force_reinstall_prints_post_db_deletion_message_on_write_failure(tmp_path, capsys):
    config_path = _make_config(tmp_path)
    db_path = _make_db(tmp_path)
    profile, profile_name = _profile_and_name()

    with (
        patch("archon_search.install.prewarm.get_search_service") as mock_svc,
        patch("archon_search.install.prewarm._write_profile_config", side_effect=OSError("disk full")),
        patch("archon_search.install.shutil.rmtree"),
    ):
        mock_svc.return_value.stop.return_value = None
        with pytest.raises(SystemExit) as exc_info:
            _execute_force_reinstall(
                config_path=config_path,
                db_path=db_path,
                profile=profile,
                profile_name=profile_name,
                multilingual=False,
                non_interactive=True,
            )

    assert exc_info.value.code == 1
    captured = capsys.readouterr()
    assert "Install failed after database deletion" in captured.err
    # Backup should NOT have been restored (it stays as .toml.bak)
    bak = config_path.with_suffix(".toml.bak")
    assert bak.exists(), "Backup should be preserved (not removed)"


# ---------------------------------------------------------------------------
# Test 8: missing db_path is silently skipped
# ---------------------------------------------------------------------------

def test_force_reinstall_handles_missing_db_directory(tmp_path):
    config_path = _make_config(tmp_path)
    db_path = tmp_path / "nonexistent_lancedb"  # does not exist
    profile, profile_name = _profile_and_name()

    with (
        patch("archon_search.install.prewarm.get_search_service") as mock_svc,
        patch("archon_search.install.prewarm._write_profile_config") as mock_write,
        patch("archon_search.install.shutil.rmtree") as mock_rmtree,
    ):
        mock_svc.return_value.stop.return_value = None
        _execute_force_reinstall(
            config_path=config_path,
            db_path=db_path,
            profile=profile,
            profile_name=profile_name,
            multilingual=False,
            non_interactive=True,
        )

    mock_rmtree.assert_not_called()
    mock_write.assert_called_once()


# ---------------------------------------------------------------------------
# Test 9: dry_run skips all destructive operations
# ---------------------------------------------------------------------------

def test_force_reinstall_dry_run_skips_all_destructive_ops(tmp_path):
    config_path = _make_config(tmp_path)
    db_path = _make_db(tmp_path)
    profile, profile_name = _profile_and_name()

    with (
        patch("archon_search.install.prewarm.get_search_service") as mock_svc,
        patch("archon_search.install.prewarm._write_profile_config") as mock_write,
        patch("archon_search.install.shutil.rmtree") as mock_rmtree,
    ):
        _execute_force_reinstall(
            config_path=config_path,
            db_path=db_path,
            profile=profile,
            profile_name=profile_name,
            multilingual=False,
            non_interactive=True,
            dry_run=True,
        )

    mock_svc.return_value.stop.assert_not_called()
    mock_rmtree.assert_not_called()
    mock_write.assert_not_called()


# ---------------------------------------------------------------------------
# Test 10: RuntimeError from stop() is treated as no-op (proceeds to rmtree + write)
# ---------------------------------------------------------------------------

def test_force_reinstall_runtime_error_from_stop_is_no_op(tmp_path):
    config_path = _make_config(tmp_path)
    db_path = _make_db(tmp_path)
    profile, profile_name = _profile_and_name()

    with (
        patch("archon_search.install.prewarm.get_search_service") as mock_svc,
        patch("archon_search.install.prewarm._write_profile_config") as mock_write,
        patch("archon_search.install.shutil.rmtree") as mock_rmtree,
    ):
        mock_svc.return_value.stop.side_effect = RuntimeError("service not running")
        # Must NOT raise — RuntimeError from stop() is swallowed
        _execute_force_reinstall(
            config_path=config_path,
            db_path=db_path,
            profile=profile,
            profile_name=profile_name,
            multilingual=False,
            non_interactive=True,
        )

    mock_rmtree.assert_called_once_with(db_path)
    mock_write.assert_called_once()


# ---------------------------------------------------------------------------
# Test 11: rmtree failure prints "during database deletion" message to stderr, SystemExit(1)
# ---------------------------------------------------------------------------

def test_force_reinstall_rmtree_failure_exits_with_message(tmp_path, capsys):
    config_path = _make_config(tmp_path)
    db_path = _make_db(tmp_path)
    profile, profile_name = _profile_and_name()

    with (
        patch("archon_search.install.prewarm.get_search_service") as mock_svc,
        patch("archon_search.install.prewarm._write_profile_config") as mock_write,
        patch("archon_search.install.shutil.rmtree", side_effect=OSError("permission denied")),
    ):
        mock_svc.return_value.stop.return_value = None
        with pytest.raises(SystemExit) as exc_info:
            _execute_force_reinstall(
                config_path=config_path,
                db_path=db_path,
                profile=profile,
                profile_name=profile_name,
                multilingual=False,
                non_interactive=True,
            )

    assert exc_info.value.code == 1
    captured = capsys.readouterr()
    assert "Install failed during database deletion" in captured.err
    # _write_profile_config must NOT be called after rmtree failure
    mock_write.assert_not_called()
    # Backup (.toml.bak) must be preserved (NOT restored to config_path)
    bak = config_path.with_suffix(".toml.bak")
    assert bak.exists(), "Backup should be preserved"


# ---------------------------------------------------------------------------
# Test 12: config does not exist — no backup created, abort still works
# ---------------------------------------------------------------------------

def test_force_reinstall_no_config_no_backup(tmp_path):
    config_path = tmp_path / "archon-search.toml"  # does not exist
    db_path = _make_db(tmp_path)
    profile, profile_name = _profile_and_name()

    with (
        patch("archon_search.install.prewarm.get_search_service") as mock_svc,
        patch("archon_search.install.prewarm._write_profile_config") as mock_write,
        patch("archon_search.install.shutil.rmtree") as mock_rmtree,
    ):
        mock_svc.return_value.stop.return_value = None
        _execute_force_reinstall(
            config_path=config_path,
            db_path=db_path,
            profile=profile,
            profile_name=profile_name,
            multilingual=False,
            non_interactive=True,
        )

    bak = config_path.with_suffix(".toml.bak")
    assert not bak.exists(), "No backup should be created when config does not exist"
    mock_rmtree.assert_called_once()
    mock_write.assert_called_once()
