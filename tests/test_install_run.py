"""Tests for RealInstaller.run() — TDD (Task 3.4)."""
from __future__ import annotations

import logging
import os
import shutil
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from archon_search.install import (
    BaseInstaller,
    DryRunInstaller,
    InstallError,
    RealInstaller,
    WizardFeatures,
    create_installer,
)
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
        "archon_search.install.installer.get_default_config_path": MagicMock(return_value=config_path),
        "archon_search.install.installer._legacy_service_path": MagicMock(return_value=fake_legacy),
        "archon_search.install.installer._remove_legacy_service": MagicMock(),
        "archon_search.install.installer._prewarm_models": MagicMock(),
        "archon_search.install.installer._check_disk_space": MagicMock(),
        "archon_search.install.RealInstaller.detect_gpu": MagicMock(return_value=GpuType.NONE),
        "archon_search.install.RealInstaller.validate_providers": MagicMock(return_value=False),
        "archon_search.install.RealInstaller.configure_providers": MagicMock(),
        "archon_search.install.RealInstaller.write_service_file": MagicMock(),
        "archon_search.install.RealInstaller.load_service": MagicMock(return_value=0),
        "archon_search.install.RealInstaller._wait_for_service": MagicMock(return_value=True),
        "archon_search.install.RealInstaller._is_service_running": MagicMock(return_value=False),
    }
    base_patches.update(extra_patches)

    with patch.multiple("archon_search.install.installer", **{
        k.replace("archon_search.install.installer.", ""): v
        for k, v in base_patches.items()
        if k.startswith("archon_search.install.") and "RealInstaller." not in k
    }):
        with patch.multiple(RealInstaller, **{
            k.replace("archon_search.install.RealInstaller.", ""): v
            for k, v in base_patches.items()
            if "RealInstaller." in k
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
        patch("archon_search.install.installer.get_default_config_path", return_value=config_path),
        patch("archon_search.install.installer._legacy_service_path", return_value=fake_legacy),
        patch("archon_search.install.installer._remove_legacy_service"),
        patch("archon_search.install.installer._prewarm_models", prewarm_mock),
        patch("archon_search.install.installer._check_disk_space"),
        patch("builtins.input", input_mock),
        patch.object(BaseInstaller, "detect_gpu", return_value=GpuType.NONE),
        patch.object(BaseInstaller, "validate_providers", return_value=False),
        patch.object(RealInstaller, "configure_providers"),
        patch.object(RealInstaller, "write_service_file"),
        patch.object(RealInstaller, "load_service", return_value=0),
        patch.object(BaseInstaller, "_wait_for_service", return_value=True),
        patch.object(BaseInstaller, "_is_service_running", return_value=False),
    ):
        installer = create_installer(config_file=str(config_path))
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
        patch("archon_search.install.installer.get_default_config_path", return_value=config_path),
        patch.object(RealInstaller, "load_service", load_service_mock),
    ):
        installer = create_installer(config_file=str(config_path))
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
        patch("archon_search.install.installer.get_default_config_path", return_value=config_path),
        patch("archon_search.install.installer._legacy_service_path", return_value=fake_legacy),
        patch("archon_search.install.installer._remove_legacy_service"),
        patch("archon_search.install.installer._prewarm_models"),
        patch("archon_search.install.installer._check_disk_space"),
        patch.object(BaseInstaller, "detect_gpu", return_value=GpuType.NONE),
        patch.object(BaseInstaller, "validate_providers", return_value=False),
        patch.object(RealInstaller, "configure_providers"),
        patch.object(RealInstaller, "write_service_file"),
        patch.object(RealInstaller, "load_service", return_value=0),
        patch.object(BaseInstaller, "_wait_for_service", return_value=True),
        patch.object(BaseInstaller, "_is_service_running", return_value=False),
    ):
        installer = create_installer(config_file=str(config_path))
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
        patch("archon_search.install.installer.get_default_config_path", return_value=config_path),
        patch("archon_search.install.installer._legacy_service_path", return_value=fake_legacy),
        patch("archon_search.install.installer._remove_legacy_service"),
        patch("archon_search.install.installer._check_disk_space"),
        patch.object(BaseInstaller, "detect_gpu", return_value=GpuType.NONE),
        patch.object(RealInstaller, "configure_providers"),
        patch.object(RealInstaller, "load_service", load_service_mock),
        patch.object(BaseInstaller, "_wait_for_service", return_value=True),
        patch.object(BaseInstaller, "_is_service_running", return_value=False),
    ):
        installer = create_installer(config_file=str(config_path))
        rc = installer.run(
            non_interactive=True,
            profile="max",  # different profile — different embedder + chunk_size
            skip_preload=True,
        )

    assert rc == 1
    load_service_mock.assert_not_called()


# ---------------------------------------------------------------------------
# Test 4a-i: interactive reinstall, different profile, user confirms rebuild
# ---------------------------------------------------------------------------

# All optional-feature flags are pre-answered so the ONLY input() call left in an
# interactive run is the new reindex-confirmation prompt.
_PREANSWERED_FEATURES = dict(
    install_code=False,
    disable_reranker=False,
    enable_watch=False,
    enable_telemetry=False,
    eager_load=False,
    routing_strategy="centroid",
    log_format="text",
)


def test_run_interactive_guard_confirm_yes_reindexes(tmp_path: Path) -> None:
    config_path = tmp_path / "archon-search.toml"
    fake_legacy = tmp_path / "fake.plist"

    from archon_search.install import _profile_toml
    from archon_search.config import load_config
    config_path.write_text(_profile_toml("minimal", False))
    existing_db_path = Path(load_config(config_path).db_path).expanduser()
    existing_db_path.mkdir(parents=True, exist_ok=True)  # simulate an existing index on disk

    rmtree_mock = MagicMock()
    input_mock = MagicMock(return_value="yes")
    load_service_mock = MagicMock(return_value=0)
    with (
        patch("archon_search.install.installer.get_default_config_path", return_value=config_path),
        patch("archon_search.install.installer._legacy_service_path", return_value=fake_legacy),
        patch("archon_search.install.installer._remove_legacy_service"),
        patch("archon_search.install.installer._prewarm_models"),
        patch("archon_search.install.installer._check_disk_space"),
        # Otherwise a real `uv pip install` of the [hyde] extra re-resolves the test environment.
        patch("archon_search.install.installer._install_query_expansion_extras", return_value=[]),
        patch("archon_search.install.shutil.rmtree", rmtree_mock),
        patch("archon_search.install.installer.get_search_service", return_value=MagicMock()),
        patch("builtins.input", input_mock),
        patch.object(BaseInstaller, "detect_gpu", return_value=GpuType.NONE),
        patch.object(BaseInstaller, "validate_providers", return_value=False),
        patch.object(RealInstaller, "configure_providers"),
        patch.object(RealInstaller, "write_service_file"),
        patch.object(RealInstaller, "load_service", load_service_mock),
        patch.object(BaseInstaller, "_wait_for_service", return_value=True),
        patch.object(BaseInstaller, "_is_service_running", return_value=False),
    ):
        installer = create_installer(config_file=str(config_path))
        rc = installer.run(
            non_interactive=False,
            profile="balanced",  # different embedder — trips the guard
            multilingual=False,
            skip_preload=True,
            **_PREANSWERED_FEATURES,
        )

    assert rc == 0
    # The guard prompt fired exactly once, and the inner force-reinstall confirmation
    # is suppressed because we already confirmed interactively at the guard.
    guard_prompts = [c for c in input_mock.call_args_list if c.args and "Rebuild the index now?" in c.args[0]]
    inner_prompts = [c for c in input_mock.call_args_list if c.args and "Type 'yes' to confirm" in c.args[0]]
    assert len(guard_prompts) == 1
    assert inner_prompts == []
    assert "bge-base-en-v1.5" in config_path.read_text()
    rmtree_mock.assert_called_once_with(existing_db_path)


# ---------------------------------------------------------------------------
# Test 4a-ii: interactive reinstall, different profile, user declines rebuild
# ---------------------------------------------------------------------------


