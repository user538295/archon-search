"""Tests for SearchInstaller.run() — TDD (Task 3.4)."""
from __future__ import annotations

import os
import shutil
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from archon_search.install import InstallError, SearchInstaller
from archon_search.platform.types import GpuType

pytestmark = pytest.mark.xdist_group("install")


# ---------------------------------------------------------------------------
# Shared mock helper
# ---------------------------------------------------------------------------


@contextmanager
def _mock_installer(tmp_path: Path, **extra_patches: Any):
    """Patch all infrastructure so run() never touches real FS or services."""
    config_path = tmp_path / "archon-search.toml"
    fake_legacy = tmp_path / "fake.plist"  # does NOT exist → no cleanup

    base_patches = {
        "archon_search.install.get_default_config_path": MagicMock(return_value=config_path),
        "archon_search.install._legacy_service_path": MagicMock(return_value=fake_legacy),
        "archon_search.install._remove_legacy_service": MagicMock(),
        "archon_search.install._prewarm_models": MagicMock(),
        "archon_search.install._check_disk_space": MagicMock(),
        "archon_search.install.SearchInstaller.detect_gpu": MagicMock(return_value=GpuType.NONE),
        "archon_search.install.SearchInstaller.validate_providers": MagicMock(return_value=False),
        "archon_search.install.SearchInstaller.configure_providers": MagicMock(),
        "archon_search.install.SearchInstaller.write_service_file": MagicMock(),
        "archon_search.install.SearchInstaller.load_service": MagicMock(return_value=0),
        "archon_search.install.SearchInstaller._wait_for_service": MagicMock(return_value=True),
        "archon_search.install.SearchInstaller._is_service_running": MagicMock(return_value=False),
    }
    base_patches.update(extra_patches)

    with patch.multiple("archon_search.install", **{
        k.replace("archon_search.install.", ""): v
        for k, v in base_patches.items()
        if k.startswith("archon_search.install.") and "SearchInstaller." not in k
    }):
        with patch.multiple(SearchInstaller, **{
            k.replace("archon_search.install.SearchInstaller.", ""): v
            for k, v in base_patches.items()
            if "SearchInstaller." in k
        }):
            yield config_path


# ---------------------------------------------------------------------------
# Test 1: non-interactive minimal with skip_preload skips pre-warm
# ---------------------------------------------------------------------------


def test_run_non_interactive_minimal_skips_preload(tmp_path: Path) -> None:
    config_path = tmp_path / "archon-search.toml"
    fake_legacy = tmp_path / "fake.plist"

    prewarm_mock = MagicMock()
    input_mock = MagicMock()
    with (
        patch("archon_search.install.get_default_config_path", return_value=config_path),
        patch("archon_search.install._legacy_service_path", return_value=fake_legacy),
        patch("archon_search.install._remove_legacy_service"),
        patch("archon_search.install._prewarm_models", prewarm_mock),
        patch("archon_search.install._check_disk_space"),
        patch("builtins.input", input_mock),
        patch.object(SearchInstaller, "detect_gpu", return_value=GpuType.NONE),
        patch.object(SearchInstaller, "validate_providers", return_value=False),
        patch.object(SearchInstaller, "configure_providers"),
        patch.object(SearchInstaller, "write_service_file"),
        patch.object(SearchInstaller, "load_service", return_value=0),
        patch.object(SearchInstaller, "_wait_for_service", return_value=True),
        patch.object(SearchInstaller, "_is_service_running", return_value=False),
    ):
        installer = SearchInstaller(config_file=str(config_path))
        rc = installer.run(
            non_interactive=True,
            profile="minimal",
            skip_preload=True,
        )

    assert rc == 0
    prewarm_mock.assert_not_called()
    input_mock.assert_not_called()
    assert config_path.exists()
    content = config_path.read_text()
    assert "BAAI/bge-small-en-v1.5" in content


# ---------------------------------------------------------------------------
# Test 2: --force without --delete-db returns 1
# ---------------------------------------------------------------------------


def test_run_force_without_delete_db_returns_1(tmp_path: Path) -> None:
    config_path = tmp_path / "archon-search.toml"

    load_service_mock = MagicMock(return_value=0)
    with (
        patch("archon_search.install.get_default_config_path", return_value=config_path),
        patch.object(SearchInstaller, "load_service", load_service_mock),
    ):
        installer = SearchInstaller(config_file=str(config_path))
        rc = installer.run(
            non_interactive=True,
            profile="minimal",
            skip_preload=True,
            force=True,
            delete_db=False,
        )

    assert rc == 1
    load_service_mock.assert_not_called()


# ---------------------------------------------------------------------------
# Test 3: reinstall with same profile is idempotent (no NeedsForceDeleteError)
# ---------------------------------------------------------------------------


def test_run_reinstall_same_profile_is_idempotent(tmp_path: Path) -> None:
    config_path = tmp_path / "archon-search.toml"
    fake_legacy = tmp_path / "fake.plist"

    # Write a minimal config that matches the "minimal" profile
    from archon_search.install import _profile_toml
    config_path.write_text(_profile_toml("minimal", False))

    with (
        patch("archon_search.install.get_default_config_path", return_value=config_path),
        patch("archon_search.install._legacy_service_path", return_value=fake_legacy),
        patch("archon_search.install._remove_legacy_service"),
        patch("archon_search.install._prewarm_models"),
        patch("archon_search.install._check_disk_space"),
        patch.object(SearchInstaller, "detect_gpu", return_value=GpuType.NONE),
        patch.object(SearchInstaller, "validate_providers", return_value=False),
        patch.object(SearchInstaller, "configure_providers"),
        patch.object(SearchInstaller, "write_service_file"),
        patch.object(SearchInstaller, "load_service", return_value=0),
        patch.object(SearchInstaller, "_wait_for_service", return_value=True),
        patch.object(SearchInstaller, "_is_service_running", return_value=False),
    ):
        installer = SearchInstaller(config_file=str(config_path))
        rc = installer.run(
            non_interactive=True,
            profile="minimal",
            skip_preload=True,
        )

    assert rc == 0


# ---------------------------------------------------------------------------
# Test 4: reinstall different profile without --force returns 1
# ---------------------------------------------------------------------------


def test_run_reinstall_different_profile_no_force_returns_1(tmp_path: Path) -> None:
    config_path = tmp_path / "archon-search.toml"
    fake_legacy = tmp_path / "fake.plist"

    # Write minimal profile config
    from archon_search.install import _profile_toml
    config_path.write_text(_profile_toml("minimal", False))

    load_service_mock = MagicMock(return_value=0)
    with (
        patch("archon_search.install.get_default_config_path", return_value=config_path),
        patch("archon_search.install._legacy_service_path", return_value=fake_legacy),
        patch("archon_search.install._remove_legacy_service"),
        patch("archon_search.install._check_disk_space"),
        patch.object(SearchInstaller, "detect_gpu", return_value=GpuType.NONE),
        patch.object(SearchInstaller, "configure_providers"),
        patch.object(SearchInstaller, "load_service", load_service_mock),
        patch.object(SearchInstaller, "_wait_for_service", return_value=True),
        patch.object(SearchInstaller, "_is_service_running", return_value=False),
    ):
        installer = SearchInstaller(config_file=str(config_path))
        rc = installer.run(
            non_interactive=True,
            profile="max",  # different profile — different embedder + chunk_size
            skip_preload=True,
        )

    assert rc == 1
    load_service_mock.assert_not_called()


# ---------------------------------------------------------------------------
# Test 4b: reinstall different profile with --dry-run does NOT return 1
# ---------------------------------------------------------------------------


def test_run_reinstall_different_profile_dry_run_continues(tmp_path: Path, capsys) -> None:
    config_path = tmp_path / "archon-search.toml"
    fake_legacy = tmp_path / "fake.plist"

    # Write minimal profile config so an existing config is detected
    from archon_search.install import _profile_toml
    config_path.write_text(_profile_toml("minimal", False))

    load_service_mock = MagicMock(return_value=0)
    with (
        patch("archon_search.install.get_default_config_path", return_value=config_path),
        patch("archon_search.install._legacy_service_path", return_value=fake_legacy),
        patch("archon_search.install._remove_legacy_service"),
        patch("archon_search.install._check_disk_space"),
        patch.object(SearchInstaller, "detect_gpu", return_value=GpuType.NONE),
        patch.object(SearchInstaller, "configure_providers"),
        patch.object(SearchInstaller, "load_service", load_service_mock),
        patch.object(SearchInstaller, "_wait_for_service", return_value=True),
        patch.object(SearchInstaller, "_is_service_running", return_value=False),
    ):
        installer = SearchInstaller(config_file=str(config_path), dry_run=True)
        rc = installer.run(
            non_interactive=True,
            profile="max",  # different profile — triggers NeedsForceDeleteError
            skip_preload=True,
        )

    assert rc != 1, "dry-run should not exit 1 on model-mismatch guard"
    captured = capsys.readouterr()
    assert "[dry-run] Warning:" in captured.out


# ---------------------------------------------------------------------------
# Test 5: multilingual balanced non-interactive returns 1 (Jina license)
# ---------------------------------------------------------------------------


