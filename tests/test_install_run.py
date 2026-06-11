"""Tests for SearchInstaller.run() — TDD (Task 3.4)."""
from __future__ import annotations

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

    # Patch Path.home() to redirect log dir creation to tmp_path
    fake_home = tmp_path / "home"
    fake_home.mkdir()

    with (
        patch("archon_search.install.get_default_config_path", return_value=config_path),
        patch("archon_search.install._legacy_service_path", return_value=fake_legacy),
        patch("archon_search.install._remove_legacy_service"),
        patch("archon_search.install._prewarm_models"),
        patch("archon_search.install._check_disk_space"),
        patch("archon_search.install.Path.home", return_value=fake_home),
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

    expected_log_dir = fake_home / ".archon-search" / "logs"
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
            routing_strategy="centroid", log_format="text"
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
            routing_strategy="centroid", log_format="text"
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
    import tomlkit
    doc = tomlkit.parse(config_path.read_text())
    assert doc["database"]["providers"] == []