def test_run_interactive_guard_decline_keeps_config(tmp_path: Path) -> None:
    config_path = tmp_path / "archon-search.toml"
    fake_legacy = tmp_path / "fake.plist"

    from archon_search.install import _profile_toml
    config_path.write_text(_profile_toml("minimal", False))

    rmtree_mock = MagicMock()
    input_mock = MagicMock(return_value="no")
    load_service_mock = MagicMock(return_value=0)
    with (
        patch("archon_search.install.installer.get_default_config_path", return_value=config_path),
        patch("archon_search.install.installer._legacy_service_path", return_value=fake_legacy),
        patch("archon_search.install.installer._remove_legacy_service"),
        patch("archon_search.install.installer._prewarm_models"),
        patch("archon_search.install.installer._check_disk_space"),
        patch("archon_search.install.shutil.rmtree", rmtree_mock),
        patch("builtins.input", input_mock),
        patch.object(BaseInstaller, "detect_gpu", return_value=GpuType.NONE),
        patch.object(RealInstaller, "configure_providers"),
        patch.object(RealInstaller, "load_service", load_service_mock),
        patch.object(BaseInstaller, "_wait_for_service", return_value=True),
        patch.object(BaseInstaller, "_is_service_running", return_value=False),
    ):
        installer = create_installer(config_file=str(config_path))
        rc = installer.run(
            non_interactive=False,
            profile="balanced",
            multilingual=False,
            skip_preload=True,
            **_PREANSWERED_FEATURES,
        )

    # Clean abort: nothing deleted, config unchanged, service untouched.
    assert rc == 0
    rmtree_mock.assert_not_called()
    load_service_mock.assert_not_called()
    assert "bge-small-en-v1.5" in config_path.read_text()


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
        patch("archon_search.install.installer.get_default_config_path", return_value=config_path),
        patch("archon_search.install.installer._legacy_service_path", return_value=fake_legacy),
        patch("archon_search.install.installer._remove_legacy_service"),
        patch("archon_search.install.installer._check_disk_space"),
        patch.object(BaseInstaller, "detect_gpu", return_value=GpuType.NONE),
        patch.object(DryRunInstaller, "configure_providers"),
        patch.object(DryRunInstaller, "load_service", load_service_mock),
        patch.object(BaseInstaller, "_wait_for_service", return_value=True),
        patch.object(BaseInstaller, "_is_service_running", return_value=False),
    ):
        installer = create_installer(config_file=str(config_path), dry_run=True)
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
        patch("archon_search.install.installer.get_default_config_path", return_value=config_path),
        patch("archon_search.install.installer._legacy_service_path", return_value=fake_legacy),
        patch("archon_search.install.installer._remove_legacy_service"),
        patch.object(BaseInstaller, "detect_gpu", return_value=GpuType.NONE),
        patch.object(RealInstaller, "load_service", load_service_mock),
    ):
        installer = create_installer(config_file=str(config_path))
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
        patch("archon_search.install.installer.get_default_config_path", return_value=config_path),
        patch("archon_search.install.installer._legacy_service_path", return_value=fake_legacy),
        patch("archon_search.install.installer._remove_legacy_service"),
        patch("archon_search.install.installer._check_disk_space", side_effect=InstallError("Insufficient disk")),
        patch.object(BaseInstaller, "detect_gpu", return_value=GpuType.NONE),
        patch.object(BaseInstaller, "validate_providers", return_value=False),
        patch.object(RealInstaller, "configure_providers"),
        patch.object(RealInstaller, "load_service", load_service_mock),
        patch.object(BaseInstaller, "_is_service_running", return_value=False),
    ):
        installer = create_installer(config_file=str(config_path))
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
        patch("archon_search.install.installer.get_default_config_path", return_value=config_path),
        patch("archon_search.install.installer._legacy_service_path", return_value=fake_legacy),
        patch("archon_search.install.installer._remove_legacy_service"),
        patch("archon_search.install.installer._prewarm_models", side_effect=InstallError("Download failed")),
        patch("archon_search.install.installer._check_disk_space"),
        patch("archon_search.install.shutil.copy2", side_effect=spy_copy2),
        patch.object(BaseInstaller, "detect_gpu", return_value=GpuType.NONE),
        patch.object(BaseInstaller, "validate_providers", return_value=False),
        patch.object(RealInstaller, "configure_providers"),
        patch.object(RealInstaller, "write_service_file"),
        patch.object(RealInstaller, "load_service", return_value=0),
        patch.object(BaseInstaller, "_wait_for_service", return_value=True),
        patch.object(BaseInstaller, "_is_service_running", return_value=False),
    ):
        installer = create_installer(config_file=str(config_path))
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
        patch("archon_search.install.installer.get_default_config_path", return_value=config_path),
        patch("archon_search.install.installer._legacy_service_path", return_value=fake_legacy),
        patch("archon_search.install.installer._remove_legacy_service"),
        patch("archon_search.install.installer._prewarm_models", side_effect=InstallError("Download failed")),
        patch("archon_search.install.installer._check_disk_space"),
        patch.object(BaseInstaller, "detect_gpu", return_value=GpuType.NONE),
        patch.object(BaseInstaller, "validate_providers", return_value=False),
        patch.object(RealInstaller, "configure_providers"),
        patch.object(RealInstaller, "write_service_file"),
        patch.object(RealInstaller, "load_service", return_value=0),
        patch.object(BaseInstaller, "_wait_for_service", return_value=True),
        patch.object(BaseInstaller, "_is_service_running", return_value=False),
    ):
        installer = create_installer(config_file=str(config_path))
        rc = installer.run(
            non_interactive=True,
            profile="minimal",
            skip_preload=False,
        )

    assert rc == 1
    assert not config_path.exists()
    assert not bak_path.exists()


def test_run_prewarm_failure_does_not_write_the_server_key(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """--server-key is written only once the embedder pre-warm has passed: a failed
    install (exit 1) leaves the key file as it was."""
    config_path = tmp_path / "archon-search.toml"

    with (
        patch("archon_search.install.installer.get_default_config_path", return_value=config_path),
        patch("archon_search.install.installer._legacy_service_path", return_value=tmp_path / "fake.plist"),
        patch("archon_search.install.installer._remove_legacy_service"),
        patch("archon_search.install.installer._prewarm_models", side_effect=InstallError("cannot be loaded")),
        patch("archon_search.install.installer._check_disk_space"),
        patch.object(BaseInstaller, "detect_gpu", return_value=GpuType.NONE),
        patch.object(RealInstaller, "configure_providers"),
        patch.object(RealInstaller, "write_server_key") as write_server_key,
        patch.object(RealInstaller, "write_service_file"),
        patch.object(RealInstaller, "load_service", return_value=0),
        patch.object(BaseInstaller, "_wait_for_service", return_value=True),
        patch.object(BaseInstaller, "_is_service_running", return_value=False),
    ):
        installer = create_installer(config_file=str(config_path))
        rc = installer.run(
            non_interactive=True, profile="minimal", skip_preload=False, server_key="a" * 32
        )

    assert rc == 1
    assert "Model pre-warm failed: cannot be loaded" in capsys.readouterr().err
    write_server_key.assert_not_called()


def test_run_writes_the_server_key_after_prewarm_and_before_the_service_starts(
    tmp_path: Path,
) -> None:
    """A passed pre-warm unlocks the --server-key write, and the key is on disk before
    the service is registered and started (it reads the key file at start)."""
    config_path, call_log, patches, method_patches = _make_ordered_installer(tmp_path)

    def _log(name: str, result: Any = None):
        def side_effect(*_args: Any, **_kwargs: Any) -> Any:
            call_log.append(name)
            return result
        return side_effect

    patches["archon_search.install.installer._prewarm_models"] = MagicMock(side_effect=_log("prewarm"))
    method_patches["write_server_key"] = MagicMock(side_effect=_log("write_server_key"))
    method_patches["register_and_start"] = MagicMock(side_effect=_log("register_and_start", 0))

    with patch.multiple("archon_search.install.installer", **{
        k.replace("archon_search.install.installer.", ""): v for k, v in patches.items()
    }):
        with patch.multiple(RealInstaller, **method_patches):
            rc = create_installer(config_file=str(config_path)).run(
                non_interactive=True, profile="minimal", skip_preload=False, server_key="a" * 32
            )

    assert rc == 0
    method_patches["write_server_key"].assert_called_once_with("a" * 32)
    steps = [name for name in call_log if name in {"prewarm", "write_server_key", "register_and_start"}]
    assert steps == ["prewarm", "write_server_key", "register_and_start"], call_log


def _real_prewarm_run_patches(config_path: Path, tmp_path: Path, load_service: MagicMock):
    """Patch set for a CPU wizard run that reaches the real _prewarm_models."""
    return (
        patch("archon_search.install.installer.get_default_config_path", return_value=config_path),
        patch("archon_search.install.installer._legacy_service_path", return_value=tmp_path / "fake.plist"),
        patch("archon_search.install.installer._remove_legacy_service"),
        patch("archon_search.install.installer._check_disk_space"),
        patch.object(BaseInstaller, "detect_gpu", return_value=GpuType.NONE),
        patch.object(BaseInstaller, "validate_providers", return_value=False),
        patch.object(RealInstaller, "write_service_file"),
        patch.object(RealInstaller, "load_service", load_service),
        patch.object(BaseInstaller, "_wait_for_service", return_value=True),
        patch.object(BaseInstaller, "_is_service_running", return_value=False),
    )


def test_run_fresh_install_model_load_failure_exits_1_naming_model_and_cause(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """S671: the embedder's files download but onnxruntime cannot build a session.
    The wizard must fail at install time — exit 1, name the model and the cause,
    remove the fresh config — rather than start a service whose every ingest fails."""
    import sys
    from contextlib import ExitStack

    from archon_search.profiles import get_profile
    from tests.test_install_prewarm import _fastembed_cls, _fastembed_modules

    config_path = tmp_path / "archon-search.toml"
    load_error = "[ONNXRuntimeError] : 1 : FAIL : External data path escapes model directory."
    fastembed = _fastembed_modules(
        _fastembed_cls(session_error=RuntimeError(load_error)), _fastembed_cls()
    )
    load_service = MagicMock(return_value=0)

    with ExitStack() as stack:
        stack.enter_context(patch.dict(sys.modules, fastembed))
        for p in _real_prewarm_run_patches(config_path, tmp_path, load_service):
            stack.enter_context(p)
        installer = create_installer(config_file=str(config_path))
        rc = installer.run(non_interactive=True, profile="minimal", skip_preload=False)

    stderr = capsys.readouterr().err
    embedder = get_profile("minimal", False).embedder
    assert rc == 1
    assert (
        f"Model pre-warm failed: The embedder model {embedder!r} was downloaded but cannot be loaded: "
        f"{load_error}"
    ) in stderr
    assert not config_path.exists()
    load_service.assert_not_called()


def test_run_reranker_load_failure_warns_and_starts_service(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """S202: a reranker that cannot load does not fail the install — the service starts,
    and the warning names the reranker and the cause."""
    import sys
    from contextlib import ExitStack

    from archon_search.profiles import get_profile
    from tests.test_install_prewarm import _fastembed_cls, _fastembed_modules

    config_path = tmp_path / "archon-search.toml"
    load_error = "[ONNXRuntimeError] : 7 : INVALID_PROTOBUF : Protobuf parsing failed."
    fastembed = _fastembed_modules(
        _fastembed_cls(), _fastembed_cls(inference_error=RuntimeError(load_error))
    )
    load_service = MagicMock(return_value=0)

    with ExitStack() as stack, caplog.at_level(logging.WARNING, logger="archon_search.install"):
        stack.enter_context(patch.dict(sys.modules, fastembed))
        for p in _real_prewarm_run_patches(config_path, tmp_path, load_service):
            stack.enter_context(p)
        installer = create_installer(config_file=str(config_path))
        rc = installer.run(non_interactive=True, profile="minimal", skip_preload=False)

    reranker = get_profile("minimal", False).reranker
    assert rc == 0
    load_service.assert_called_once()
    assert any(
        f"The reranker model {reranker!r} was downloaded but cannot be loaded: {load_error}"
        in r.getMessage()
        for r in caplog.records
    ), [r.getMessage() for r in caplog.records]


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
        patch("archon_search.install.installer.get_default_config_path", return_value=config_path),
        patch("archon_search.install.installer._legacy_service_path", return_value=fake_legacy),
        patch("archon_search.install.installer._remove_legacy_service"),
        patch("archon_search.install.installer._prewarm_models", side_effect=InstallError("Download failed")),
        patch("archon_search.install.installer._check_disk_space"),
        patch("archon_search.install.installer.get_search_service", return_value=MagicMock()),
        patch.object(BaseInstaller, "detect_gpu", return_value=GpuType.NONE),
        patch.object(BaseInstaller, "validate_providers", return_value=False),
        patch.object(RealInstaller, "configure_providers"),
        patch.object(RealInstaller, "write_service_file"),
        patch.object(RealInstaller, "load_service", return_value=0),
        patch.object(BaseInstaller, "_wait_for_service", return_value=True),
        patch.object(BaseInstaller, "_is_service_running", return_value=False),
    ):
        installer = create_installer(config_file=str(config_path))
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
        patch("archon_search.install.installer.get_default_config_path", return_value=config_path),
        patch("archon_search.install.installer._legacy_service_path", return_value=fake_legacy),
        patch("archon_search.install.installer._remove_legacy_service"),
        patch("archon_search.install.installer._prewarm_models"),
        patch("archon_search.install.installer._check_disk_space"),
        patch("archon_search.install.shutil.rmtree", rmtree_mock),
        patch("archon_search.install.installer.get_search_service", return_value=MagicMock()),
        patch.object(BaseInstaller, "detect_gpu", return_value=GpuType.NONE),
        patch.object(BaseInstaller, "validate_providers", return_value=False),
        patch.object(RealInstaller, "configure_providers"),
        patch.object(RealInstaller, "write_service_file"),
        patch.object(RealInstaller, "load_service", return_value=0),
        patch.object(BaseInstaller, "_wait_for_service", return_value=True),
        patch.object(BaseInstaller, "_is_service_running", return_value=False),
    ):
        installer = create_installer(config_file=str(config_path))
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
        patch("archon_search.install.installer.get_default_config_path", return_value=config_path),
        patch("archon_search.install.installer._legacy_service_path", return_value=fake_legacy),
        patch("archon_search.install.installer._remove_legacy_service"),
        patch("archon_search.install.installer._prewarm_models"),
        patch("archon_search.install.installer._check_disk_space"),
        patch("archon_search.install.installer.get_data_dir", return_value=fake_data_dir),
        patch.object(BaseInstaller, "detect_gpu", return_value=GpuType.NONE),
        patch.object(BaseInstaller, "validate_providers", return_value=False),
        patch.object(RealInstaller, "configure_providers"),
        patch.object(RealInstaller, "write_service_file"),
        patch.object(RealInstaller, "load_service", return_value=0),
        patch.object(BaseInstaller, "_wait_for_service", return_value=True),
        patch.object(BaseInstaller, "_is_service_running", return_value=False),
    ):
        installer = create_installer(config_file=str(config_path))
        installer.run(
            non_interactive=True,
            profile="minimal",
            skip_preload=True,
        )

    expected_log_dir = fake_data_dir / "logs"
    assert expected_log_dir.exists()