def test_run_jina_multilingual_non_interactive_returns_1(tmp_path: Path) -> None:
    config_path = tmp_path / "archon-search.toml"
    fake_legacy = tmp_path / "fake.plist"

    load_service_mock = MagicMock(return_value=0)
    with (
        patch("archon_search.install.get_default_config_path", return_value=config_path),
        patch("archon_search.install._legacy_service_path", return_value=fake_legacy),
        patch("archon_search.install._remove_legacy_service"),
        patch.object(SearchInstaller, "detect_gpu", return_value=GpuType.NONE),
        patch.object(SearchInstaller, "load_service", load_service_mock),
    ):
        installer = SearchInstaller(config_file=str(config_path))
        rc = installer.run(
            non_interactive=True,
            profile="balanced",
            multilingual=True,
            skip_preload=True,
        )

    assert rc == 1
    load_service_mock.assert_not_called()


# ---------------------------------------------------------------------------
# Test 6: disk space failure returns 1
# ---------------------------------------------------------------------------


def test_run_disk_space_failure_returns_1(tmp_path: Path) -> None:
    config_path = tmp_path / "archon-search.toml"
    fake_legacy = tmp_path / "fake.plist"

    load_service_mock = MagicMock(return_value=0)
    with (
        patch("archon_search.install.get_default_config_path", return_value=config_path),
        patch("archon_search.install._legacy_service_path", return_value=fake_legacy),
        patch("archon_search.install._remove_legacy_service"),
        patch("archon_search.install._check_disk_space", side_effect=InstallError("Insufficient disk")),
        patch.object(SearchInstaller, "detect_gpu", return_value=GpuType.NONE),
        patch.object(SearchInstaller, "validate_providers", return_value=False),
        patch.object(SearchInstaller, "configure_providers"),
        patch.object(SearchInstaller, "load_service", load_service_mock),
        patch.object(SearchInstaller, "_is_service_running", return_value=False),
    ):
        installer = SearchInstaller(config_file=str(config_path))
        rc = installer.run(
            non_interactive=True,
            profile="minimal",
            skip_preload=True,
        )

    assert rc == 1
    load_service_mock.assert_not_called()


# ---------------------------------------------------------------------------
# Test 7: idempotent reinstall, pre-warm failure → config restored to original
# ---------------------------------------------------------------------------


def test_run_prewarm_failure_returns_1(tmp_path: Path) -> None:
    import shutil as real_shutil

    config_path = tmp_path / "archon-search.toml"
    fake_legacy = tmp_path / "fake.plist"
    bak_path = config_path.with_suffix(".toml.bak")

    # Write existing minimal config
    from archon_search.install import _profile_toml
    original_content = _profile_toml("minimal", False)
    config_path.write_text(original_content)

    copy2_calls: list[tuple[str, str]] = []
    original_copy2 = real_shutil.copy2

    def spy_copy2(src: object, dst: object) -> object:
        copy2_calls.append((str(src), str(dst)))
        return original_copy2(src, dst)  # type: ignore[arg-type]

    with (
        patch("archon_search.install.get_default_config_path", return_value=config_path),
        patch("archon_search.install._legacy_service_path", return_value=fake_legacy),
        patch("archon_search.install._remove_legacy_service"),
        patch("archon_search.install._prewarm_models", side_effect=InstallError("Download failed")),
        patch("archon_search.install._check_disk_space"),
        patch("archon_search.install.shutil.copy2", side_effect=spy_copy2),
        patch.object(SearchInstaller, "detect_gpu", return_value=GpuType.NONE),
        patch.object(SearchInstaller, "validate_providers", return_value=False),
        patch.object(SearchInstaller, "configure_providers"),
        patch.object(SearchInstaller, "write_service_file"),
        patch.object(SearchInstaller, "load_service", return_value=0),
        patch.object(SearchInstaller, "_wait_for_service", return_value=True),
        patch.object(SearchInstaller, "_is_service_running", return_value=False),
    ):
        installer = SearchInstaller(config_file=str(config_path))
        rc = installer.run(
            non_interactive=True,
            profile="minimal",
            skip_preload=False,
        )

    assert rc == 1
    # Verify the backup was created then restored (proving rollback happened)
    restore_call = (str(bak_path), str(config_path))
    assert restore_call in copy2_calls, f"Expected restore {restore_call} in copy2 calls: {copy2_calls}"
    # Config should still exist with correct content
    assert config_path.exists()
    assert "BAAI/bge-small-en-v1.5" in config_path.read_text()


# ---------------------------------------------------------------------------
# Test 8: fresh install, pre-warm failure → config file and .bak removed
# ---------------------------------------------------------------------------


def test_run_fresh_install_prewarm_failure_cleans_up_config(tmp_path: Path) -> None:
    config_path = tmp_path / "archon-search.toml"
    fake_legacy = tmp_path / "fake.plist"
    bak_path = config_path.with_suffix(".toml.bak")

    # No existing config (fresh install)
    assert not config_path.exists()

    with (
        patch("archon_search.install.get_default_config_path", return_value=config_path),
        patch("archon_search.install._legacy_service_path", return_value=fake_legacy),
        patch("archon_search.install._remove_legacy_service"),
        patch("archon_search.install._prewarm_models", side_effect=InstallError("Download failed")),
        patch("archon_search.install._check_disk_space"),
        patch.object(SearchInstaller, "detect_gpu", return_value=GpuType.NONE),
        patch.object(SearchInstaller, "validate_providers", return_value=False),
        patch.object(SearchInstaller, "configure_providers"),
        patch.object(SearchInstaller, "write_service_file"),
        patch.object(SearchInstaller, "load_service", return_value=0),
        patch.object(SearchInstaller, "_wait_for_service", return_value=True),
        patch.object(SearchInstaller, "_is_service_running", return_value=False),
    ):
        installer = SearchInstaller(config_file=str(config_path))
        rc = installer.run(
            non_interactive=True,
            profile="minimal",
            skip_preload=False,
        )

    assert rc == 1
    assert not config_path.exists()
    assert not bak_path.exists()


# ---------------------------------------------------------------------------
# Test 9: force reinstall, pre-warm failure → config has NEW profile, .bak present
# ---------------------------------------------------------------------------


def test_run_force_reinstall_prewarm_failure_does_not_restore_old_backup(tmp_path: Path) -> None:
    config_path = tmp_path / "archon-search.toml"
    fake_legacy = tmp_path / "fake.plist"
    bak_path = config_path.with_suffix(".toml.bak")

    # Write existing minimal config
    from archon_search.install import _profile_toml
    config_path.write_text(_profile_toml("minimal", False))

    with (
        patch("archon_search.install.get_default_config_path", return_value=config_path),
        patch("archon_search.install._legacy_service_path", return_value=fake_legacy),
        patch("archon_search.install._remove_legacy_service"),
        patch("archon_search.install._prewarm_models", side_effect=InstallError("Download failed")),
        patch("archon_search.install._check_disk_space"),
        patch("archon_search.install.get_search_service", return_value=MagicMock()),
        patch.object(SearchInstaller, "detect_gpu", return_value=GpuType.NONE),
        patch.object(SearchInstaller, "validate_providers", return_value=False),
        patch.object(SearchInstaller, "configure_providers"),
        patch.object(SearchInstaller, "write_service_file"),
        patch.object(SearchInstaller, "load_service", return_value=0),
        patch.object(SearchInstaller, "_wait_for_service", return_value=True),
        patch.object(SearchInstaller, "_is_service_running", return_value=False),
    ):
        installer = SearchInstaller(config_file=str(config_path))
        rc = installer.run(
            non_interactive=True,
            profile="balanced",
            skip_preload=False,
            force=True,
            delete_db=True,
        )

    assert rc == 1
    # Config should have the NEW (balanced) profile after force reinstall writes it
    assert config_path.exists()
    assert "bge-base-en-v1.5" in config_path.read_text()
    # .bak should still be present (not cleaned up in force branch)
    assert bak_path.exists()


# ---------------------------------------------------------------------------
# Test 10: force + delete_db with different profile succeeds
# ---------------------------------------------------------------------------


def test_run_force_delete_db_different_profile_succeeds(tmp_path: Path) -> None:
    config_path = tmp_path / "archon-search.toml"
    fake_legacy = tmp_path / "fake.plist"
    db_path = tmp_path / "db"
    db_path.mkdir()  # simulate existing DB

    # Write existing minimal config
    from archon_search.install import _profile_toml
    from archon_search.config import load_config
    config_path.write_text(_profile_toml("minimal", False))

    # Read the db_path from the config before running (to verify rmtree target)
    existing_db_path = Path(load_config(config_path).db_path).expanduser()

    rmtree_mock = MagicMock()
    with (
        patch("archon_search.install.get_default_config_path", return_value=config_path),
        patch("archon_search.install._legacy_service_path", return_value=fake_legacy),
        patch("archon_search.install._remove_legacy_service"),
        patch("archon_search.install._prewarm_models"),
        patch("archon_search.install._check_disk_space"),
        patch("archon_search.install.shutil.rmtree", rmtree_mock),
        patch("archon_search.install.get_search_service", return_value=MagicMock()),
        patch.object(SearchInstaller, "detect_gpu", return_value=GpuType.NONE),
        patch.object(SearchInstaller, "validate_providers", return_value=False),
        patch.object(SearchInstaller, "configure_providers"),
        patch.object(SearchInstaller, "write_service_file"),
        patch.object(SearchInstaller, "load_service", return_value=0),
        patch.object(SearchInstaller, "_wait_for_service", return_value=True),
        patch.object(SearchInstaller, "_is_service_running", return_value=False),
    ):
        installer = SearchInstaller(config_file=str(config_path))
        rc = installer.run(
            non_interactive=True,
            profile="balanced",
            skip_preload=True,
            force=True,
            delete_db=True,
        )

    assert rc == 0
    assert config_path.exists()
    assert "bge-base-en-v1.5" in config_path.read_text()
    rmtree_mock.assert_called_once_with(existing_db_path)


