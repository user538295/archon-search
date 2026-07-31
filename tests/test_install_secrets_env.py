"""Tests for BE-12: wizard creates .secrets.env and macOS wrapper when HyDE/RAG Fusion enabled."""
from __future__ import annotations

import stat
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from archon_search.install import (
    BaseInstaller,
    DryRunInstaller,
    RealInstaller,
    _create_secrets_env,
    create_installer,
)
from archon_search.platform.types import GpuType

pytestmark = pytest.mark.xdist_group("install")


@pytest.fixture(autouse=True)
def _mock_query_expansion_install():
    """Mock the provider-package install so enable_hyde/enable_rag_fusion runs in
    this file never shell out to a real `pip install archon-search[hyde]`."""
    with patch("archon_search.install.installer._install_query_expansion_extras", return_value=[]):
        yield


# ---------------------------------------------------------------------------
# Unit tests for _create_secrets_env
# ---------------------------------------------------------------------------


def test_create_secrets_env_creates_file_with_correct_mode(tmp_path: Path) -> None:
    """_create_secrets_env creates .secrets.env with mode 0o600; returns True."""
    secrets_path = tmp_path / ".secrets.env"
    result = _create_secrets_env(secrets_path, dry_run=False)
    assert result is True
    assert secrets_path.exists()
    mode = stat.S_IMODE(secrets_path.stat().st_mode)
    assert mode == 0o600


def test_wizard_creates_secrets_env_content_is_empty(tmp_path: Path) -> None:
    """_create_secrets_env creates an empty .secrets.env file."""
    secrets_path = tmp_path / ".secrets.env"
    _create_secrets_env(secrets_path, dry_run=False)
    assert secrets_path.read_text() == ""


def test_create_secrets_env_is_idempotent(tmp_path: Path) -> None:
    """_create_secrets_env is a no-op when .secrets.env already exists; returns False."""
    secrets_path = tmp_path / ".secrets.env"
    secrets_path.write_text("EXISTING_CONTENT=1")
    secrets_path.chmod(0o600)
    result = _create_secrets_env(secrets_path, dry_run=False)
    assert result is False
    assert secrets_path.read_text() == "EXISTING_CONTENT=1"


def test_create_secrets_env_dry_run_skips_creation(tmp_path: Path) -> None:
    """_create_secrets_env does not create the file in dry-run mode; returns False."""
    secrets_path = tmp_path / ".secrets.env"
    result = _create_secrets_env(secrets_path, dry_run=True)
    assert result is False
    assert not secrets_path.exists()


# ---------------------------------------------------------------------------
# Integration test: wizard end-to-end creates .secrets.env and wrapper on macOS
# ---------------------------------------------------------------------------


def test_wizard_creates_secrets_env_and_wrapper_on_macos_positive(tmp_path: Path) -> None:
    """End-to-end: enable_hyde=True on macOS creates both .secrets.env (BE-12) and run-server.sh (BE-11).
    The wrapper assertion is intentional — BE-11 creates it unconditionally on service register();
    this test verifies both artifacts co-exist when AI expansion is enabled."""
    config_path = tmp_path / "archon-search.toml"
    fake_legacy = tmp_path / "fake.plist"
    data_dir = tmp_path / ".archon-search"
    data_dir.mkdir(parents=True, exist_ok=True)

    installer = create_installer(config_file=str(config_path), dry_run=False)

    # Mock macOS platform — use LaunchdSearchService writing to tmp_path
    from archon_search.platform.macos import LaunchdSearchService

    plist_path = tmp_path / "Library" / "LaunchAgents" / "com.archon.search.plist"

    with (
        patch("archon_search.install.installer.get_default_config_path", return_value=config_path),
        patch("archon_search.install.installer._legacy_service_path", return_value=fake_legacy),
        patch("archon_search.install.installer._remove_legacy_service"),
        patch("archon_search.install.installer._prewarm_models"),
        patch("archon_search.install.installer._check_disk_space"),
        patch("archon_search.install.installer.get_data_dir", return_value=data_dir),
        patch("archon_search.paths.get_data_dir", return_value=data_dir),
        patch.object(BaseInstaller, "detect_gpu", return_value=GpuType.NONE),
        patch.object(BaseInstaller, "validate_providers", return_value=False),
        patch.object(RealInstaller, "configure_providers"),
        patch.object(BaseInstaller, "_wait_for_service", return_value=True),
        patch.object(BaseInstaller, "_is_service_running", return_value=False),
        patch("archon_search.install.installer.get_search_service") as mock_get_svc,
        patch("pathlib.Path.home", return_value=tmp_path),
    ):
        # Use a real LaunchdSearchService but with home redirected to tmp_path
        svc = LaunchdSearchService()
        mock_get_svc.return_value = svc

        with (
            patch.object(svc, "start", return_value=0),
            patch.object(svc, "pre_activate_cleanup"),
        ):
            rc = installer.run(
                non_interactive=True,
                profile="minimal",
                skip_preload=True,
                enable_hyde=True,
            )

    assert rc == 0, f"Expected exit code 0, got {rc}"

    secrets_path = tmp_path / ".archon-search" / ".secrets.env"
    assert secrets_path.exists(), ".secrets.env must be created when HyDE is enabled"
    mode = stat.S_IMODE(secrets_path.stat().st_mode)
    assert mode == 0o600, f".secrets.env must have mode 0o600, got 0o{mode:o}"

    wrapper_path = tmp_path / ".archon-search" / "run-server.sh"
    assert wrapper_path.exists(), "run-server.sh must be created when HyDE is enabled"
    wrapper_mode = stat.S_IMODE(wrapper_path.stat().st_mode)
    assert wrapper_mode == 0o755, f"run-server.sh must have mode 0o755, got 0o{wrapper_mode:o}"