# ---------------------------------------------------------------------------
# Test 12: run() never calls _remove_legacy_service, even when a legacy file exists.
# pre_activate_cleanup() → stop() → _wait_until_stopped() handles service transition
# correctly with proper waiting; bare _remove_legacy_service() skips the wait and
# causes a port-hold race that times out the wizard (S194, S201, S202).
# ---------------------------------------------------------------------------


def test_run_does_not_call_legacy_service_cleanup(tmp_path: Path) -> None:
    config_path = tmp_path / "archon-search.toml"

    # Create the fake legacy file so it exists on disk
    fake_legacy = tmp_path / "fake.plist"
    fake_legacy.touch()

    remove_legacy_mock = MagicMock()
    with (
        patch("archon_search.install.installer.get_default_config_path", return_value=config_path),
        patch("archon_search.install.installer._legacy_service_path", return_value=fake_legacy),
        patch("archon_search.install.installer._remove_legacy_service", remove_legacy_mock),
        patch("archon_search.install.installer._prewarm_models"),
        patch("archon_search.install.installer._check_disk_space"),
        patch.object(BaseInstaller, "detect_gpu", return_value=GpuType.NONE),
        patch.object(BaseInstaller, "validate_providers", return_value=False),
        patch.object(RealInstaller, "configure_providers"),
        patch.object(RealInstaller, "write_service_file"),
        patch.object(RealInstaller, "load_service", return_value=0),
        patch.object(BaseInstaller, "_wait_for_service", return_value=True),
        patch.object(BaseInstaller, "_is_service_running", return_value=False),
    ):
        installer = create_installer(config_file=str(config_path))
        installer.run(
            non_interactive=True,
            profile="minimal",
            skip_preload=True,
        )

    # _remove_legacy_service must NEVER be called from run() — the race-free
    # service transition goes through pre_activate_cleanup() → stop() → _wait_until_stopped().
    remove_legacy_mock.assert_not_called()


# ---------------------------------------------------------------------------
# Test 12b (S199): an aborting reinstall guard must NOT remove the legacy
# service. Removing it in Step 0 before the guard leaves the running service
# dismantled and unrecoverable by `start` once the guard refuses the change.
# ---------------------------------------------------------------------------


def test_run_guard_abort_does_not_remove_legacy_service(tmp_path: Path) -> None:
    config_path = tmp_path / "archon-search.toml"

    # A REAL existing legacy file so legacy.exists() is True (a nonexistent path
    # would make this test vacuous — the Step-0 branch would be skipped anyway).
    fake_legacy = tmp_path / "fake.plist"
    fake_legacy.touch()

    # Existing minimal-profile config so the reinstall guard fires on a switch.
    from archon_search.install import _profile_toml
    config_path.write_text(_profile_toml("minimal", False))

    remove_legacy_mock = MagicMock()
    load_service_mock = MagicMock(return_value=0)
    with (
        patch("archon_search.install.installer.get_default_config_path", return_value=config_path),
        patch("archon_search.install.installer._legacy_service_path", return_value=fake_legacy),
        patch("archon_search.install.installer._remove_legacy_service", remove_legacy_mock),
        patch("archon_search.install.installer._check_disk_space"),
        patch.object(BaseInstaller, "detect_gpu", return_value=GpuType.NONE),
        patch.object(RealInstaller, "configure_providers"),
        patch.object(RealInstaller, "write_service_file"),
        patch.object(RealInstaller, "load_service", load_service_mock),
        patch.object(BaseInstaller, "_wait_for_service", return_value=True),
        patch.object(BaseInstaller, "_is_service_running", return_value=False),
    ):
        installer = create_installer(config_file=str(config_path))
        rc = installer.run(
            non_interactive=True,
            profile="max",  # different embedder + chunk_size → guard aborts
            skip_preload=True,
        )

    assert rc == 1
    # The service must be left exactly as found: the guard refused the change,
    # so the legacy service must NOT have been dismantled.
    remove_legacy_mock.assert_not_called()
    # And the legacy file itself must still be present on disk.
    assert fake_legacy.exists()
    # Nothing downstream should have run either.
    load_service_mock.assert_not_called()


# ---------------------------------------------------------------------------
# run_register_and_start() — new method for `archon-search install`
# ---------------------------------------------------------------------------


def test_run_register_and_start_no_config_returns_1(tmp_path: Path) -> None:
    config_path = tmp_path / "archon-search.toml"
    assert not config_path.exists()

    installer = create_installer(config_file=str(config_path))
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
        patch.object(RealInstaller, "write_service_file", write_svc_mock),
        patch.object(RealInstaller, "load_service", load_svc_mock),
        patch.object(BaseInstaller, "_wait_for_service", wait_mock),
    ):
        installer = create_installer(config_file=str(config_path))
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
        patch.object(RealInstaller, "write_service_file"),
        patch.object(RealInstaller, "load_service", return_value=2),
        patch.object(BaseInstaller, "_wait_for_service", return_value=True),
    ):
        installer = create_installer(config_file=str(config_path))
        rc = installer.run_register_and_start()

    assert rc == 2


def test_run_register_and_start_dry_run_skips_wait(tmp_path: Path) -> None:
    from archon_search.install import _profile_toml

    config_path = tmp_path / "archon-search.toml"
    config_path.write_text(_profile_toml("minimal", False))

    wait_mock = MagicMock(return_value=True)

    with (
        patch.object(DryRunInstaller, "write_service_file"),
        patch.object(DryRunInstaller, "load_service", return_value=0),
        patch.object(BaseInstaller, "_wait_for_service", wait_mock),
    ):
        installer = create_installer(config_file=str(config_path), dry_run=True)
        rc = installer.run_register_and_start()

    assert rc == 0
    wait_mock.assert_not_called()