# ---------------------------------------------------------------------------
# Test 11: run() creates the logs directory
# ---------------------------------------------------------------------------


def test_run_creates_log_directory(tmp_path: Path) -> None:
    config_path = tmp_path / "archon-search.toml"
    fake_legacy = tmp_path / "fake.plist"

    # Patch get_data_dir() to redirect log dir creation to tmp_path
    fake_data_dir = tmp_path / "data"
    fake_data_dir.mkdir()

    with (
        patch("archon_search.install.get_default_config_path", return_value=config_path),
        patch("archon_search.install._legacy_service_path", return_value=fake_legacy),
        patch("archon_search.install._remove_legacy_service"),
        patch("archon_search.install._prewarm_models"),
        patch("archon_search.install._check_disk_space"),
        patch("archon_search.install.get_data_dir", return_value=fake_data_dir),
        patch.object(SearchInstaller, "detect_gpu", return_value=GpuType.NONE),
        patch.object(SearchInstaller, "validate_providers", return_value=False),
        patch.object(SearchInstaller, "configure_providers"),
        patch.object(SearchInstaller, "write_service_file"),
        patch.object(SearchInstaller, "load_service", return_value=0),
        patch.object(SearchInstaller, "_wait_for_service", return_value=True),
        patch.object(SearchInstaller, "_is_service_running", return_value=False),
    ):
        installer = SearchInstaller(config_file=str(config_path))
        installer.run(
            non_interactive=True,
            profile="minimal",
            skip_preload=True,
        )

    expected_log_dir = fake_data_dir / "logs"
    assert expected_log_dir.exists()


# ---------------------------------------------------------------------------
# Test 12: run() calls _remove_legacy_service when legacy file exists
# ---------------------------------------------------------------------------


def test_run_calls_legacy_service_cleanup(tmp_path: Path) -> None:
    config_path = tmp_path / "archon-search.toml"

    # Create the fake legacy file so legacy.exists() returns True
    fake_legacy = tmp_path / "fake.plist"
    fake_legacy.touch()

    remove_legacy_mock = MagicMock()
    with (
        patch("archon_search.install.get_default_config_path", return_value=config_path),
        patch("archon_search.install._legacy_service_path", return_value=fake_legacy),
        patch("archon_search.install._remove_legacy_service", remove_legacy_mock),
        patch("archon_search.install._prewarm_models"),
        patch("archon_search.install._check_disk_space"),
        patch.object(SearchInstaller, "detect_gpu", return_value=GpuType.NONE),
        patch.object(SearchInstaller, "validate_providers", return_value=False),
        patch.object(SearchInstaller, "configure_providers"),
        patch.object(SearchInstaller, "write_service_file"),
        patch.object(SearchInstaller, "load_service", return_value=0),
        patch.object(SearchInstaller, "_wait_for_service", return_value=True),
        patch.object(SearchInstaller, "_is_service_running", return_value=False),
    ):
        installer = SearchInstaller(config_file=str(config_path))
        installer.run(
            non_interactive=True,
            profile="minimal",
            skip_preload=True,
        )

    remove_legacy_mock.assert_called_once_with(fake_legacy)


# ---------------------------------------------------------------------------
# run_register_and_start() — new method for `archon-search install`
# ---------------------------------------------------------------------------


def test_run_register_and_start_no_config_returns_1(tmp_path: Path) -> None:
    config_path = tmp_path / "archon-search.toml"
    assert not config_path.exists()

    installer = SearchInstaller(config_file=str(config_path))
    rc = installer.run_register_and_start()

    assert rc == 1


def test_run_register_and_start_with_config_registers_and_starts(tmp_path: Path) -> None:
    from archon_search.install import _profile_toml

    config_path = tmp_path / "archon-search.toml"
    config_path.write_text(_profile_toml("minimal", False))

    write_svc_mock = MagicMock()
    load_svc_mock = MagicMock(return_value=0)
    wait_mock = MagicMock(return_value=True)

    with (
        patch.object(SearchInstaller, "write_service_file", write_svc_mock),
        patch.object(SearchInstaller, "load_service", load_svc_mock),
        patch.object(SearchInstaller, "_wait_for_service", wait_mock),
    ):
        installer = SearchInstaller(config_file=str(config_path))
        rc = installer.run_register_and_start()

    assert rc == 0
    write_svc_mock.assert_called_once()
    load_svc_mock.assert_called_once()
    wait_mock.assert_called_once()


def test_run_register_and_start_service_start_failure_returns_nonzero(tmp_path: Path) -> None:
    from archon_search.install import _profile_toml

    config_path = tmp_path / "archon-search.toml"
    config_path.write_text(_profile_toml("minimal", False))

    with (
        patch.object(SearchInstaller, "write_service_file"),
        patch.object(SearchInstaller, "load_service", return_value=2),
        patch.object(SearchInstaller, "_wait_for_service", return_value=True),
    ):
        installer = SearchInstaller(config_file=str(config_path))
        rc = installer.run_register_and_start()

    assert rc == 2


def test_run_register_and_start_dry_run_skips_wait(tmp_path: Path) -> None:
    from archon_search.install import _profile_toml

    config_path = tmp_path / "archon-search.toml"
    config_path.write_text(_profile_toml("minimal", False))

    wait_mock = MagicMock(return_value=True)

    with (
        patch.object(SearchInstaller, "write_service_file"),
        patch.object(SearchInstaller, "load_service", return_value=0),
        patch.object(SearchInstaller, "_wait_for_service", wait_mock),
    ):
        installer = SearchInstaller(config_file=str(config_path), dry_run=True)
        rc = installer.run_register_and_start()

    assert rc == 0
    wait_mock.assert_not_called()


# ---------------------------------------------------------------------------
# Task 3.1 — New wiring tests
# ---------------------------------------------------------------------------


def test_run_prompts_multilingual_question(tmp_path: Path) -> None:
    """Wizard in interactive mode without --multilingual/--no-multilingual passes None to _prompt_multilingual."""
    config_path = tmp_path / "archon-search.toml"
    fake_legacy = tmp_path / "fake.plist"

    prompt_multilingual_mock = MagicMock(return_value=False)
    with (
        patch("archon_search.install.get_default_config_path", return_value=config_path),
        patch("archon_search.install._legacy_service_path", return_value=fake_legacy),
        patch("archon_search.install._remove_legacy_service"),
        patch("archon_search.install._prewarm_models"),
        patch("archon_search.install._check_disk_space"),
        patch("archon_search.install._prompt_multilingual", prompt_multilingual_mock),
        patch("archon_search.install._prompt_optional_features", return_value=MagicMock(
            install_code_extra=False, disable_reranker=False, enable_watch=False,
            enable_telemetry=False, eager_load_embedders=False,
            routing_strategy="centroid", log_format="text",
            host=None, port=None, db_path=None, log_level=None, log_to_stderr=False,
            top_k=None, telemetry_retention_days=None, enable_hyde=False, enable_rag_fusion=False,
        )),
        patch("archon_search.install._prompt_gpu_confirm", return_value=True),
        patch("builtins.input", return_value="y"),  # "Proceed?" prompt
        patch.object(SearchInstaller, "detect_gpu", return_value=GpuType.NONE),
        patch.object(SearchInstaller, "validate_providers", return_value=False),
        patch.object(SearchInstaller, "configure_providers"),
        patch.object(SearchInstaller, "write_service_file"),
        patch.object(SearchInstaller, "load_service", return_value=0),
        patch.object(SearchInstaller, "_wait_for_service", return_value=True),
        patch.object(SearchInstaller, "_is_service_running", return_value=False),
    ):
        installer = SearchInstaller(config_file=str(config_path))
        rc = installer.run(non_interactive=False, profile="minimal", multilingual=None, skip_preload=True)

    assert rc == 0
    prompt_multilingual_mock.assert_called_once_with(False, None)