def test_wizard_creates_secrets_env_when_rag_fusion_only(tmp_path: Path) -> None:
    """Wizard with enable_rag_fusion=True alone (enable_hyde=False) creates .secrets.env."""
    config_path = tmp_path / "archon-search.toml"
    fake_legacy = tmp_path / "fake.plist"
    data_dir = tmp_path / ".archon-search"
    data_dir.mkdir(parents=True, exist_ok=True)

    installer = create_installer(config_file=str(config_path), dry_run=False)

    with (
        patch("archon_search.install.installer.get_default_config_path", return_value=config_path),
        patch("archon_search.install.installer._legacy_service_path", return_value=fake_legacy),
        patch("archon_search.install.installer._remove_legacy_service"),
        patch("archon_search.install.installer._prewarm_models"),
        patch("archon_search.install.installer._check_disk_space"),
        patch("archon_search.install.installer.get_data_dir", return_value=data_dir),
        patch("archon_search.paths.get_data_dir", return_value=data_dir),
        patch.object(BaseInstaller, "detect_gpu", return_value=GpuType.NONE),
        patch.object(BaseInstaller, "validate_providers", return_value=False),
        patch.object(RealInstaller, "configure_providers"),
        patch.object(RealInstaller, "write_service_file"),
        patch.object(RealInstaller, "load_service", return_value=0),
        patch.object(BaseInstaller, "_wait_for_service", return_value=True),
        patch.object(BaseInstaller, "_is_service_running", return_value=False),
        patch("pathlib.Path.home", return_value=tmp_path),
    ):
        rc = installer.run(
            non_interactive=True,
            profile="minimal",
            skip_preload=True,
            enable_hyde=False,
            enable_rag_fusion=True,
        )

    assert rc == 0
    secrets_path = data_dir / ".secrets.env"
    assert secrets_path.exists(), ".secrets.env must be created when RAG Fusion is enabled"
    mode = stat.S_IMODE(secrets_path.stat().st_mode)
    assert mode == 0o600


def test_wizard_dry_run_secrets_env_not_created_via_installer(tmp_path: Path) -> None:
    """Wizard with dry_run=True and enable_hyde=True does not create .secrets.env."""
    config_path = tmp_path / "archon-search.toml"
    fake_legacy = tmp_path / "fake.plist"
    data_dir = tmp_path / ".archon-search"
    data_dir.mkdir(parents=True, exist_ok=True)

    installer = create_installer(config_file=str(config_path), dry_run=True)

    with (
        patch("archon_search.install.installer.get_default_config_path", return_value=config_path),
        patch("archon_search.install.installer._legacy_service_path", return_value=fake_legacy),
        patch("archon_search.install.installer._remove_legacy_service"),
        patch("archon_search.install.installer._prewarm_models"),
        patch("archon_search.install.installer._check_disk_space"),
        patch("archon_search.install.installer.get_data_dir", return_value=data_dir),
        patch("archon_search.paths.get_data_dir", return_value=data_dir),
        patch.object(BaseInstaller, "detect_gpu", return_value=GpuType.NONE),
        patch.object(BaseInstaller, "validate_providers", return_value=False),
        patch.object(DryRunInstaller, "configure_providers"),
        patch.object(DryRunInstaller, "write_service_file"),
        patch.object(DryRunInstaller, "load_service", return_value=0),
        patch.object(BaseInstaller, "_wait_for_service", return_value=True),
        patch.object(BaseInstaller, "_is_service_running", return_value=False),
        patch("pathlib.Path.home", return_value=tmp_path),
    ):
        rc = installer.run(
            non_interactive=True,
            profile="minimal",
            skip_preload=True,
            enable_hyde=True,
        )

    assert rc == 0
    secrets_path = data_dir / ".secrets.env"
    assert not secrets_path.exists(), ".secrets.env must NOT be created in dry-run mode"