def test_run_register_and_start_dry_run_output_marks_simulation(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """S197: dry-run completion output must be marked as a simulation.

    A bare past-tense "archon-search service registered and running." is
    indistinguishable from a real install that just succeeded. In dry-run mode
    the output must carry the DryRunInstaller "[DRY RUN]" convention instead.
    """
    from archon_search.install import _profile_toml

    config_path = tmp_path / "archon-search.toml"
    config_path.write_text(_profile_toml("minimal", False))

    with (
        patch.object(DryRunInstaller, "write_service_file"),
        patch.object(DryRunInstaller, "load_service", return_value=0),
        patch.object(BaseInstaller, "_wait_for_service", return_value=True),
    ):
        installer = create_installer(config_file=str(config_path), dry_run=True)
        rc = installer.run_register_and_start()

    assert rc == 0
    out = capsys.readouterr().out
    assert "[DRY RUN]" in out, f"dry-run completion output missing marker; got:\n{out}"
    assert "archon-search service registered and running." not in out, (
        f"bare past-tense state report emitted in dry-run mode; got:\n{out}"
    )


# ---------------------------------------------------------------------------
# Task 3.1 — New wiring tests
# ---------------------------------------------------------------------------


def test_run_prompts_multilingual_question(tmp_path: Path) -> None:
    """Wizard in interactive mode without --multilingual/--no-multilingual passes None to _prompt_multilingual."""
    config_path = tmp_path / "archon-search.toml"
    fake_legacy = tmp_path / "fake.plist"

    prompt_multilingual_mock = MagicMock(return_value=False)
    with (
        patch("archon_search.install.installer.get_default_config_path", return_value=config_path),
        patch("archon_search.install.installer._legacy_service_path", return_value=fake_legacy),
        patch("archon_search.install.installer._remove_legacy_service"),
        patch("archon_search.install.installer._prewarm_models"),
        patch("archon_search.install.installer._check_disk_space"),
        patch("archon_search.install.installer._prompt_multilingual", prompt_multilingual_mock),
        patch("archon_search.install.installer._prompt_optional_features", return_value=WizardFeatures()),
        patch("archon_search.install.installer._prompt_gpu_confirm", return_value=True),
        patch("builtins.input", return_value="y"),  # "Proceed?" prompt
        patch.object(BaseInstaller, "detect_gpu", return_value=GpuType.NONE),
        patch.object(BaseInstaller, "validate_providers", return_value=False),
        patch.object(RealInstaller, "configure_providers"),
        patch.object(RealInstaller, "write_service_file"),
        patch.object(RealInstaller, "load_service", return_value=0),
        patch.object(BaseInstaller, "_wait_for_service", return_value=True),
        patch.object(BaseInstaller, "_is_service_running", return_value=False),
    ):
        installer = create_installer(config_file=str(config_path))
        rc = installer.run(non_interactive=False, profile="minimal", multilingual=None, skip_preload=True)

    assert rc == 0
    prompt_multilingual_mock.assert_called_once_with(False, None)


def test_run_multilingual_flag_skips_prompt(tmp_path: Path) -> None:
    """multilingual=True is passed to _prompt_multilingual; the flag still takes precedence."""
    config_path = tmp_path / "archon-search.toml"
    fake_legacy = tmp_path / "fake.plist"

    prompt_multilingual_mock = MagicMock(return_value=True)
    with (
        patch("archon_search.install.installer.get_default_config_path", return_value=config_path),
        patch("archon_search.install.installer._legacy_service_path", return_value=fake_legacy),
        patch("archon_search.install.installer._remove_legacy_service"),
        patch("archon_search.install.installer._prewarm_models"),
        patch("archon_search.install.installer._check_disk_space"),
        # Otherwise a real `uv pip install` of [multilingual] re-resolves the test environment.
        patch("archon_search.install.installer._install_multilingual_extra"),
        patch("archon_search.install.installer._prompt_multilingual", prompt_multilingual_mock),
        patch("archon_search.install.installer._prompt_optional_features", return_value=WizardFeatures()),
        patch("archon_search.install.installer._prompt_gpu_confirm", return_value=True),
        patch("archon_search.install.installer._prompt_fasttext_license"),
        patch("archon_search.install.installer._download_fasttext_model"),
        patch.object(BaseInstaller, "detect_gpu", return_value=GpuType.NONE),
        patch.object(BaseInstaller, "validate_providers", return_value=False),
        patch.object(RealInstaller, "configure_providers"),
        patch.object(RealInstaller, "write_service_file"),
        patch.object(RealInstaller, "load_service", return_value=0),
        patch.object(BaseInstaller, "_wait_for_service", return_value=True),
        patch.object(BaseInstaller, "_is_service_running", return_value=False),
    ):
        installer = create_installer(config_file=str(config_path))
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
        patch("archon_search.install.installer.get_default_config_path", return_value=config_path),
        patch("archon_search.install.installer._legacy_service_path", return_value=fake_legacy),
        patch("archon_search.install.installer._remove_legacy_service"),
        patch("archon_search.install.installer._prewarm_models"),
        patch("archon_search.install.installer._check_disk_space"),
        patch("archon_search.install.installer._prompt_multilingual", return_value=False),
        patch("archon_search.install.installer._prompt_optional_features", prompt_features_mock),
        patch("archon_search.install.installer._prompt_gpu_confirm", return_value=True),
        patch.object(BaseInstaller, "detect_gpu", return_value=GpuType.NONE),
        patch.object(BaseInstaller, "validate_providers", return_value=False),
        patch.object(RealInstaller, "configure_providers"),
        patch.object(RealInstaller, "write_service_file"),
        patch.object(RealInstaller, "load_service", return_value=0),
        patch.object(BaseInstaller, "_wait_for_service", return_value=True),
        patch.object(BaseInstaller, "_is_service_running", return_value=False),
    ):
        installer = create_installer(config_file=str(config_path))
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
        patch("archon_search.install.installer.get_default_config_path", return_value=config_path),
        patch("archon_search.install.installer._legacy_service_path", return_value=fake_legacy),
        patch("archon_search.install.installer._remove_legacy_service"),
        patch("archon_search.install.installer._prewarm_models"),
        patch("archon_search.install.installer._check_disk_space"),
        patch("archon_search.install.installer._prompt_multilingual", return_value=False),
        patch("archon_search.install.installer._prompt_optional_features", return_value=features),
        patch("archon_search.install.installer._prompt_gpu_confirm", return_value=True),
        patch("archon_search.install.installer._install_code_extra", install_code_mock),
        patch.object(BaseInstaller, "detect_gpu", return_value=GpuType.NONE),
        patch.object(BaseInstaller, "validate_providers", return_value=False),
        patch.object(RealInstaller, "configure_providers"),
        patch.object(RealInstaller, "write_service_file"),
        patch.object(RealInstaller, "load_service", return_value=0),
        patch.object(BaseInstaller, "_wait_for_service", return_value=True),
        patch.object(BaseInstaller, "_is_service_running", return_value=False),
    ):
        installer = create_installer(config_file=str(config_path))
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
        patch("archon_search.install.installer.get_default_config_path", return_value=config_path),
        patch("archon_search.install.installer._legacy_service_path", return_value=fake_legacy),
        patch("archon_search.install.installer._remove_legacy_service"),
        patch("archon_search.install.installer._prewarm_models"),
        patch("archon_search.install.installer._check_disk_space"),
        patch("archon_search.install.installer._prompt_multilingual", return_value=False),
        patch("archon_search.install.installer._prompt_optional_features", return_value=features),
        patch("archon_search.install.installer._prompt_gpu_confirm", return_value=True),
        patch("archon_search.install.installer._install_code_extra", side_effect=InstallError("pip failed")),
        patch.object(BaseInstaller, "detect_gpu", return_value=GpuType.NONE),
        patch.object(BaseInstaller, "validate_providers", return_value=False),
        patch.object(RealInstaller, "configure_providers"),
        patch.object(RealInstaller, "write_service_file"),
        patch.object(RealInstaller, "load_service", return_value=0),
        patch.object(BaseInstaller, "_wait_for_service", return_value=True),
        patch.object(BaseInstaller, "_is_service_running", return_value=False),
    ):
        installer = create_installer(config_file=str(config_path))
        rc = installer.run(non_interactive=True, profile="minimal", skip_preload=True, install_code=True)

    assert rc == 0