def test_run_multilingual_flag_skips_prompt(tmp_path: Path) -> None:
    """multilingual=True is passed to _prompt_multilingual; the flag still takes precedence."""
    config_path = tmp_path / "archon-search.toml"
    fake_legacy = tmp_path / "fake.plist"

    prompt_multilingual_mock = MagicMock(return_value=True)
    with (
        patch("archon_search.install.get_default_config_path", return_value=config_path),
        patch("archon_search.install._legacy_service_path", return_value=fake_legacy),
        patch("archon_search.install._remove_legacy_service"),
        patch("archon_search.install._prewarm_models"),
        patch("archon_search.install._check_disk_space"),
        patch("archon_search.install._prompt_multilingual", prompt_multilingual_mock),
        patch("archon_search.install._prompt_optional_features", return_value=MagicMock(
            install_code_extra=False, disable_reranker=False, enable_watch=False,
            enable_telemetry=False, eager_load_embedders=False,
            routing_strategy="centroid", log_format="text",
            host=None, port=None, db_path=None, log_level=None, log_to_stderr=False,
            top_k=None, telemetry_retention_days=None, enable_hyde=False, enable_rag_fusion=False,
        )),
        patch("archon_search.install._prompt_gpu_confirm", return_value=True),
        patch("archon_search.install._prompt_fasttext_license"),
        patch("archon_search.install._download_fasttext_model"),
        patch.object(SearchInstaller, "detect_gpu", return_value=GpuType.NONE),
        patch.object(SearchInstaller, "validate_providers", return_value=False),
        patch.object(SearchInstaller, "configure_providers"),
        patch.object(SearchInstaller, "write_service_file"),
        patch.object(SearchInstaller, "load_service", return_value=0),
        patch.object(SearchInstaller, "_wait_for_service", return_value=True),
        patch.object(SearchInstaller, "_is_service_running", return_value=False),
    ):
        installer = SearchInstaller(config_file=str(config_path))
        # multilingual=True should be forwarded to _prompt_multilingual as flag_value=True
        rc = installer.run(non_interactive=True, profile="minimal", multilingual=True, skip_preload=True)

    assert rc == 0
    prompt_multilingual_mock.assert_called_once_with(True, True)


def test_run_optional_features_prompted(tmp_path: Path) -> None:
    """wizard calls _prompt_optional_features after profile selection."""
    config_path = tmp_path / "archon-search.toml"
    fake_legacy = tmp_path / "fake.plist"

    from archon_search.install import WizardFeatures
    features = WizardFeatures()
    prompt_features_mock = MagicMock(return_value=features)

    with (
        patch("archon_search.install.get_default_config_path", return_value=config_path),
        patch("archon_search.install._legacy_service_path", return_value=fake_legacy),
        patch("archon_search.install._remove_legacy_service"),
        patch("archon_search.install._prewarm_models"),
        patch("archon_search.install._check_disk_space"),
        patch("archon_search.install._prompt_multilingual", return_value=False),
        patch("archon_search.install._prompt_optional_features", prompt_features_mock),
        patch("archon_search.install._prompt_gpu_confirm", return_value=True),
        patch.object(SearchInstaller, "detect_gpu", return_value=GpuType.NONE),
        patch.object(SearchInstaller, "validate_providers", return_value=False),
        patch.object(SearchInstaller, "configure_providers"),
        patch.object(SearchInstaller, "write_service_file"),
        patch.object(SearchInstaller, "load_service", return_value=0),
        patch.object(SearchInstaller, "_wait_for_service", return_value=True),
        patch.object(SearchInstaller, "_is_service_running", return_value=False),
    ):
        installer = SearchInstaller(config_file=str(config_path))
        rc = installer.run(non_interactive=True, profile="minimal", skip_preload=True)

    assert rc == 0
    prompt_features_mock.assert_called_once()


def test_run_code_extra_installed_when_requested(tmp_path: Path) -> None:
    """install_code=True triggers _install_code_extra()."""
    config_path = tmp_path / "archon-search.toml"
    fake_legacy = tmp_path / "fake.plist"

    from archon_search.install import WizardFeatures
    features = WizardFeatures(install_code_extra=True)
    install_code_mock = MagicMock()

    with (
        patch("archon_search.install.get_default_config_path", return_value=config_path),
        patch("archon_search.install._legacy_service_path", return_value=fake_legacy),
        patch("archon_search.install._remove_legacy_service"),
        patch("archon_search.install._prewarm_models"),
        patch("archon_search.install._check_disk_space"),
        patch("archon_search.install._prompt_multilingual", return_value=False),
        patch("archon_search.install._prompt_optional_features", return_value=features),
        patch("archon_search.install._prompt_gpu_confirm", return_value=True),
        patch("archon_search.install._install_code_extra", install_code_mock),
        patch.object(SearchInstaller, "detect_gpu", return_value=GpuType.NONE),
        patch.object(SearchInstaller, "validate_providers", return_value=False),
        patch.object(SearchInstaller, "configure_providers"),
        patch.object(SearchInstaller, "write_service_file"),
        patch.object(SearchInstaller, "load_service", return_value=0),
        patch.object(SearchInstaller, "_wait_for_service", return_value=True),
        patch.object(SearchInstaller, "_is_service_running", return_value=False),
    ):
        installer = SearchInstaller(config_file=str(config_path))
        rc = installer.run(non_interactive=True, profile="minimal", skip_preload=True, install_code=True)

    assert rc == 0
    install_code_mock.assert_called_once()


def test_run_code_install_failure_is_non_fatal(tmp_path: Path) -> None:
    """_install_code_extra raising InstallError → run() continues and returns 0."""
    config_path = tmp_path / "archon-search.toml"
    fake_legacy = tmp_path / "fake.plist"

    from archon_search.install import WizardFeatures
    features = WizardFeatures(install_code_extra=True)

    with (
        patch("archon_search.install.get_default_config_path", return_value=config_path),
        patch("archon_search.install._legacy_service_path", return_value=fake_legacy),
        patch("archon_search.install._remove_legacy_service"),
        patch("archon_search.install._prewarm_models"),
        patch("archon_search.install._check_disk_space"),
        patch("archon_search.install._prompt_multilingual", return_value=False),
        patch("archon_search.install._prompt_optional_features", return_value=features),
        patch("archon_search.install._prompt_gpu_confirm", return_value=True),
        patch("archon_search.install._install_code_extra", side_effect=InstallError("pip failed")),
        patch.object(SearchInstaller, "detect_gpu", return_value=GpuType.NONE),
        patch.object(SearchInstaller, "validate_providers", return_value=False),
        patch.object(SearchInstaller, "configure_providers"),
        patch.object(SearchInstaller, "write_service_file"),
        patch.object(SearchInstaller, "load_service", return_value=0),
        patch.object(SearchInstaller, "_wait_for_service", return_value=True),
        patch.object(SearchInstaller, "_is_service_running", return_value=False),
    ):
        installer = SearchInstaller(config_file=str(config_path))
        rc = installer.run(non_interactive=True, profile="minimal", skip_preload=True, install_code=True)

    assert rc == 0


def test_run_gpu_confirm_decline_writes_cpu(tmp_path: Path) -> None:
    """disable_gpu=True → providers written as [] in config."""
    config_path = tmp_path / "archon-search.toml"
    fake_legacy = tmp_path / "fake.plist"

    from archon_search.install import WizardFeatures
    features = WizardFeatures()

    with (
        patch("archon_search.install.get_default_config_path", return_value=config_path),
        patch("archon_search.install._legacy_service_path", return_value=fake_legacy),
        patch("archon_search.install._remove_legacy_service"),
        patch("archon_search.install._prewarm_models"),
        patch("archon_search.install._check_disk_space"),
        patch("archon_search.install._prompt_multilingual", return_value=False),
        patch("archon_search.install._prompt_optional_features", return_value=features),
        patch("archon_search.install._prompt_gpu_confirm", return_value=False),
        patch.object(SearchInstaller, "detect_gpu", return_value=GpuType.METAL),
        patch.object(SearchInstaller, "validate_providers", return_value=False),
        patch.object(SearchInstaller, "configure_providers"),
        patch.object(SearchInstaller, "write_service_file"),
        patch.object(SearchInstaller, "load_service", return_value=0),
        patch.object(SearchInstaller, "_wait_for_service", return_value=True),
        patch.object(SearchInstaller, "_is_service_running", return_value=False),
    ):
        installer = SearchInstaller(config_file=str(config_path))
        rc = installer.run(non_interactive=True, profile="minimal", skip_preload=True, disable_gpu=True)

    assert rc == 0
    import tomlkit
    doc = tomlkit.parse(config_path.read_text())
    assert doc["database"]["providers"] == []


def test_run_non_interactive_uses_defaults(tmp_path: Path) -> None:
    """non_interactive=True skips all prompts; uses default WizardFeatures."""
    config_path = tmp_path / "archon-search.toml"
    fake_legacy = tmp_path / "fake.plist"

    from archon_search.install import WizardFeatures
    features = WizardFeatures()
    prompt_features_mock = MagicMock(return_value=features)
    prompt_multilingual_mock = MagicMock(return_value=False)
    prompt_gpu_mock = MagicMock(return_value=True)

    with (
        patch("archon_search.install.get_default_config_path", return_value=config_path),
        patch("archon_search.install._legacy_service_path", return_value=fake_legacy),
        patch("archon_search.install._remove_legacy_service"),
        patch("archon_search.install._prewarm_models"),
        patch("archon_search.install._check_disk_space"),
        patch("archon_search.install._prompt_multilingual", prompt_multilingual_mock),
        patch("archon_search.install._prompt_optional_features", prompt_features_mock),
        patch("archon_search.install._prompt_gpu_confirm", prompt_gpu_mock),
        patch.object(SearchInstaller, "detect_gpu", return_value=GpuType.NONE),
        patch.object(SearchInstaller, "validate_providers", return_value=False),
        patch.object(SearchInstaller, "configure_providers"),
        patch.object(SearchInstaller, "write_service_file"),
        patch.object(SearchInstaller, "load_service", return_value=0),
        patch.object(SearchInstaller, "_wait_for_service", return_value=True),
        patch.object(SearchInstaller, "_is_service_running", return_value=False),
    ):
        installer = SearchInstaller(config_file=str(config_path))
        rc = installer.run(non_interactive=True, profile="minimal", skip_preload=True)

    assert rc == 0
    # _prompt_multilingual receives non_interactive=True and flag_value=None (default)
    prompt_multilingual_mock.assert_called_once_with(True, None)
    # _prompt_optional_features receives non_interactive=True
    args, kwargs = prompt_features_mock.call_args
    assert args[0] is True  # non_interactive
    # _prompt_gpu_confirm receives non_interactive=True
    prompt_gpu_mock.assert_called_once_with(True, GpuType.NONE)