def test_wizard_no_secrets_env_when_expansion_disabled(tmp_path: Path) -> None:
    """Wizard with enable_hyde=False, enable_rag_fusion=False does not create .secrets.env."""
    config_path = tmp_path / "archon-search.toml"
    fake_legacy = tmp_path / "fake.plist"
    data_dir = tmp_path / ".archon-search"
    data_dir.mkdir(parents=True, exist_ok=True)

    installer = create_installer(config_file=str(config_path), dry_run=False)

    with (
        patch("archon_search.install.installer.get_default_config_path", return_value=config_path),
        patch("archon_search.install.installer._legacy_service_path", return_value=fake_legacy),
        patch("archon_search.install.installer._remove_legacy_service"),
        patch("archon_search.install.installer._prewarm_models"),
        patch("archon_search.install.installer._check_disk_space"),
        patch("archon_search.install.installer.get_data_dir", return_value=data_dir),
        patch("archon_search.paths.get_data_dir", return_value=data_dir),
        patch.object(BaseInstaller, "detect_gpu", return_value=GpuType.NONE),
        patch.object(BaseInstaller, "validate_providers", return_value=False),
        patch.object(RealInstaller, "configure_providers"),
        patch.object(RealInstaller, "write_service_file"),
        patch.object(RealInstaller, "load_service", return_value=0),
        patch.object(BaseInstaller, "_wait_for_service", return_value=True),
        patch.object(BaseInstaller, "_is_service_running", return_value=False),
    ):
        rc = installer.run(
            non_interactive=True,
            profile="minimal",
            skip_preload=True,
            enable_hyde=False,
            enable_rag_fusion=False,
        )

    assert rc == 0
    secrets_path = data_dir / ".secrets.env"
    assert not secrets_path.exists(), ".secrets.env must NOT be created when expansion is disabled"


# ---------------------------------------------------------------------------
# Additional tests added in Cycle 2 review
# ---------------------------------------------------------------------------


def test_create_secrets_env_parent_dir_auto_created(tmp_path: Path) -> None:
    """_create_secrets_env creates the parent directory if it does not already exist."""
    secrets_path = tmp_path / "nonexistent_dir" / ".secrets.env"
    result = _create_secrets_env(secrets_path, dry_run=False)
    assert result is True
    assert secrets_path.exists()
    assert secrets_path.parent.exists()
    mode = stat.S_IMODE(secrets_path.stat().st_mode)
    assert mode == 0o600


def test_wizard_secrets_env_oserror_is_nonfatal(tmp_path: Path) -> None:
    """OSError from _create_secrets_env is caught; installer returns 0 and warns to stderr."""
    import io

    config_path = tmp_path / "archon-search.toml"
    fake_legacy = tmp_path / "fake.plist"
    data_dir = tmp_path / ".archon-search"
    data_dir.mkdir(parents=True, exist_ok=True)

    installer = create_installer(config_file=str(config_path), dry_run=False)

    with (
        patch("archon_search.install.installer.get_default_config_path", return_value=config_path),
        patch("archon_search.install.installer._legacy_service_path", return_value=fake_legacy),
        patch("archon_search.install.installer._remove_legacy_service"),
        patch("archon_search.install.installer._prewarm_models"),
        patch("archon_search.install.installer._check_disk_space"),
        patch("archon_search.install.installer.get_data_dir", return_value=data_dir),
        patch("archon_search.paths.get_data_dir", return_value=data_dir),
        patch.object(BaseInstaller, "detect_gpu", return_value=GpuType.NONE),
        patch.object(BaseInstaller, "validate_providers", return_value=False),
        patch.object(RealInstaller, "configure_providers"),
        patch.object(RealInstaller, "write_service_file"),
        patch.object(RealInstaller, "load_service", return_value=0),
        patch.object(BaseInstaller, "_wait_for_service", return_value=True),
        patch.object(BaseInstaller, "_is_service_running", return_value=False),
        patch("pathlib.Path.home", return_value=tmp_path),
        patch("archon_search.install.installer._create_secrets_env", side_effect=PermissionError("permission denied")),
    ):
        stderr_buf = io.StringIO()
        with patch("sys.stderr", stderr_buf):
            rc = installer.run(
                non_interactive=True,
                profile="minimal",
                skip_preload=True,
                enable_hyde=True,
            )

    assert rc == 0, "OSError must be non-fatal; installer must return 0"
    assert "could not create .secrets.env" in stderr_buf.getvalue()