def test_run_gpu_confirm_decline_writes_cpu(tmp_path: Path) -> None:
    """disable_gpu=True → providers written as [] in config."""
    config_path = tmp_path / "archon-search.toml"
    fake_legacy = tmp_path / "fake.plist"

    from archon_search.install import WizardFeatures
    features = WizardFeatures()

    with (
        patch("archon_search.install.installer.get_default_config_path", return_value=config_path),
        patch("archon_search.install.installer._legacy_service_path", return_value=fake_legacy),
        patch("archon_search.install.installer._remove_legacy_service"),
        patch("archon_search.install.installer._prewarm_models"),
        patch("archon_search.install.installer._check_disk_space"),
        patch("archon_search.install.installer._prompt_multilingual", return_value=False),
        patch("archon_search.install.installer._prompt_optional_features", return_value=features),
        patch("archon_search.install.installer._prompt_gpu_confirm", return_value=False),
        patch.object(BaseInstaller, "detect_gpu", return_value=GpuType.METAL),
        patch.object(BaseInstaller, "validate_providers", return_value=False),
        patch.object(RealInstaller, "configure_providers"),
        patch.object(RealInstaller, "write_service_file"),
        patch.object(RealInstaller, "load_service", return_value=0),
        patch.object(BaseInstaller, "_wait_for_service", return_value=True),
        patch.object(BaseInstaller, "_is_service_running", return_value=False),
    ):
        installer = create_installer(config_file=str(config_path))
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
        patch("archon_search.install.installer.get_default_config_path", return_value=config_path),
        patch("archon_search.install.installer._legacy_service_path", return_value=fake_legacy),
        patch("archon_search.install.installer._remove_legacy_service"),
        patch("archon_search.install.installer._prewarm_models"),
        patch("archon_search.install.installer._check_disk_space"),
        patch("archon_search.install.installer._prompt_multilingual", prompt_multilingual_mock),
        patch("archon_search.install.installer._prompt_optional_features", prompt_features_mock),
        patch("archon_search.install.installer._prompt_gpu_confirm", prompt_gpu_mock),
        patch.object(BaseInstaller, "detect_gpu", return_value=GpuType.NONE),
        patch.object(BaseInstaller, "validate_providers", return_value=False),
        patch.object(RealInstaller, "configure_providers"),
        patch.object(RealInstaller, "write_service_file"),
        patch.object(RealInstaller, "load_service", return_value=0),
        patch.object(BaseInstaller, "_wait_for_service", return_value=True),
        patch.object(BaseInstaller, "_is_service_running", return_value=False),
    ):
        installer = create_installer(config_file=str(config_path))
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
        patch("archon_search.install.installer.get_default_config_path", return_value=config_path),
        patch("archon_search.install.installer._legacy_service_path", return_value=fake_legacy),
        patch("archon_search.install.installer._remove_legacy_service"),
        patch("archon_search.install.installer._prewarm_models"),
        patch("archon_search.install.installer._check_disk_space"),
        patch("archon_search.install.installer._prompt_multilingual", return_value=False),
        patch("archon_search.install.installer._prompt_optional_features", return_value=features),
        patch("archon_search.install.installer._prompt_gpu_confirm", return_value=True),
        patch.object(BaseInstaller, "detect_gpu", return_value=GpuType.NONE),
        patch.object(BaseInstaller, "validate_providers", return_value=False),
        patch.object(RealInstaller, "configure_providers"),
        patch.object(RealInstaller, "write_service_file"),
        patch.object(RealInstaller, "load_service", return_value=0),
        patch.object(BaseInstaller, "_wait_for_service", return_value=True),
        patch.object(BaseInstaller, "_is_service_running", return_value=False),
    ):
        installer = create_installer(config_file=str(config_path))
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
        patch("archon_search.install.installer.get_default_config_path", return_value=config_path),
        patch("archon_search.install.installer._legacy_service_path", return_value=fake_legacy),
        patch("archon_search.install.installer._remove_legacy_service"),
        patch("archon_search.install.installer._prewarm_models"),
        patch("archon_search.install.installer._check_disk_space"),
        patch("archon_search.install.installer._prompt_multilingual", return_value=False),
        patch("archon_search.install.installer._prompt_optional_features", return_value=features),
        patch("archon_search.install.installer._prompt_gpu_confirm", return_value=True),
        patch.object(BaseInstaller, "detect_gpu", return_value=GpuType.NONE),
        patch.object(BaseInstaller, "validate_providers", return_value=False),
        patch.object(RealInstaller, "configure_providers"),
        patch.object(RealInstaller, "write_service_file"),
        patch.object(RealInstaller, "load_service", return_value=0),
        patch.object(BaseInstaller, "_wait_for_service", return_value=True),
        patch.object(BaseInstaller, "_is_service_running", return_value=False),
    ):
        installer = create_installer(config_file=str(config_path))
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
        patch("archon_search.install.installer.get_default_config_path", return_value=config_path),
        patch("archon_search.install.installer._legacy_service_path", return_value=fake_legacy),
        patch("archon_search.install.installer._remove_legacy_service"),
        patch("archon_search.install.installer._prewarm_models"),
        patch("archon_search.install.installer._check_disk_space"),
        patch("archon_search.install.installer._prompt_multilingual", return_value=False),
        patch("archon_search.install.installer._prompt_optional_features", return_value=features),
        patch("archon_search.install.installer._prompt_gpu_confirm", return_value=True),
        patch("archon_search.install.installer.get_search_service", return_value=MagicMock()),
        patch("archon_search.install.shutil.rmtree", MagicMock()),
        patch.object(BaseInstaller, "detect_gpu", return_value=GpuType.NONE),
        patch.object(BaseInstaller, "validate_providers", return_value=False),
        patch.object(RealInstaller, "configure_providers"),
        patch.object(RealInstaller, "write_service_file"),
        patch.object(RealInstaller, "load_service", return_value=0),
        patch.object(BaseInstaller, "_wait_for_service", return_value=True),
        patch.object(BaseInstaller, "_is_service_running", return_value=False),
    ):
        installer = create_installer(config_file=str(config_path))
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
        patch("archon_search.install.installer.get_default_config_path", return_value=config_path),
        patch("archon_search.install.installer._legacy_service_path", return_value=fake_legacy),
        patch("archon_search.install.installer._remove_legacy_service"),
        patch("archon_search.install.installer._prewarm_models"),
        patch("archon_search.install.installer._check_disk_space"),
        patch("archon_search.install.installer._prompt_multilingual", return_value=False),
        patch("archon_search.install.installer._prompt_optional_features", return_value=features),
        patch("archon_search.install.installer._prompt_gpu_confirm", return_value=False),  # user declines GPU
        patch("builtins.input", return_value="y"),  # "Proceed?" prompt
        patch.object(BaseInstaller, "detect_gpu", return_value=GpuType.METAL),
        patch.object(BaseInstaller, "validate_providers", return_value=False),
        patch.object(RealInstaller, "configure_providers"),
        patch.object(RealInstaller, "write_service_file"),
        patch.object(RealInstaller, "load_service", return_value=0),
        patch.object(BaseInstaller, "_wait_for_service", return_value=True),
        patch.object(BaseInstaller, "_is_service_running", return_value=False),
    ):
        installer = create_installer(config_file=str(config_path))
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
        "archon_search.install.installer.get_default_config_path": MagicMock(return_value=config_path),
        "archon_search.install.installer._legacy_service_path": MagicMock(return_value=fake_legacy),
        "archon_search.install.installer._remove_legacy_service": MagicMock(),
        "archon_search.install.installer._prewarm_models": MagicMock(),
        "archon_search.install.installer._check_disk_space": MagicMock(),
        "archon_search.install.installer._prompt_multilingual": MagicMock(return_value=False),
        "archon_search.install.installer._prompt_jina_license": MagicMock(
            side_effect=_log_side_effect("jina_license")
        ),
        "archon_search.install.installer._prompt_fasttext_license": MagicMock(
            side_effect=_log_side_effect("fasttext_license")
        ),
        "archon_search.install.installer._prompt_optional_features": MagicMock(
            side_effect=_log_side_effect("optional_features", features)
        ),
        "archon_search.install.installer._prompt_gpu_confirm": MagicMock(
            side_effect=_log_side_effect("gpu_confirm", True)
        ),
        "archon_search.install.installer._write_profile_config": MagicMock(
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

    with patch.multiple("archon_search.install.installer", **{
        k.replace("archon_search.install.installer.", ""): v for k, v in patches.items()
    }):
        with patch.multiple(RealInstaller, **method_patches):
            installer = create_installer(config_file=str(config_path))
            with patch("archon_search.install.installer._requires_jina_license", return_value=True):
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

    with patch.multiple("archon_search.install.installer", **{
        k.replace("archon_search.install.installer.", ""): v for k, v in patches.items()
    }):
        with patch.multiple(RealInstaller, **method_patches):
            installer = create_installer(config_file=str(config_path))
            with patch("archon_search.install.installer._requires_jina_license", return_value=True):
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

    with patch.multiple("archon_search.install.installer", **{
        k.replace("archon_search.install.installer.", ""): v for k, v in patches.items()
    }):
        with patch.multiple(RealInstaller, **method_patches):
            installer = create_installer(config_file=str(config_path))
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

    with patch.multiple("archon_search.install.installer", **{
        k.replace("archon_search.install.installer.", ""): v for k, v in patches.items()
    }):
        with patch.multiple(RealInstaller, **method_patches):
            installer = create_installer(config_file=str(config_path))
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


def test_graph_providers_are_settled_after_the_prewarm(tmp_path: Path) -> None:
    """FE-5 Step 14b: [graph].providers is settled from the pre-warm's own device,
    and only after pre-warm — stage 2 rides it, so the order is load-bearing."""
    from archon_search.install import WizardFeatures
    config_path, call_log, patches, method_patches = _make_ordered_installer(tmp_path)

    def _log(name, result=None):
        def side_effect(*args, **kwargs):
            call_log.append(name)
            return result
        return side_effect

    patches["archon_search.install.installer._prompt_optional_features"] = MagicMock(
        side_effect=_log("optional_features", WizardFeatures(install_graph_extra=True))
    )
    patches["archon_search.install.installer._install_graph_extra"] = MagicMock()
    method_patches["preload_models"] = MagicMock(side_effect=_log("preload_models", "cuda"))
    graph_mock = MagicMock(side_effect=_log("settle_graph_providers"))
    method_patches["_configure_graph_providers"] = graph_mock

    with patch.multiple("archon_search.install.installer", **{
        k.replace("archon_search.install.installer.", ""): v for k, v in patches.items()
    }):
        with patch.multiple(RealInstaller, **method_patches):
            rc = create_installer(config_file=str(config_path)).run(
                non_interactive=True, profile="minimal",
                graph_providers=["CUDAExecutionProvider"],
            )

    assert rc == 0
    assert call_log.index("preload_models") < call_log.index("settle_graph_providers"), call_log
    candidates, _availability, prewarm_device, non_interactive = graph_mock.call_args.args
    assert candidates == ["CUDAExecutionProvider"], "--graph-providers must reach the probe"
    assert prewarm_device == "cuda", "the pre-warm's resolved device must be passed"
    assert non_interactive is True
    # _availability is probe_device_availability(candidates) — host-dependent, so unasserted.


def test_graph_extra_and_graph_providers_reach_the_prewarm(tmp_path: Path) -> None:
    """With the graph extra installed, run() hands pre-warm the graph flag and the
    --graph-providers candidates, so the graph model loads where stage 2 probes it."""
    from archon_search.install import WizardFeatures
    config_path, _call_log, patches, method_patches = _make_ordered_installer(tmp_path)

    patches["archon_search.install.installer._prompt_optional_features"] = MagicMock(
        return_value=WizardFeatures(install_graph_extra=True)
    )
    patches["archon_search.install.installer._install_graph_extra"] = MagicMock()
    prewarm_mock = MagicMock(return_value="cuda")
    patches["archon_search.install.installer._prewarm_models"] = prewarm_mock
    method_patches["_configure_graph_providers"] = MagicMock()

    with patch.multiple("archon_search.install.installer", **{
        k.replace("archon_search.install.installer.", ""): v for k, v in patches.items()
    }):
        with patch.multiple(RealInstaller, **method_patches):
            rc = create_installer(config_file=str(config_path)).run(
                non_interactive=True, profile="minimal",
                graph_providers=["CUDAExecutionProvider"],
            )

    assert rc == 0
    prewarm_mock.assert_called_once()
    assert prewarm_mock.call_args.kwargs == {
        "install_graph_extra": True,
        "graph_providers": ["CUDAExecutionProvider"],
    }


def test_a_failure_settling_graph_providers_never_fails_the_install(tmp_path: Path) -> None:
    """Step 14b is an auxiliary write: it logs and continues, it does not abort run()."""
    from archon_search.install import WizardFeatures
    config_path, _call_log, patches, method_patches = _make_ordered_installer(tmp_path)

    patches["archon_search.install.installer._prompt_optional_features"] = MagicMock(
        return_value=WizardFeatures(install_graph_extra=True)
    )
    patches["archon_search.install.installer._install_graph_extra"] = MagicMock()
    method_patches["preload_models"] = MagicMock(return_value="cuda")
    method_patches["_configure_graph_providers"] = MagicMock(side_effect=OSError("disk gone"))

    with patch.multiple("archon_search.install.installer", **{
        k.replace("archon_search.install.installer.", ""): v for k, v in patches.items()
    }):
        with patch.multiple(RealInstaller, **method_patches):
            rc = create_installer(config_file=str(config_path)).run(
                non_interactive=True, profile="minimal",
            )

    assert rc == 0


def test_graph_providers_are_not_settled_when_prewarm_is_skipped(tmp_path: Path) -> None:
    """--skip-preload leaves no stage-2 evidence, so nothing is settled and an
    operator's hand-set [graph].providers survives a re-run."""
    from archon_search.install import WizardFeatures
    config_path, _call_log, patches, method_patches = _make_ordered_installer(tmp_path)

    patches["archon_search.install.installer._prompt_optional_features"] = MagicMock(
        return_value=WizardFeatures(install_graph_extra=True)
    )
    patches["archon_search.install.installer._install_graph_extra"] = MagicMock()
    graph_mock = MagicMock()
    method_patches["_configure_graph_providers"] = graph_mock

    with patch.multiple("archon_search.install.installer", **{
        k.replace("archon_search.install.installer.", ""): v for k, v in patches.items()
    }):
        with patch.multiple(RealInstaller, **method_patches):
            rc = create_installer(config_file=str(config_path)).run(
                non_interactive=True, profile="minimal", skip_preload=True,
            )

    assert rc == 0
    graph_mock.assert_not_called()


def test_graph_providers_are_not_settled_when_the_graph_extra_failed(tmp_path: Path) -> None:
    """A failed graph install reverts graph.enabled — providers must not be written
    into a config whose graph is now off."""
    from archon_search.install import WizardFeatures
    from archon_search.install.errors import InstallError
    config_path, call_log, patches, method_patches = _make_ordered_installer(tmp_path)

    patches["archon_search.install.installer._prompt_optional_features"] = MagicMock(
        return_value=WizardFeatures(install_graph_extra=True)
    )
    patches["archon_search.install.installer._install_graph_extra"] = MagicMock(
        side_effect=InstallError("boom")
    )
    patches["archon_search.install.installer._revert_graph_enabled_flag"] = MagicMock()
    method_patches["preload_models"] = MagicMock(return_value="cuda")
    graph_mock = MagicMock()
    method_patches["_configure_graph_providers"] = graph_mock

    with patch.multiple("archon_search.install.installer", **{
        k.replace("archon_search.install.installer.", ""): v for k, v in patches.items()
    }):
        with patch.multiple(RealInstaller, **method_patches):
            rc = create_installer(config_file=str(config_path)).run(
                non_interactive=True, profile="minimal",
            )

    assert rc == 0
    graph_mock.assert_not_called()


def test_reorder_non_interactive_still_succeeds(tmp_path: Path) -> None:
    """Full non-interactive run with all wizard flags returns exit code 0."""
    config_path = tmp_path / "archon-search.toml"
    fake_legacy = tmp_path / "fake.plist"

    from archon_search.install import WizardFeatures
    features = WizardFeatures()

    with (
        patch("archon_search.install.installer.get_default_config_path", return_value=config_path),
        patch("archon_search.install.installer._legacy_service_path", return_value=fake_legacy),
        patch("archon_search.install.installer._remove_legacy_service"),
        patch("archon_search.install.installer._prewarm_models"),
        patch("archon_search.install.installer._check_disk_space"),
        patch("archon_search.install.installer._prompt_multilingual", return_value=False),
        patch("archon_search.install.installer._prompt_optional_features", return_value=features),
        patch("archon_search.install.installer._prompt_gpu_confirm", return_value=True),
        patch.object(BaseInstaller, "detect_gpu", return_value=GpuType.NONE),
        patch.object(BaseInstaller, "validate_providers", return_value=False),
        patch.object(RealInstaller, "configure_providers"),
        patch.object(RealInstaller, "write_service_file"),
        patch.object(RealInstaller, "load_service", return_value=0),
        patch.object(BaseInstaller, "_wait_for_service", return_value=True),
        patch.object(BaseInstaller, "_is_service_running", return_value=False),
    ):
        installer = create_installer(config_file=str(config_path))
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
        patch("archon_search.install.installer.get_default_config_path", return_value=config_path),
        patch("archon_search.install.installer._legacy_service_path", return_value=tmp_path / "fake.plist"),
        patch("archon_search.install.installer._remove_legacy_service"),
        patch("archon_search.install.installer._prewarm_models"),
        patch("archon_search.install.installer._check_disk_space"),
        patch("archon_search.install.installer._write_profile_config", write_mock),
        patch("archon_search.install.installer._detect_config_hand_edits", return_value=True),
        patch("archon_search.install.installer._prompt_optional_features", return_value=WizardFeatures()),
        # input() used for: overwrite prompt ("y") and Proceed? ("y")
        patch("builtins.input", return_value="y"),
        patch.object(BaseInstaller, "detect_gpu", return_value=GpuType.NONE),
        patch.object(BaseInstaller, "validate_providers", return_value=False),
        patch.object(RealInstaller, "configure_providers"),
        patch.object(RealInstaller, "write_service_file"),
        patch.object(RealInstaller, "load_service", return_value=0),
        patch.object(BaseInstaller, "_wait_for_service", return_value=True),
        patch.object(BaseInstaller, "_is_service_running", return_value=False),
    ):
        installer = create_installer(config_file=str(config_path))
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
        patch("archon_search.install.installer.get_default_config_path", return_value=config_path),
        patch("archon_search.install.installer._legacy_service_path", return_value=tmp_path / "fake.plist"),
        patch("archon_search.install.installer._remove_legacy_service"),
        patch("archon_search.install.installer._prewarm_models"),
        patch("archon_search.install.installer._check_disk_space"),
        patch("archon_search.install.installer._write_profile_config", write_mock),
        patch("archon_search.install.installer._detect_config_hand_edits", return_value=True),
        patch("archon_search.install.installer._prompt_optional_features", return_value=WizardFeatures()),
        # "n" causes the overwrite prompt to abort
        patch("builtins.input", return_value="n"),
        patch.object(BaseInstaller, "detect_gpu", return_value=GpuType.NONE),
        patch.object(BaseInstaller, "validate_providers", return_value=False),
        patch.object(RealInstaller, "configure_providers"),
        patch.object(RealInstaller, "write_service_file"),
        patch.object(RealInstaller, "load_service", return_value=0),
        patch.object(BaseInstaller, "_wait_for_service", return_value=True),
    ):
        installer = create_installer(config_file=str(config_path))
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
        patch("archon_search.install.installer.get_default_config_path", return_value=config_path),
        patch("archon_search.install.installer._legacy_service_path", return_value=tmp_path / "fake.plist"),
        patch("archon_search.install.installer._remove_legacy_service"),
        patch("archon_search.install.installer._prewarm_models"),
        patch("archon_search.install.installer._check_disk_space"),
        patch("archon_search.install.installer._detect_config_hand_edits", return_value=True),
        patch("archon_search.install.installer._prompt_optional_features", return_value=WizardFeatures()),
        patch("builtins.input", return_value="n"),
        patch.object(BaseInstaller, "detect_gpu", return_value=GpuType.NONE),
        patch.object(BaseInstaller, "validate_providers", return_value=False),
        patch.object(RealInstaller, "configure_providers"),
        patch.object(RealInstaller, "write_service_file"),
        patch.object(RealInstaller, "load_service", return_value=0),
        patch.object(BaseInstaller, "_wait_for_service", return_value=True),
    ):
        installer = create_installer(config_file=str(config_path))
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
        patch("archon_search.install.installer.get_default_config_path", return_value=config_path),
        patch("archon_search.install.installer._legacy_service_path", return_value=tmp_path / "fake.plist"),
        patch("archon_search.install.installer._remove_legacy_service"),
        patch("archon_search.install.installer._prewarm_models"),
        patch("archon_search.install.installer._check_disk_space"),
        # _detect_config_hand_edits returns False = no edits
        patch("archon_search.install.installer._detect_config_hand_edits", return_value=False),
        patch("builtins.input", input_mock),
        patch.object(BaseInstaller, "detect_gpu", return_value=GpuType.NONE),
        patch.object(BaseInstaller, "validate_providers", return_value=False),
        patch.object(RealInstaller, "configure_providers"),
        patch.object(RealInstaller, "write_service_file"),
        patch.object(RealInstaller, "load_service", return_value=0),
        patch.object(BaseInstaller, "_wait_for_service", return_value=True),
        patch.object(BaseInstaller, "_is_service_running", return_value=False),
    ):
        installer = create_installer(config_file=str(config_path))
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
        patch("archon_search.install.installer.get_default_config_path", return_value=config_path),
        patch("archon_search.install.installer._legacy_service_path", return_value=tmp_path / "fake.plist"),
        patch("archon_search.install.installer._remove_legacy_service"),
        patch("archon_search.install.installer._prewarm_models"),
        patch("archon_search.install.installer._check_disk_space"),
        patch("archon_search.install.installer._write_profile_config", write_mock),
        patch("archon_search.install.installer._detect_config_hand_edits", return_value=True),
        patch("builtins.input", input_mock),
        patch.object(BaseInstaller, "detect_gpu", return_value=GpuType.NONE),
        patch.object(BaseInstaller, "validate_providers", return_value=False),
        patch.object(RealInstaller, "configure_providers"),
        patch.object(RealInstaller, "write_service_file"),
        patch.object(RealInstaller, "load_service", return_value=0),
        patch.object(BaseInstaller, "_wait_for_service", return_value=True),
        patch.object(BaseInstaller, "_is_service_running", return_value=False),
    ):
        installer = create_installer(config_file=str(config_path))
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
        patch("archon_search.install.installer.get_default_config_path", return_value=config_path),
        patch("archon_search.install.installer._legacy_service_path", return_value=tmp_path / "fake.plist"),
        patch("archon_search.install.installer._remove_legacy_service"),
        patch("archon_search.install.installer._prewarm_models"),
        patch("archon_search.install.installer._check_disk_space"),
        patch("archon_search.install.installer._detect_config_hand_edits", return_value=True),
        patch.object(BaseInstaller, "detect_gpu", return_value=GpuType.NONE),
        patch.object(BaseInstaller, "validate_providers", return_value=False),
        patch.object(RealInstaller, "configure_providers"),
        patch.object(RealInstaller, "write_service_file"),
        patch.object(RealInstaller, "load_service", return_value=0),
        patch.object(BaseInstaller, "_wait_for_service", return_value=True),
        patch.object(BaseInstaller, "_is_service_running", return_value=False),
    ):
        installer = create_installer(config_file=str(config_path))
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
        patch("archon_search.install.installer.get_default_config_path", return_value=config_path),
        patch("archon_search.install.installer._legacy_service_path", return_value=tmp_path / "fake.plist"),
        patch("archon_search.install.installer._remove_legacy_service"),
        patch("archon_search.install.installer._prewarm_models"),
        patch("archon_search.install.installer._check_disk_space"),
        patch("archon_search.install.installer._detect_config_hand_edits", return_value=True),
        patch("builtins.input", input_mock),
        patch.object(BaseInstaller, "detect_gpu", return_value=GpuType.NONE),
        patch.object(BaseInstaller, "validate_providers", return_value=False),
        patch.object(DryRunInstaller, "configure_providers"),
        patch.object(DryRunInstaller, "write_service_file"),
        patch.object(DryRunInstaller, "load_service", return_value=0),
        patch.object(BaseInstaller, "_wait_for_service", return_value=True),
        patch.object(BaseInstaller, "_is_service_running", return_value=False),
    ):
        installer = create_installer(config_file=str(config_path), dry_run=True)
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
        patch("archon_search.install.installer.get_default_config_path", return_value=config_path),
        patch("archon_search.install.installer._legacy_service_path", return_value=tmp_path / "fake.plist"),
        patch("archon_search.install.installer._remove_legacy_service"),
        patch("archon_search.install.installer._prewarm_models"),
        patch("archon_search.install.installer._check_disk_space"),
        patch("archon_search.install.installer._detect_config_hand_edits", return_value=True),
        patch.object(BaseInstaller, "detect_gpu", return_value=GpuType.NONE),
        patch.object(BaseInstaller, "validate_providers", return_value=False),
        patch.object(RealInstaller, "configure_providers"),
        patch.object(RealInstaller, "write_service_file"),
        patch.object(RealInstaller, "load_service", return_value=0),
        patch.object(BaseInstaller, "_wait_for_service", return_value=True),
        patch.object(BaseInstaller, "_is_service_running", return_value=False),
    ):
        installer = create_installer(config_file=str(config_path))
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
        patch("archon_search.install.installer.get_default_config_path", return_value=config_path),
        patch("archon_search.install.installer._legacy_service_path", return_value=tmp_path / "fake.plist"),
        patch("archon_search.install.installer._remove_legacy_service"),
        patch("archon_search.install.installer._prewarm_models"),
        patch("archon_search.install.installer._check_disk_space"),
        patch("archon_search.install.installer._write_profile_config", write_mock),
        patch("archon_search.install.installer._detect_config_hand_edits", return_value=True),
        patch("archon_search.install.installer._prompt_optional_features", return_value=WizardFeatures()),
        # EOFError on the overwrite prompt (piped/non-tty stdin)
        patch("builtins.input", side_effect=EOFError),
        patch.object(BaseInstaller, "detect_gpu", return_value=GpuType.NONE),
        patch.object(BaseInstaller, "validate_providers", return_value=False),
        patch.object(RealInstaller, "configure_providers"),
        patch.object(RealInstaller, "write_service_file"),
        patch.object(RealInstaller, "load_service", return_value=0),
        patch.object(BaseInstaller, "_wait_for_service", return_value=True),
    ):
        installer = create_installer(config_file=str(config_path))
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
        patch("archon_search.install.installer.get_default_config_path", return_value=config_path),
        patch("archon_search.install.installer._legacy_service_path", return_value=tmp_path / "fake.plist"),
        patch("archon_search.install.installer._remove_legacy_service"),
        patch("archon_search.install.installer._prewarm_models"),
        patch("archon_search.install.installer._check_disk_space"),
        patch("archon_search.install.installer._detect_config_hand_edits", return_value=True),
        patch.object(BaseInstaller, "detect_gpu", return_value=GpuType.NONE),
        patch.object(BaseInstaller, "validate_providers", return_value=False),
        patch.object(RealInstaller, "configure_providers"),
        patch.object(RealInstaller, "write_service_file"),
        patch.object(RealInstaller, "load_service", return_value=0),
        patch.object(BaseInstaller, "_wait_for_service", return_value=True),
        patch.object(BaseInstaller, "_is_service_running", return_value=False),
    ):
        installer = create_installer(config_file=str(config_path))
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
        patch("archon_search.install.installer.get_default_config_path", return_value=config_path),
        patch("archon_search.install.installer._legacy_service_path", return_value=fake_legacy),
        patch("archon_search.install.installer._remove_legacy_service"),
        patch("archon_search.install.installer._prewarm_models"),
        patch("archon_search.install.installer._check_disk_space"),
        patch("archon_search.key_manager.load_or_generate_key", return_value=(key, source)),
        patch("archon_search.install.installer.load_or_generate_key", return_value=(key, source)),
        patch.object(BaseInstaller, "detect_gpu", return_value=GpuType.NONE),
        patch.object(BaseInstaller, "validate_providers", return_value=False),
        patch.object(RealInstaller, "configure_providers"),
        patch.object(RealInstaller, "write_service_file"),
        patch.object(RealInstaller, "load_service", return_value=0),
        patch.object(BaseInstaller, "_wait_for_service", return_value=True),
        patch.object(BaseInstaller, "_is_service_running", return_value=False),
    ):
        installer = create_installer(config_file=str(config_path), dry_run=dry_run)
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