def test_run_disable_reranker_writes_empty_string(tmp_path: Path) -> None:
    """disable_reranker=True → load_config() shows reranker_model == ''."""
    config_path = tmp_path / "archon-search.toml"
    fake_legacy = tmp_path / "fake.plist"

    from archon_search.install import WizardFeatures
    from archon_search.config import load_config
    features = WizardFeatures(disable_reranker=True)

    with (
        patch("archon_search.install.get_default_config_path", return_value=config_path),
        patch("archon_search.install._legacy_service_path", return_value=fake_legacy),
        patch("archon_search.install._remove_legacy_service"),
        patch("archon_search.install._prewarm_models"),
        patch("archon_search.install._check_disk_space"),
        patch("archon_search.install._prompt_multilingual", return_value=False),
        patch("archon_search.install._prompt_optional_features", return_value=features),
        patch("archon_search.install._prompt_gpu_confirm", return_value=True),
        patch.object(SearchInstaller, "detect_gpu", return_value=GpuType.NONE),
        patch.object(SearchInstaller, "validate_providers", return_value=False),
        patch.object(SearchInstaller, "configure_providers"),
        patch.object(SearchInstaller, "write_service_file"),
        patch.object(SearchInstaller, "load_service", return_value=0),
        patch.object(SearchInstaller, "_wait_for_service", return_value=True),
        patch.object(SearchInstaller, "_is_service_running", return_value=False),
    ):
        installer = SearchInstaller(config_file=str(config_path))
        rc = installer.run(non_interactive=True, profile="balanced", skip_preload=True, disable_reranker=True)

    assert rc == 0
    cfg = load_config(config_path)
    assert cfg.reranker_model == ""


def test_run_watch_written_to_config(tmp_path: Path) -> None:
    """enable_watch=True → load_config() shows watch == True."""
    config_path = tmp_path / "archon-search.toml"
    fake_legacy = tmp_path / "fake.plist"

    from archon_search.install import WizardFeatures
    from archon_search.config import load_config
    features = WizardFeatures(enable_watch=True)

    with (
        patch("archon_search.install.get_default_config_path", return_value=config_path),
        patch("archon_search.install._legacy_service_path", return_value=fake_legacy),
        patch("archon_search.install._remove_legacy_service"),
        patch("archon_search.install._prewarm_models"),
        patch("archon_search.install._check_disk_space"),
        patch("archon_search.install._prompt_multilingual", return_value=False),
        patch("archon_search.install._prompt_optional_features", return_value=features),
        patch("archon_search.install._prompt_gpu_confirm", return_value=True),
        patch.object(SearchInstaller, "detect_gpu", return_value=GpuType.NONE),
        patch.object(SearchInstaller, "validate_providers", return_value=False),
        patch.object(SearchInstaller, "configure_providers"),
        patch.object(SearchInstaller, "write_service_file"),
        patch.object(SearchInstaller, "load_service", return_value=0),
        patch.object(SearchInstaller, "_wait_for_service", return_value=True),
        patch.object(SearchInstaller, "_is_service_running", return_value=False),
    ):
        installer = SearchInstaller(config_file=str(config_path))
        rc = installer.run(non_interactive=True, profile="minimal", skip_preload=True, enable_watch=True)

    assert rc == 0
    cfg = load_config(config_path)
    assert cfg.watch is True


def test_run_force_reinstall_preserves_features(tmp_path: Path) -> None:
    """force=True, delete_db=True, enable_watch=True → config has [collections].watch = true."""
    config_path = tmp_path / "archon-search.toml"
    fake_legacy = tmp_path / "fake.plist"

    from archon_search.install import WizardFeatures, _profile_toml
    from archon_search.config import load_config
    features = WizardFeatures(enable_watch=True)
    config_path.write_text(_profile_toml("minimal", False))

    with (
        patch("archon_search.install.get_default_config_path", return_value=config_path),
        patch("archon_search.install._legacy_service_path", return_value=fake_legacy),
        patch("archon_search.install._remove_legacy_service"),
        patch("archon_search.install._prewarm_models"),
        patch("archon_search.install._check_disk_space"),
        patch("archon_search.install._prompt_multilingual", return_value=False),
        patch("archon_search.install._prompt_optional_features", return_value=features),
        patch("archon_search.install._prompt_gpu_confirm", return_value=True),
        patch("archon_search.install.get_search_service", return_value=MagicMock()),
        patch("archon_search.install.shutil.rmtree", MagicMock()),
        patch.object(SearchInstaller, "detect_gpu", return_value=GpuType.NONE),
        patch.object(SearchInstaller, "validate_providers", return_value=False),
        patch.object(SearchInstaller, "configure_providers"),
        patch.object(SearchInstaller, "write_service_file"),
        patch.object(SearchInstaller, "load_service", return_value=0),
        patch.object(SearchInstaller, "_wait_for_service", return_value=True),
        patch.object(SearchInstaller, "_is_service_running", return_value=False),
    ):
        installer = SearchInstaller(config_file=str(config_path))
        rc = installer.run(
            non_interactive=True, profile="minimal", skip_preload=True,
            force=True, delete_db=True, enable_watch=True
        )

    assert rc == 0
    cfg = load_config(config_path)
    assert cfg.watch is True


def test_run_interactive_gpu_decline_writes_cpu(tmp_path: Path) -> None:
    """Interactive mode, detect_gpu() returns METAL, user declines → config has database.providers = []."""
    config_path = tmp_path / "archon-search.toml"
    fake_legacy = tmp_path / "fake.plist"

    from archon_search.install import WizardFeatures
    features = WizardFeatures()

    with (
        patch("archon_search.install.get_default_config_path", return_value=config_path),
        patch("archon_search.install._legacy_service_path", return_value=fake_legacy),
        patch("archon_search.install._remove_legacy_service"),
        patch("archon_search.install._prewarm_models"),
        patch("archon_search.install._check_disk_space"),
        patch("archon_search.install._prompt_multilingual", return_value=False),
        patch("archon_search.install._prompt_optional_features", return_value=features),
        patch("archon_search.install._prompt_gpu_confirm", return_value=False),  # user declines GPU
        patch("builtins.input", return_value="y"),  # "Proceed?" prompt
        patch.object(SearchInstaller, "detect_gpu", return_value=GpuType.METAL),
        patch.object(SearchInstaller, "validate_providers", return_value=False),
        patch.object(SearchInstaller, "configure_providers"),
        patch.object(SearchInstaller, "write_service_file"),
        patch.object(SearchInstaller, "load_service", return_value=0),
        patch.object(SearchInstaller, "_wait_for_service", return_value=True),
        patch.object(SearchInstaller, "_is_service_running", return_value=False),
    ):
        installer = SearchInstaller(config_file=str(config_path))
        rc = installer.run(non_interactive=False, profile="minimal", skip_preload=True)

    assert rc == 0
    import tomlkit as _tomlkit
    doc = _tomlkit.parse(config_path.read_text())
    assert doc["database"]["providers"] == []


# ---------------------------------------------------------------------------
# Task 3.1 — Prompt reordering tests
# ---------------------------------------------------------------------------

def _make_ordered_installer(tmp_path: Path):
    """Return (config_path, call_order_log, patches, method_patches) for ordering tests.

    Records the order in which _prompt_gpu_confirm, _prompt_jina_license,
    _prompt_fasttext_license, _prompt_optional_features, _write_profile_config,
    and configure_providers are called into a shared list.
    """
    from archon_search.install import WizardFeatures
    config_path = tmp_path / "archon-search.toml"
    fake_legacy = tmp_path / "fake.plist"
    call_log: list[str] = []

    def _log_side_effect(name: str, return_value=None):
        def side_effect(*args, **kwargs):
            call_log.append(name)
            return return_value
        return side_effect

    features = WizardFeatures()

    patches = {
        "archon_search.install.get_default_config_path": MagicMock(return_value=config_path),
        "archon_search.install._legacy_service_path": MagicMock(return_value=fake_legacy),
        "archon_search.install._remove_legacy_service": MagicMock(),
        "archon_search.install._prewarm_models": MagicMock(),
        "archon_search.install._check_disk_space": MagicMock(),
        "archon_search.install._prompt_multilingual": MagicMock(return_value=False),
        "archon_search.install._prompt_jina_license": MagicMock(
            side_effect=_log_side_effect("jina_license")
        ),
        "archon_search.install._prompt_fasttext_license": MagicMock(
            side_effect=_log_side_effect("fasttext_license")
        ),
        "archon_search.install._prompt_optional_features": MagicMock(
            side_effect=_log_side_effect("optional_features", features)
        ),
        "archon_search.install._prompt_gpu_confirm": MagicMock(
            side_effect=_log_side_effect("gpu_confirm", True)
        ),
        "archon_search.install._write_profile_config": MagicMock(
            side_effect=_log_side_effect("write_profile_config")
        ),
    }

    method_patches = {
        "detect_gpu": MagicMock(return_value=GpuType.NONE),
        "validate_providers": MagicMock(return_value=False),
        "configure_providers": MagicMock(side_effect=_log_side_effect("configure_providers")),
        "write_service_file": MagicMock(),
        "load_service": MagicMock(return_value=0),
        "_wait_for_service": MagicMock(return_value=True),
        "_is_service_running": MagicMock(return_value=False),
    }

    return config_path, call_log, patches, method_patches