def test_wizard_creates_secrets_env_both_flags_enabled(tmp_path: Path) -> None:
    """Wizard with both enable_hyde=True AND enable_rag_fusion=True creates .secrets.env."""
    config_path = tmp_path / "archon-search.toml"
    fake_legacy = tmp_path / "fake.plist"
    data_dir = tmp_path / ".archon-search"
    data_dir.mkdir(parents=True, exist_ok=True)

    installer = create_installer(config_file=str(config_path), dry_run=False)

    with (
        patch("archon_search.install.installer.get_default_config_path", return_value=config_path),
        patch("archon_search.install.installer._legacy_service_path", return_value=fake_legacy),
        patch("archon_search.install.installer._remove_legacy_service"),
        patch("archon_search.install.installer._prewarm_models"),
        patch("archon_search.install.installer._check_disk_space"),
        patch("archon_search.install.installer.get_data_dir", return_value=data_dir),
        patch("archon_search.paths.get_data_dir", return_value=data_dir),
        patch.object(BaseInstaller, "detect_gpu", return_value=GpuType.NONE),
        patch.object(BaseInstaller, "validate_providers", return_value=False),
        patch.object(RealInstaller, "configure_providers"),
        patch.object(RealInstaller, "write_service_file"),
        patch.object(RealInstaller, "load_service", return_value=0),
        patch.object(BaseInstaller, "_wait_for_service", return_value=True),
        patch.object(BaseInstaller, "_is_service_running", return_value=False),
        patch("pathlib.Path.home", return_value=tmp_path),
    ):
        rc = installer.run(
            non_interactive=True,
            profile="minimal",
            skip_preload=True,
            enable_hyde=True,
            enable_rag_fusion=True,
        )

    assert rc == 0
    secrets_path = data_dir / ".secrets.env"
    assert secrets_path.exists(), ".secrets.env must be created when both flags are True"
    mode = stat.S_IMODE(secrets_path.stat().st_mode)
    assert mode == 0o600


def test_wizard_secrets_env_no_created_hint_on_reinstall(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    """On re-install (file already exists), 'Created:' is NOT printed and content is preserved."""
    config_path = tmp_path / "archon-search.toml"
    fake_legacy = tmp_path / "fake.plist"
    data_dir = tmp_path / ".archon-search"
    data_dir.mkdir(parents=True, exist_ok=True)

    existing_secrets = data_dir / ".secrets.env"
    existing_secrets.write_text("ANTHROPIC_API_KEY=existing_key")
    existing_secrets.chmod(0o600)

    installer = create_installer(config_file=str(config_path), dry_run=False)

    with (
        patch("archon_search.install.installer.get_default_config_path", return_value=config_path),
        patch("archon_search.install.installer._legacy_service_path", return_value=fake_legacy),
        patch("archon_search.install.installer._remove_legacy_service"),
        patch("archon_search.install.installer._prewarm_models"),
        patch("archon_search.install.installer._check_disk_space"),
        patch("archon_search.install.installer.get_data_dir", return_value=data_dir),
        patch("archon_search.paths.get_data_dir", return_value=data_dir),
        patch.object(BaseInstaller, "detect_gpu", return_value=GpuType.NONE),
        patch.object(BaseInstaller, "validate_providers", return_value=False),
        patch.object(RealInstaller, "configure_providers"),
        patch.object(RealInstaller, "write_service_file"),
        patch.object(RealInstaller, "load_service", return_value=0),
        patch.object(BaseInstaller, "_wait_for_service", return_value=True),
        patch.object(BaseInstaller, "_is_service_running", return_value=False),
        patch("pathlib.Path.home", return_value=tmp_path),
    ):
        rc = installer.run(
            non_interactive=True,
            profile="minimal",
            skip_preload=True,
            enable_hyde=True,
        )

    assert rc == 0
    captured = capsys.readouterr()
    assert "Created:" not in captured.out, "Must not print 'Created:' when file already existed"
    assert existing_secrets.read_text() == "ANTHROPIC_API_KEY=existing_key", "Existing content must be preserved"