# ---------------------------------------------------------------------------
# Step 9 GPU provider configuration → the config Step 14 pre-warms (S671)
# ---------------------------------------------------------------------------

_COREML = "CoreMLExecutionProvider"
_CUDA = "CUDAExecutionProvider"


@contextmanager
def _gpu_patches(
    config_path: Path,
    fake_legacy: Path,
    validate_mock: MagicMock,
    prewarm_mock: MagicMock | None = None,
    gpu: GpuType = GpuType.METAL,
):
    """Patch set for a GPU wizard run; Step 9's provider writes reach the real config."""
    from contextlib import ExitStack

    patches = (
        patch("archon_search.install.installer.get_default_config_path", return_value=config_path),
        patch("archon_search.install.installer._legacy_service_path", return_value=fake_legacy),
        patch("archon_search.install.installer._remove_legacy_service"),
        patch("archon_search.install.installer._install_cuda_torch"),
        patch("archon_search.install.installer._prewarm_models", prewarm_mock or MagicMock()),
        patch("archon_search.install.installer._check_disk_space"),
        patch.object(BaseInstaller, "detect_gpu", return_value=gpu),
        patch.object(BaseInstaller, "validate_providers", validate_mock),
        patch.object(RealInstaller, "write_service_file"),
        patch.object(RealInstaller, "load_service", return_value=0),
        patch.object(BaseInstaller, "_wait_for_service", return_value=True),
        patch.object(BaseInstaller, "_is_service_running", return_value=False),
    )
    with ExitStack() as stack:
        for p in patches:
            stack.enter_context(p)
        yield