def test_prompt_order_gpu_before_license(tmp_path: Path) -> None:
    """GPU confirmation must appear before Jina license gate in execution order."""
    config_path, call_log, patches, method_patches = _make_ordered_installer(tmp_path)

    with patch.multiple("archon_search.install", **{
        k.replace("archon_search.install.", ""): v for k, v in patches.items()
    }):
        with patch.multiple(SearchInstaller, **method_patches):
            installer = SearchInstaller(config_file=str(config_path))
            with patch("archon_search.install._requires_jina_license", return_value=True):
                rc = installer.run(
                    non_interactive=True,
                    profile="minimal",
                    skip_preload=True,
                    accept_jina_license=True,
                )

    assert rc == 0
    assert "gpu_confirm" in call_log
    assert "jina_license" in call_log
    assert call_log.index("gpu_confirm") < call_log.index("jina_license"), (
        f"Expected gpu_confirm before jina_license, got: {call_log}"
    )


def test_prompt_order_optional_features_after_license(tmp_path: Path) -> None:
    """Optional features prompt must appear after license gates in execution order."""
    config_path, call_log, patches, method_patches = _make_ordered_installer(tmp_path)

    with patch.multiple("archon_search.install", **{
        k.replace("archon_search.install.", ""): v for k, v in patches.items()
    }):
        with patch.multiple(SearchInstaller, **method_patches):
            installer = SearchInstaller(config_file=str(config_path))
            with patch("archon_search.install._requires_jina_license", return_value=True):
                rc = installer.run(
                    non_interactive=True,
                    profile="minimal",
                    skip_preload=True,
                    accept_jina_license=True,
                )

    assert rc == 0
    assert "optional_features" in call_log
    assert "jina_license" in call_log
    assert call_log.index("jina_license") < call_log.index("optional_features"), (
        f"Expected jina_license before optional_features, got: {call_log}"
    )


def test_gpu_prompt_before_config_write(tmp_path: Path) -> None:
    """GPU confirmation must happen before the profile config is written (idempotent branch)."""
    config_path, call_log, patches, method_patches = _make_ordered_installer(tmp_path)

    # Write existing config so idempotent (Branch C) path is taken,
    # which calls _write_profile_config (which we've patched to log)
    from archon_search.install import _profile_toml
    config_path.write_text(_profile_toml("minimal", False))

    with patch.multiple("archon_search.install", **{
        k.replace("archon_search.install.", ""): v for k, v in patches.items()
    }):
        with patch.multiple(SearchInstaller, **method_patches):
            installer = SearchInstaller(config_file=str(config_path))
            rc = installer.run(
                non_interactive=True,
                profile="minimal",
                skip_preload=True,
            )

    assert rc == 0
    assert "gpu_confirm" in call_log
    assert "write_profile_config" in call_log
    assert call_log.index("gpu_confirm") < call_log.index("write_profile_config"), (
        f"Expected gpu_confirm before write_profile_config, got: {call_log}"
    )


def test_configure_providers_after_config_write(tmp_path: Path) -> None:
    """configure_providers must be called after _write_profile_config (idempotent branch)."""
    config_path, call_log, patches, method_patches = _make_ordered_installer(tmp_path)

    # Write existing config so idempotent (Branch C) path is taken
    from archon_search.install import _profile_toml
    config_path.write_text(_profile_toml("minimal", False))

    # Use CUDA GPU so configure_providers actually fires
    method_patches["detect_gpu"] = MagicMock(return_value=GpuType.CUDA)

    with patch.multiple("archon_search.install", **{
        k.replace("archon_search.install.", ""): v for k, v in patches.items()
    }):
        with patch.multiple(SearchInstaller, **method_patches):
            installer = SearchInstaller(config_file=str(config_path))
            rc = installer.run(
                non_interactive=True,
                profile="minimal",
                skip_preload=True,
            )

    assert rc == 0
    assert "write_profile_config" in call_log
    assert "configure_providers" in call_log
    assert call_log.index("write_profile_config") < call_log.index("configure_providers"), (
        f"Expected write_profile_config before configure_providers, got: {call_log}"
    )


def test_reorder_non_interactive_still_succeeds(tmp_path: Path) -> None:
    """Full non-interactive run with all wizard flags returns exit code 0."""
    config_path = tmp_path / "archon-search.toml"
    fake_legacy = tmp_path / "fake.plist"

    from archon_search.install import WizardFeatures
    features = WizardFeatures()

    with (
        patch("archon_search.install.get_default_config_path", return_value=config_path),
        patch("archon_search.install._legacy_service_path", return_value=fake_legacy),
        patch("archon_search.install._remove_legacy_service"),
        patch("archon_search.install._prewarm_models"),
        patch("archon_search.install._check_disk_space"),
        patch("archon_search.install._prompt_multilingual", return_value=False),
        patch("archon_search.install._prompt_optional_features", return_value=features),
        patch("archon_search.install._prompt_gpu_confirm", return_value=True),
        patch.object(SearchInstaller, "detect_gpu", return_value=GpuType.NONE),
        patch.object(SearchInstaller, "validate_providers", return_value=False),
        patch.object(SearchInstaller, "configure_providers"),
        patch.object(SearchInstaller, "write_service_file"),
        patch.object(SearchInstaller, "load_service", return_value=0),
        patch.object(SearchInstaller, "_wait_for_service", return_value=True),
        patch.object(SearchInstaller, "_is_service_running", return_value=False),
    ):
        installer = SearchInstaller(config_file=str(config_path))
        rc = installer.run(
            non_interactive=True,
            profile="minimal",
            multilingual=False,
            skip_preload=True,
            install_code=False,
            disable_reranker=False,
            enable_watch=False,
            enable_telemetry=False,
            eager_load=False,
            routing_strategy="centroid",
            log_format="text",
            disable_gpu=False,
        )

    assert rc == 0


# ---------------------------------------------------------------------------
# Task 5.5 — Overwrite warning integration into Branch C
# ---------------------------------------------------------------------------


def _make_idempotent_config(tmp_path: Path, profile_name: str = "minimal") -> Path:
    """Write a wizard-default config for Branch C (idempotent re-run) tests."""
    from archon_search.install import _profile_toml
    config_path = tmp_path / "archon-search.toml"
    config_path.write_text(_profile_toml(profile_name, False))
    return config_path


def test_overwrite_warning_triggers_on_hand_edit(tmp_path: Path) -> None:
    """Branch C, hand-edit detected (via mock), interactive mode, user confirms → _write_profile_config called.

    Uses non_interactive=False to trigger the interactive overwrite prompt.
    _detect_config_hand_edits is mocked to return True (simulating a detected edit).
    multilingual=False is passed explicitly to avoid the multilingual prompt triggering
    the reinstall guard (English vs multilingual profile mismatch).
    _prompt_optional_features is also mocked to avoid complex input sequencing.
    """
    config_path = _make_idempotent_config(tmp_path)

    write_mock = MagicMock()

    with (
        patch("archon_search.install.get_default_config_path", return_value=config_path),
        patch("archon_search.install._legacy_service_path", return_value=tmp_path / "fake.plist"),
        patch("archon_search.install._remove_legacy_service"),
        patch("archon_search.install._prewarm_models"),
        patch("archon_search.install._check_disk_space"),
        patch("archon_search.install._write_profile_config", write_mock),
        patch("archon_search.install._detect_config_hand_edits", return_value=True),
        patch("archon_search.install._prompt_optional_features", return_value=MagicMock(
            install_code_extra=False, disable_reranker=False, enable_watch=False,
            enable_telemetry=False, eager_load_embedders=False,
            routing_strategy="centroid", log_format="text"
        )),
        # input() used for: overwrite prompt ("y") and Proceed? ("y")
        patch("builtins.input", return_value="y"),
        patch.object(SearchInstaller, "detect_gpu", return_value=GpuType.NONE),
        patch.object(SearchInstaller, "validate_providers", return_value=False),
        patch.object(SearchInstaller, "configure_providers"),
        patch.object(SearchInstaller, "write_service_file"),
        patch.object(SearchInstaller, "load_service", return_value=0),
        patch.object(SearchInstaller, "_wait_for_service", return_value=True),
        patch.object(SearchInstaller, "_is_service_running", return_value=False),
    ):
        installer = SearchInstaller(config_file=str(config_path))
        rc = installer.run(
            non_interactive=False,
            profile="minimal",
            multilingual=False,  # explicit to avoid reinstall guard mismatch
            skip_preload=True,
        )

    assert rc == 0
    write_mock.assert_called_once()