def test_wizard_metal_checks_coreml_once_before_prewarm(tmp_path: Path) -> None:
    """Metal runs the Step 9 CoreML check exactly once, before pre-warm; pre-warm itself
    then loads both models under the providers it wrote, so no second validation follows."""
    config_path = tmp_path / "archon-search.toml"
    order: list[str] = []
    validate_mock = MagicMock(side_effect=lambda *_args: order.append("validate") or True)
    prewarm_mock = MagicMock(side_effect=lambda *_args, **_kwargs: order.append("prewarm"))

    with _gpu_patches(config_path, tmp_path / "fake.plist", validate_mock, prewarm_mock):
        installer = create_installer(config_file=str(config_path))
        rc = installer.run(non_interactive=True, profile="balanced", skip_preload=False)

    assert rc == 0
    validate_mock.assert_called_once_with([_COREML])
    assert order == ["validate", "prewarm"]


@pytest.mark.parametrize(
    ("gpu", "coreml_combined_ok", "use_default_path", "expected_providers", "expected_reranker"),
    [
        (GpuType.METAL, True, False, [_COREML], [_COREML]),
        (GpuType.METAL, True, True, [_COREML], [_COREML]),
        (GpuType.METAL, False, False, [_COREML], None),
        (GpuType.CUDA, False, False, [_CUDA], [_CUDA]),
    ],
    ids=["metal-combined", "metal-combined-default-path", "metal-split-coreml", "cuda"],
)
def test_wizard_prewarms_the_providers_step_9_wrote(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    gpu: GpuType,
    coreml_combined_ok: bool,
    use_default_path: bool,
    expected_providers: list[str],
    expected_reranker: list[str] | None,
) -> None:
    """Step 9 rewrites [database].providers (and, on the split CoreML path,
    reranker_providers = []) after Step 8b loaded the config; pre-warm must load the
    models under what the service will read, not the stale config."""
    from archon_search.config import resolve_reranker_providers
    from archon_search.install import _prewarm_timeout
    from archon_search.profiles import get_profile

    config_path = tmp_path / "archon-search.toml"
    if use_default_path:
        monkeypatch.setenv("ARCHON_SEARCH_CONFIG", str(config_path))
    prewarm_mock = MagicMock(return_value=None)

    with (
        _gpu_patches(
            config_path, tmp_path / "fake.plist", MagicMock(return_value=coreml_combined_ok),
            prewarm_mock, gpu=gpu,
        ),
        patch.object(BaseInstaller, "validate_embedder_only", return_value=True),
    ):
        installer = create_installer(config_file=None if use_default_path else str(config_path))
        rc = installer.run(non_interactive=True, profile="minimal", skip_preload=False)

    assert rc == 0
    prewarm_mock.assert_called_once()
    cfg, timeout = prewarm_mock.call_args.args
    assert cfg.providers == expected_providers
    assert resolve_reranker_providers(cfg) == expected_reranker
    assert timeout == _prewarm_timeout(get_profile("minimal", False))
    assert installer.cfg is cfg


def test_wizard_disable_gpu_prewarms_cpu_over_a_previous_coreml_config(tmp_path: Path) -> None:
    """--disable-gpu on a re-run rewrites [database].providers to [] at Step 9 after
    Step 8b loaded the previous CoreML config; pre-warm must load on CPU, as the
    service will."""
    import tomlkit

    from archon_search.install import _profile_toml

    config_path = tmp_path / "archon-search.toml"
    doc = tomlkit.parse(_profile_toml("minimal", False))
    doc["database"]["providers"] = [_COREML]
    config_path.write_text(tomlkit.dumps(doc))
    validate_mock = MagicMock(return_value=True)
    prewarm_mock = MagicMock(return_value=None)

    with _gpu_patches(config_path, tmp_path / "fake.plist", validate_mock, prewarm_mock):
        installer = create_installer(config_file=str(config_path))
        rc = installer.run(
            non_interactive=True, profile="minimal", skip_preload=False, disable_gpu=True
        )

    assert rc == 0
    validate_mock.assert_not_called()
    prewarm_mock.assert_called_once()
    assert prewarm_mock.call_args.args[0].providers == []


def test_wizard_coreml_fallback_writes_cpu_providers(tmp_path: Path) -> None:
    """Both CoreML checks fail → the wizard falls back to CPU by writing providers = [],
    overriding the CoreML a previous run wrote (the re-run preserves [database].providers)."""
    import tomlkit

    from archon_search.install import _profile_toml

    config_path = tmp_path / "archon-search.toml"
    doc = tomlkit.parse(_profile_toml("minimal", False))
    doc["database"]["providers"] = [_COREML]
    config_path.write_text(tomlkit.dumps(doc))

    with (
        _gpu_patches(config_path, tmp_path / "fake.plist", MagicMock(return_value=False)),
        patch.object(BaseInstaller, "validate_embedder_only", return_value=False),
    ):
        installer = create_installer(config_file=str(config_path))
        rc = installer.run(non_interactive=True, profile="minimal", skip_preload=False)

    assert rc == 0
    assert tomlkit.parse(config_path.read_text())["database"]["providers"] == []


@pytest.mark.parametrize("gpu", [GpuType.NONE, GpuType.CUDA], ids=["cpu", "cuda"])
def test_wizard_runs_no_provider_check_without_metal(tmp_path: Path, gpu: GpuType) -> None:
    """Only the Metal path validates providers at Step 9: a CPU install has nothing to
    check, and CUDA install-time validation is out of scope (D6)."""
    config_path = tmp_path / "archon-search.toml"
    validate_mock = MagicMock(return_value=True)

    with (
        patch("archon_search.install.installer.get_default_config_path", return_value=config_path),
        patch("archon_search.install.installer._legacy_service_path", return_value=tmp_path / "fake.plist"),
        patch("archon_search.install.installer._remove_legacy_service"),
        patch("archon_search.install.installer._install_cuda_torch"),
        patch("archon_search.install.installer._prewarm_models"),
        patch("archon_search.install.installer._check_disk_space"),
        patch.object(BaseInstaller, "detect_gpu", return_value=gpu),
        patch.object(BaseInstaller, "validate_providers", validate_mock),
        patch.object(RealInstaller, "configure_providers"),
        patch.object(RealInstaller, "write_service_file"),
        patch.object(RealInstaller, "load_service", return_value=0),
        patch.object(BaseInstaller, "_wait_for_service", return_value=True),
        patch.object(BaseInstaller, "_is_service_running", return_value=False),
    ):
        installer = create_installer(config_file=str(config_path))
        rc = installer.run(non_interactive=True, profile="balanced", skip_preload=False)

    assert rc == 0
    validate_mock.assert_not_called()


# ---------------------------------------------------------------------------
# Multilingual + --skip-preload: the required lid.176.ftz model is still
# downloaded (2026-07-16), while the heavy embedder/reranker preload is skipped.
# ---------------------------------------------------------------------------


def _multilingual_run_patches(config_path: Path, fake_legacy: Path, **overrides: Any):
    """Full patch set for a multilingual run() that never touches real FS/pip.

    Every _install_* / download helper is mocked, and the wizard answers are a
    real all-default WizardFeatures, so no feature flag spawns a real subprocess.
    """
    features = WizardFeatures()
    base = {
        "get_default_config_path": MagicMock(return_value=config_path),
        "_legacy_service_path": MagicMock(return_value=fake_legacy),
        "_remove_legacy_service": MagicMock(),
        "_prewarm_models": MagicMock(),
        "_check_disk_space": MagicMock(),
        "_prompt_multilingual": MagicMock(return_value=True),
        "_prompt_optional_features": MagicMock(return_value=features),
        "_prompt_gpu_confirm": MagicMock(return_value=True),
        "_install_multilingual_extra": MagicMock(),
    }
    base.update(overrides)
    return base


_MULTILINGUAL_METHOD_PATCHES = {
    "detect_gpu": MagicMock(return_value=GpuType.NONE),
    "validate_providers": MagicMock(return_value=False),
    "configure_providers": MagicMock(),
    "write_service_file": MagicMock(),
    "load_service": MagicMock(return_value=0),
    "_wait_for_service": MagicMock(return_value=True),
    "_is_service_running": MagicMock(return_value=False),
}


def test_multilingual_skip_preload_downloads_model_but_not_heavy_weights(tmp_path: Path) -> None:
    """--multilingual --skip-preload (non-interactive, license accepted):

    the required lid.176.ftz model IS downloaded, but the heavy embedder/reranker
    pre-warm is still skipped. Regression for 2026-07-16.
    """
    config_path = tmp_path / "archon-search.toml"
    fake_legacy = tmp_path / "fake.plist"

    download_mock = MagicMock()
    prewarm_mock = MagicMock()
    patches = _multilingual_run_patches(
        config_path, fake_legacy,
        _download_fasttext_model=download_mock,
        _prewarm_models=prewarm_mock,
        _prompt_fasttext_license=MagicMock(),
    )

    with patch.multiple("archon_search.install.installer", **patches):
        with patch.multiple(RealInstaller, **_MULTILINGUAL_METHOD_PATCHES):
            installer = create_installer(config_file=str(config_path))
            rc = installer.run(
                non_interactive=True,
                profile="minimal",
                multilingual=True,
                skip_preload=True,
                accept_fasttext_license=True,
            )

    assert rc == 0
    download_mock.assert_called_once()   # tiny required model downloaded
    prewarm_mock.assert_not_called()     # heavy weights still deferred


def test_multilingual_skip_preload_without_license_flag_stops(tmp_path: Path) -> None:
    """Non-interactive --multilingual --skip-preload WITHOUT --accept-fasttext-license

    still stops with the license error (rc==1) and never reaches the download.
    """
    config_path = tmp_path / "archon-search.toml"
    fake_legacy = tmp_path / "fake.plist"

    download_mock = MagicMock()
    # Real _prompt_fasttext_license → non-interactive + no flag raises SystemExit(1).
    patches = _multilingual_run_patches(
        config_path, fake_legacy,
        _download_fasttext_model=download_mock,
    )

    with patch.multiple("archon_search.install.installer", **patches):
        with patch.multiple(RealInstaller, **_MULTILINGUAL_METHOD_PATCHES):
            installer = create_installer(config_file=str(config_path))
            rc = installer.run(
                non_interactive=True,
                profile="minimal",
                multilingual=True,
                skip_preload=True,
                accept_fasttext_license=False,
            )

    assert rc == 1
    download_mock.assert_not_called()


def test_multilingual_download_failure_degrades_to_english(tmp_path: Path) -> None:
    """When the lid.176.ftz download fails, the install continues English-only

    (rc==0) and the written config has multilingual=false, so the server boots.
    """
    from archon_search.install import InstallError

    config_path = tmp_path / "archon-search.toml"
    fake_legacy = tmp_path / "fake.plist"

    install_extra_mock = MagicMock()
    patches = _multilingual_run_patches(
        config_path, fake_legacy,
        _download_fasttext_model=MagicMock(side_effect=InstallError("network down")),
        _prompt_fasttext_license=MagicMock(),
        _install_multilingual_extra=install_extra_mock,
    )

    with patch.multiple("archon_search.install.installer", **patches):
        with patch.multiple(RealInstaller, **_MULTILINGUAL_METHOD_PATCHES):
            installer = create_installer(config_file=str(config_path))
            rc = installer.run(
                non_interactive=True,
                profile="minimal",
                multilingual=True,
                skip_preload=True,
                accept_fasttext_license=True,
            )

    assert rc == 0
    assert config_path.exists()
    assert "multilingual = false" in config_path.read_text()
    # Degraded to English-only → the [multilingual] extra is not installed.
    install_extra_mock.assert_not_called()


def test_no_rendered_failure_contains_exception_text(tmp_path: Path, capsys, caplog) -> None:
    """The wizard renders the sanitized category, never the caught exception (FE-4).

    The insufficient-disk message embeds the models directory path, so the call
    site's old `str(exc)` interpolation was wire-facing. Sanitizing it must not
    also discard the detail, so it is logged instead of printed, and the install
    still degrades to English-only (S11).
    """
    from archon_search.install import InstallError, _render_provision_failure
    from archon_search.install.provisioning import ProvisionFailureKind

    leak = "SENTINEL /private/var/models/lid.176.ftz internals"
    config_path = tmp_path / "archon-search.toml"
    fake_legacy = tmp_path / "fake.plist"

    patches = _multilingual_run_patches(
        config_path, fake_legacy,
        _download_fasttext_model=MagicMock(
            side_effect=InstallError(leak, kind=ProvisionFailureKind.digest_mismatch)
        ),
        _prompt_fasttext_license=MagicMock(),
    )

    with caplog.at_level(logging.WARNING, logger="archon_search.install.installer"):
        with patch.multiple("archon_search.install.installer", **patches):
            with patch.multiple(RealInstaller, **_MULTILINGUAL_METHOD_PATCHES):
                installer = create_installer(config_file=str(config_path))
                rc = installer.run(
                    non_interactive=True,
                    profile="minimal",
                    multilingual=True,
                    skip_preload=True,
                    accept_fasttext_license=True,
                )

    assert rc == 0
    captured = capsys.readouterr()
    assert "SENTINEL" not in captured.err + captured.out
    assert _render_provision_failure(ProvisionFailureKind.digest_mismatch) in captured.err

    # Sanitized on the wire, but not thrown away — the operator can still recover it.
    assert leak in caplog.text

    # S11: the install still degrades to English-only rather than aborting.
    assert "multilingual = false" in config_path.read_text()


def test_bare_oserror_from_provisioning_degrades_instead_of_crashing(tmp_path: Path, capsys) -> None:
    """A read-only models dir must degrade to English-only, not crash the wizard.

    The mkdir/chmod/disk_usage/unlink calls around the download raise OSError bare,
    outside any InstallError wrapper, so catching InstallError alone let them escape
    run() as a traceback — the opposite of S11.
    """
    from archon_search.install import _render_provision_failure

    config_path = tmp_path / "archon-search.toml"
    fake_legacy = tmp_path / "fake.plist"

    patches = _multilingual_run_patches(
        config_path, fake_legacy,
        _download_fasttext_model=MagicMock(
            side_effect=PermissionError(13, "Permission denied", str(tmp_path / "models"))
        ),
        _prompt_fasttext_license=MagicMock(),
    )

    with patch.multiple("archon_search.install.installer", **patches):
        with patch.multiple(RealInstaller, **_MULTILINGUAL_METHOD_PATCHES):
            installer = create_installer(config_file=str(config_path))
            rc = installer.run(
                non_interactive=True,
                profile="minimal",
                multilingual=True,
                skip_preload=True,
                accept_fasttext_license=True,
            )

    assert rc == 0
    captured = capsys.readouterr()
    # OSError carries no `kind`, so it renders the filesystem copy, not a KeyError.
    assert _render_provision_failure(None) in captured.err
    assert "Permission denied" not in captured.err + captured.out
    assert "multilingual = false" in config_path.read_text()


# ---------------------------------------------------------------------------
# S206: write_service_file threads the chosen --config path into register()
# ---------------------------------------------------------------------------


def test_write_service_file_passes_config_file_to_register(tmp_path: Path) -> None:
    """RealInstaller.write_service_file() must register the service with the
    installer's config_file so the launchd/systemd unit's ARCHON_SEARCH_CONFIG
    points at the config the wizard just wrote, not the hardcoded default (S206).
    """
    from archon_search.install import _profile_toml

    config_path = tmp_path / "custom" / "archon-search.toml"
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(_profile_toml("minimal", False))

    fake_svc = MagicMock()
    with patch("archon_search.install.installer.get_search_service", return_value=fake_svc):
        installer = create_installer(config_file=str(config_path))
        installer.write_service_file()

    fake_svc.register.assert_called_once_with(dry_run=False, config_path=str(config_path))