def test_overwrite_warning_aborts_on_n(tmp_path: Path) -> None:
    """Branch C, hand-edit detected, user answers 'n' to overwrite prompt → return code 1."""
    config_path = _make_idempotent_config(tmp_path)

    write_mock = MagicMock()

    with (
        patch("archon_search.install.get_default_config_path", return_value=config_path),
        patch("archon_search.install._legacy_service_path", return_value=tmp_path / "fake.plist"),
        patch("archon_search.install._remove_legacy_service"),
        patch("archon_search.install._prewarm_models"),
        patch("archon_search.install._check_disk_space"),
        patch("archon_search.install._write_profile_config", write_mock),
        patch("archon_search.install._detect_config_hand_edits", return_value=True),
        patch("archon_search.install._prompt_optional_features", return_value=MagicMock(
            install_code_extra=False, disable_reranker=False, enable_watch=False,
            enable_telemetry=False, eager_load_embedders=False,
            routing_strategy="centroid", log_format="text"
        )),
        # "n" causes the overwrite prompt to abort
        patch("builtins.input", return_value="n"),
        patch.object(SearchInstaller, "detect_gpu", return_value=GpuType.NONE),
        patch.object(SearchInstaller, "validate_providers", return_value=False),
        patch.object(SearchInstaller, "configure_providers"),
        patch.object(SearchInstaller, "write_service_file"),
        patch.object(SearchInstaller, "load_service", return_value=0),
        patch.object(SearchInstaller, "_wait_for_service", return_value=True),
    ):
        installer = SearchInstaller(config_file=str(config_path))
        rc = installer.run(
            non_interactive=False,
            profile="minimal",
            multilingual=False,
            skip_preload=True,
        )

    assert rc == 1
    write_mock.assert_not_called()


def test_overwrite_warning_bak_not_created_on_n(tmp_path: Path) -> None:
    """Branch C, hand-edit detected, user answers 'n' → .toml.bak not created."""
    config_path = _make_idempotent_config(tmp_path)
    bak_path = config_path.with_suffix(".toml.bak")

    with (
        patch("archon_search.install.get_default_config_path", return_value=config_path),
        patch("archon_search.install._legacy_service_path", return_value=tmp_path / "fake.plist"),
        patch("archon_search.install._remove_legacy_service"),
        patch("archon_search.install._prewarm_models"),
        patch("archon_search.install._check_disk_space"),
        patch("archon_search.install._detect_config_hand_edits", return_value=True),
        patch("archon_search.install._prompt_optional_features", return_value=MagicMock(
            install_code_extra=False, disable_reranker=False, enable_watch=False,
            enable_telemetry=False, eager_load_embedders=False,
            routing_strategy="centroid", log_format="text"
        )),
        patch("builtins.input", return_value="n"),
        patch.object(SearchInstaller, "detect_gpu", return_value=GpuType.NONE),
        patch.object(SearchInstaller, "validate_providers", return_value=False),
        patch.object(SearchInstaller, "configure_providers"),
        patch.object(SearchInstaller, "write_service_file"),
        patch.object(SearchInstaller, "load_service", return_value=0),
        patch.object(SearchInstaller, "_wait_for_service", return_value=True),
    ):
        installer = SearchInstaller(config_file=str(config_path))
        installer.run(
            non_interactive=False,
            profile="minimal",
            multilingual=False,
            skip_preload=True,
        )

    assert not bak_path.exists()


def test_overwrite_no_warning_on_clean_config(tmp_path: Path) -> None:
    """Branch C, no hand-edits detected → overwrite prompt NOT shown."""
    config_path = _make_idempotent_config(tmp_path)
    # No hand-edit — config is wizard defaults

    input_mock = MagicMock()

    with (
        patch("archon_search.install.get_default_config_path", return_value=config_path),
        patch("archon_search.install._legacy_service_path", return_value=tmp_path / "fake.plist"),
        patch("archon_search.install._remove_legacy_service"),
        patch("archon_search.install._prewarm_models"),
        patch("archon_search.install._check_disk_space"),
        # _detect_config_hand_edits returns False = no edits
        patch("archon_search.install._detect_config_hand_edits", return_value=False),
        patch("builtins.input", input_mock),
        patch.object(SearchInstaller, "detect_gpu", return_value=GpuType.NONE),
        patch.object(SearchInstaller, "validate_providers", return_value=False),
        patch.object(SearchInstaller, "configure_providers"),
        patch.object(SearchInstaller, "write_service_file"),
        patch.object(SearchInstaller, "load_service", return_value=0),
        patch.object(SearchInstaller, "_wait_for_service", return_value=True),
        patch.object(SearchInstaller, "_is_service_running", return_value=False),
    ):
        installer = SearchInstaller(config_file=str(config_path))
        rc = installer.run(
            non_interactive=True,  # non-interactive → no optional-feature prompts either
            profile="minimal",
            skip_preload=True,
        )

    assert rc == 0
    # No overwrite prompt (input should not be called)
    for call_args in input_mock.call_args_list:
        prompt_text = call_args.args[0] if call_args.args else ""
        assert "custom values" not in prompt_text, (
            f"Overwrite prompt shown unexpectedly: {prompt_text}"
        )


def test_overwrite_non_interactive_auto_accepts(tmp_path: Path) -> None:
    """Branch C, hand-edit detected, --non-interactive → auto-accepts without prompt."""
    config_path = _make_idempotent_config(tmp_path)
    # Do NOT call _hand_edit_config — that changes chunk_size triggering reinstall guard.
    # Mock _detect_config_hand_edits to simulate a soft hand-edit.

    write_mock = MagicMock()
    input_mock = MagicMock()

    with (
        patch("archon_search.install.get_default_config_path", return_value=config_path),
        patch("archon_search.install._legacy_service_path", return_value=tmp_path / "fake.plist"),
        patch("archon_search.install._remove_legacy_service"),
        patch("archon_search.install._prewarm_models"),
        patch("archon_search.install._check_disk_space"),
        patch("archon_search.install._write_profile_config", write_mock),
        patch("archon_search.install._detect_config_hand_edits", return_value=True),
        patch("builtins.input", input_mock),
        patch.object(SearchInstaller, "detect_gpu", return_value=GpuType.NONE),
        patch.object(SearchInstaller, "validate_providers", return_value=False),
        patch.object(SearchInstaller, "configure_providers"),
        patch.object(SearchInstaller, "write_service_file"),
        patch.object(SearchInstaller, "load_service", return_value=0),
        patch.object(SearchInstaller, "_wait_for_service", return_value=True),
        patch.object(SearchInstaller, "_is_service_running", return_value=False),
    ):
        installer = SearchInstaller(config_file=str(config_path))
        rc = installer.run(
            non_interactive=True,
            profile="minimal",
            skip_preload=True,
        )

    assert rc == 0
    write_mock.assert_called_once()
    # No overwrite prompt in non-interactive mode
    for call_args in input_mock.call_args_list:
        prompt_text = call_args.args[0] if call_args.args else ""
        assert "custom values" not in prompt_text


def test_overwrite_non_interactive_bak_still_created(tmp_path: Path) -> None:
    """Branch C, hand-edit detected, non-interactive auto-accept → .bak still created."""
    config_path = _make_idempotent_config(tmp_path)
    bak_path = config_path.with_suffix(".toml.bak")

    with (
        patch("archon_search.install.get_default_config_path", return_value=config_path),
        patch("archon_search.install._legacy_service_path", return_value=tmp_path / "fake.plist"),
        patch("archon_search.install._remove_legacy_service"),
        patch("archon_search.install._prewarm_models"),
        patch("archon_search.install._check_disk_space"),
        patch("archon_search.install._detect_config_hand_edits", return_value=True),
        patch.object(SearchInstaller, "detect_gpu", return_value=GpuType.NONE),
        patch.object(SearchInstaller, "validate_providers", return_value=False),
        patch.object(SearchInstaller, "configure_providers"),
        patch.object(SearchInstaller, "write_service_file"),
        patch.object(SearchInstaller, "load_service", return_value=0),
        patch.object(SearchInstaller, "_wait_for_service", return_value=True),
        patch.object(SearchInstaller, "_is_service_running", return_value=False),
    ):
        installer = SearchInstaller(config_file=str(config_path))
        rc = installer.run(
            non_interactive=True,
            profile="minimal",
            skip_preload=True,
        )

    assert rc == 0
    assert bak_path.exists()


def test_overwrite_dry_run_no_prompt_no_writes(tmp_path: Path) -> None:
    """--dry-run + hand-edit detected → no overwrite prompt shown, no writes made."""
    config_path = _make_idempotent_config(tmp_path)
    original_content = config_path.read_text()
    bak_path = config_path.with_suffix(".toml.bak")

    input_mock = MagicMock()

    with (
        patch("archon_search.install.get_default_config_path", return_value=config_path),
        patch("archon_search.install._legacy_service_path", return_value=tmp_path / "fake.plist"),
        patch("archon_search.install._remove_legacy_service"),
        patch("archon_search.install._prewarm_models"),
        patch("archon_search.install._check_disk_space"),
        patch("archon_search.install._detect_config_hand_edits", return_value=True),
        patch("builtins.input", input_mock),
        patch.object(SearchInstaller, "detect_gpu", return_value=GpuType.NONE),
        patch.object(SearchInstaller, "validate_providers", return_value=False),
        patch.object(SearchInstaller, "configure_providers"),
        patch.object(SearchInstaller, "write_service_file"),
        patch.object(SearchInstaller, "load_service", return_value=0),
        patch.object(SearchInstaller, "_wait_for_service", return_value=True),
        patch.object(SearchInstaller, "_is_service_running", return_value=False),
    ):
        installer = SearchInstaller(config_file=str(config_path), dry_run=True)
        rc = installer.run(
            non_interactive=True,  # non-interactive + dry-run
            profile="minimal",
            skip_preload=True,
        )

    assert rc == 0
    # No overwrite prompt
    for call_args in input_mock.call_args_list:
        prompt_text = call_args.args[0] if call_args.args else ""
        assert "custom values" not in prompt_text
    # Config unchanged
    assert config_path.read_text() == original_content
    # No .bak created
    assert not bak_path.exists()


def test_bak_content_integrity(tmp_path: Path) -> None:
    """Branch C with overwrite accepted (non-interactive) → .bak contains original config."""
    config_path = _make_idempotent_config(tmp_path)
    original_content = config_path.read_text()
    bak_path = config_path.with_suffix(".toml.bak")

    with (
        patch("archon_search.install.get_default_config_path", return_value=config_path),
        patch("archon_search.install._legacy_service_path", return_value=tmp_path / "fake.plist"),
        patch("archon_search.install._remove_legacy_service"),
        patch("archon_search.install._prewarm_models"),
        patch("archon_search.install._check_disk_space"),
        patch("archon_search.install._detect_config_hand_edits", return_value=True),
        patch.object(SearchInstaller, "detect_gpu", return_value=GpuType.NONE),
        patch.object(SearchInstaller, "validate_providers", return_value=False),
        patch.object(SearchInstaller, "configure_providers"),
        patch.object(SearchInstaller, "write_service_file"),
        patch.object(SearchInstaller, "load_service", return_value=0),
        patch.object(SearchInstaller, "_wait_for_service", return_value=True),
        patch.object(SearchInstaller, "_is_service_running", return_value=False),
    ):
        installer = SearchInstaller(config_file=str(config_path))
        rc = installer.run(
            non_interactive=True,
            profile="minimal",
            skip_preload=True,
        )

    assert rc == 0
    assert bak_path.exists()
    assert bak_path.read_text() == original_content


def test_overwrite_eof_on_prompt_aborts(tmp_path: Path, capsys) -> None:
    """Branch C, interactive mode, EOFError on overwrite prompt → aborts cleanly (return 1)."""
    config_path = _make_idempotent_config(tmp_path)

    write_mock = MagicMock()

    with (
        patch("archon_search.install.get_default_config_path", return_value=config_path),
        patch("archon_search.install._legacy_service_path", return_value=tmp_path / "fake.plist"),
        patch("archon_search.install._remove_legacy_service"),
        patch("archon_search.install._prewarm_models"),
        patch("archon_search.install._check_disk_space"),
        patch("archon_search.install._write_profile_config", write_mock),
        patch("archon_search.install._detect_config_hand_edits", return_value=True),
        patch("archon_search.install._prompt_optional_features", return_value=MagicMock(
            install_code_extra=False, disable_reranker=False, enable_watch=False,
            enable_telemetry=False, eager_load_embedders=False,
            routing_strategy="centroid", log_format="text"
        )),
        # EOFError on the overwrite prompt (piped/non-tty stdin)
        patch("builtins.input", side_effect=EOFError),
        patch.object(SearchInstaller, "detect_gpu", return_value=GpuType.NONE),
        patch.object(SearchInstaller, "validate_providers", return_value=False),
        patch.object(SearchInstaller, "configure_providers"),
        patch.object(SearchInstaller, "write_service_file"),
        patch.object(SearchInstaller, "load_service", return_value=0),
        patch.object(SearchInstaller, "_wait_for_service", return_value=True),
    ):
        installer = SearchInstaller(config_file=str(config_path))
        rc = installer.run(
            non_interactive=False,
            profile="minimal",
            multilingual=False,
            skip_preload=True,
        )

    assert rc == 1
    write_mock.assert_not_called()
    captured = capsys.readouterr()
    assert "Installation aborted" in captured.out


def test_overwrite_bak_location_printed_on_success(tmp_path: Path, capsys) -> None:
    """Branch C non-interactive overwrite → .bak file path is printed to stdout."""
    config_path = _make_idempotent_config(tmp_path)
    bak_path = config_path.with_suffix(".toml.bak")

    with (
        patch("archon_search.install.get_default_config_path", return_value=config_path),
        patch("archon_search.install._legacy_service_path", return_value=tmp_path / "fake.plist"),
        patch("archon_search.install._remove_legacy_service"),
        patch("archon_search.install._prewarm_models"),
        patch("archon_search.install._check_disk_space"),
        patch("archon_search.install._detect_config_hand_edits", return_value=True),
        patch.object(SearchInstaller, "detect_gpu", return_value=GpuType.NONE),
        patch.object(SearchInstaller, "validate_providers", return_value=False),
        patch.object(SearchInstaller, "configure_providers"),
        patch.object(SearchInstaller, "write_service_file"),
        patch.object(SearchInstaller, "load_service", return_value=0),
        patch.object(SearchInstaller, "_wait_for_service", return_value=True),
        patch.object(SearchInstaller, "_is_service_running", return_value=False),
    ):
        installer = SearchInstaller(config_file=str(config_path))
        rc = installer.run(
            non_interactive=True,
            profile="minimal",
            skip_preload=True,
        )

    assert rc == 0
    captured = capsys.readouterr()
    assert str(bak_path) in captured.out, (
        f"Expected .bak path {bak_path} in stdout. Got: {captured.out}"
    )


# ---------------------------------------------------------------------------
# Task 3.1 — Success output: full API key + source + "keep this key private"
# ---------------------------------------------------------------------------

_FAKE_KEY = "a" * 64  # 64-char lowercase hex (valid token_hex(32) output)


def _run_with_key_source(
    tmp_path: Path,
    key: str,
    source: str,
    dry_run: bool = False,
    capsys=None,
) -> tuple[int, str]:
    """Run installer with load_or_generate_key patched to return (key, source).

    Returns (rc, stdout_text).
    """
    config_path = tmp_path / "archon-search.toml"
    fake_legacy = tmp_path / "fake.plist"
    key_file = tmp_path / ".search.env"

    with (
        patch.dict(os.environ, {"ARCHON_SEARCH_DATA_DIR": str(tmp_path)}),
        patch("archon_search.install.get_default_config_path", return_value=config_path),
        patch("archon_search.install._legacy_service_path", return_value=fake_legacy),
        patch("archon_search.install._remove_legacy_service"),
        patch("archon_search.install._prewarm_models"),
        patch("archon_search.install._check_disk_space"),
        patch("archon_search.key_manager.load_or_generate_key", return_value=(key, source)),
        patch("archon_search.install.load_or_generate_key", return_value=(key, source)),
        patch.object(SearchInstaller, "detect_gpu", return_value=GpuType.NONE),
        patch.object(SearchInstaller, "validate_providers", return_value=False),
        patch.object(SearchInstaller, "configure_providers"),
        patch.object(SearchInstaller, "write_service_file"),
        patch.object(SearchInstaller, "load_service", return_value=0),
        patch.object(SearchInstaller, "_wait_for_service", return_value=True),
        patch.object(SearchInstaller, "_is_service_running", return_value=False),
    ):
        installer = SearchInstaller(config_file=str(config_path), dry_run=dry_run)
        rc = installer.run(
            non_interactive=True,
            profile="minimal",
            skip_preload=True,
        )

    captured = capsys.readouterr() if capsys else None
    stdout = captured.out if captured else ""
    return rc, stdout


def test_run_success_prints_full_key_env_var(tmp_path: Path, capsys) -> None:
    """Success output includes full key + env var source label + 'keep this key private'."""
    rc, stdout = _run_with_key_source(
        tmp_path,
        _FAKE_KEY,
        "env var",
        capsys=capsys,
    )

    assert rc == 0
    assert _FAKE_KEY in stdout
    assert "$ARCHON_SEARCH_API_KEY" in stdout
    assert "keep this key private" in stdout


def test_run_success_prints_full_key_auto_generated(tmp_path: Path, capsys) -> None:
    """Success output includes full key + 'generated fresh' + key file path."""
    key_file = tmp_path / ".search.env"

    rc, stdout = _run_with_key_source(
        tmp_path,
        _FAKE_KEY,
        "auto-generated",
        capsys=capsys,
    )

    assert rc == 0
    assert _FAKE_KEY in stdout
    assert "generated fresh" in stdout
    assert "keep this key private" in stdout


def test_run_success_prints_full_key_file(tmp_path: Path, capsys) -> None:
    """Success output for file source: full key + 'also stored at' + no env var reference."""
    key_file = tmp_path / ".search.env"
    source = f"file: {key_file}"

    rc, stdout = _run_with_key_source(
        tmp_path,
        _FAKE_KEY,
        source,
        capsys=capsys,
    )

    assert rc == 0
    assert _FAKE_KEY in stdout
    assert "keep this key private" in stdout
    assert "also stored at" in stdout
    assert "$ARCHON_SEARCH_API_KEY" not in stdout


def test_run_success_key_not_printed_in_dry_run(tmp_path: Path, capsys) -> None:
    """In dry-run mode, API key is NOT printed in success output."""
    rc, stdout = _run_with_key_source(
        tmp_path,
        _FAKE_KEY,
        "auto-generated",
        dry_run=True,
        capsys=capsys,
    )

    # dry_run returns before success block; key must not appear
    assert _FAKE_KEY not in stdout
