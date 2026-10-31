"""Tests for BE-12: wizard creates .secrets.env and macOS wrapper when HyDE/RAG Fusion enabled."""
from __future__ import annotations

import stat
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from archon_search.config import _PROVIDER_REGISTRY
from archon_search.install import (
    BaseInstaller,
    DryRunInstaller,
    RealInstaller,
    WizardFeatures,
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

    with (
        patch("archon_search.install.installer.get_default_config_path", return_value=config_path),
        patch("archon_search.install.installer._legacy_service_path", return_value=fake_legacy),
        patch("archon_search.install.installer._remove_legacy_service"),
        patch("archon_search.install.installer._prewarm_models"),
        patch("archon_search.install.installer._check_disk_space"),
        patch("archon_search.install.installer.get_data_dir", return_value=data_dir),
        patch("archon_search.paths.get_data_dir", return_value=data_dir),
        patch("archon_search.platform.macos.get_data_dir", return_value=data_dir),
        patch.object(BaseInstaller, "detect_gpu", return_value=GpuType.NONE),
        patch.object(BaseInstaller, "_probe_and_configure_device", return_value=[]),
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


def test_wizard_systemd_unit_loads_the_secrets_env_it_creates_on_linux(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """End-to-end: on Linux with a custom data dir, the unit's EnvironmentFile= is the .secrets.env the wizard created."""
    config_path = tmp_path / "archon-search.toml"
    fake_legacy = tmp_path / "fake.service"
    data_dir = tmp_path / "custom-data"
    data_dir.mkdir()
    monkeypatch.setenv("ARCHON_SEARCH_DATA_DIR", str(data_dir))
    unit_path = tmp_path / "systemd" / "archon-search.service"

    installer = create_installer(config_file=str(config_path), dry_run=False)

    from archon_search.platform.linux import SystemdSearchService

    svc = SystemdSearchService()
    ok = subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")

    with (
        patch("archon_search.install.installer.get_default_config_path", return_value=config_path),
        patch("archon_search.install.installer._legacy_service_path", return_value=fake_legacy),
        patch("archon_search.install.installer._remove_legacy_service"),
        patch("archon_search.install.installer._prewarm_models"),
        patch("archon_search.install.installer._check_disk_space"),
        patch.object(BaseInstaller, "detect_gpu", return_value=GpuType.NONE),
        patch.object(BaseInstaller, "_probe_and_configure_device", return_value=[]),
        patch.object(BaseInstaller, "_wait_for_service", return_value=True),
        patch.object(BaseInstaller, "_is_service_running", return_value=False),
        patch("archon_search.install.installer.get_search_service", return_value=svc),
        patch("pathlib.Path.home", return_value=tmp_path),
        patch.object(SystemdSearchService, "_unit_path", property(lambda self: unit_path)),
        patch.object(svc, "_run", return_value=ok),
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
    secrets_path = data_dir / ".secrets.env"
    assert secrets_path.exists(), ".secrets.env must be created under the custom data dir"
    mode = stat.S_IMODE(secrets_path.stat().st_mode)
    assert mode == 0o600, f".secrets.env must have mode 0o600, got 0o{mode:o}"
    env_file_lines = [
        line for line in unit_path.read_text().splitlines() if line.startswith("EnvironmentFile=-")
    ]
    assert len(env_file_lines) == 1, env_file_lines
    assert env_file_lines[0] == f"EnvironmentFile=-{secrets_path}"


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
        patch.object(BaseInstaller, "_probe_and_configure_device", return_value=[]),
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
        patch.object(BaseInstaller, "_probe_and_configure_device", return_value=[]),
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
        patch.object(BaseInstaller, "_probe_and_configure_device", return_value=[]),
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
        patch.object(BaseInstaller, "_probe_and_configure_device", return_value=[]),
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
        patch.object(BaseInstaller, "_probe_and_configure_device", return_value=[]),
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


def test_wizard_secrets_env_created_hint_is_provider_neutral(
    tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    """The 'Created:' hint names both key-needing providers' variables, not only Anthropic's."""
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
        patch.object(BaseInstaller, "_probe_and_configure_device", return_value=[]),
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
    out = capsys.readouterr().out
    assert f"  Created:    {data_dir / '.secrets.env'}\n" in out, out
    assert "ANTHROPIC_API_KEY=<key>" in out, out
    assert "OPENAI_API_KEY=<key>" in out, out
    assert "Add ANTHROPIC_API_KEY=<key> to this file" not in out, out


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
        patch.object(BaseInstaller, "_probe_and_configure_device", return_value=[]),
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


# ---------------------------------------------------------------------------
# FE-8: description and graph providers that need an API key count too
# ---------------------------------------------------------------------------


def _run_with_features(tmp_path: Path, features: WizardFeatures, *, dry_run: bool = False) -> tuple[int, Path]:
    """Run a non-interactive install whose wizard answers are *features*; return the exit code and the data dir."""
    config_path = tmp_path / "archon-search.toml"
    data_dir = tmp_path / ".archon-search"
    data_dir.mkdir(parents=True, exist_ok=True)
    installer = create_installer(config_file=str(config_path), dry_run=dry_run)
    with (
        patch("archon_search.install.installer.get_default_config_path", return_value=config_path),
        patch("archon_search.install.installer._legacy_service_path", return_value=tmp_path / "fake.plist"),
        patch("archon_search.install.installer._remove_legacy_service"),
        patch("archon_search.install.installer._prewarm_models"),
        patch("archon_search.install.installer._check_disk_space"),
        patch("archon_search.install.installer._prompt_optional_features", return_value=features),
        patch("archon_search.install.installer.get_data_dir", return_value=data_dir),
        patch("archon_search.paths.get_data_dir", return_value=data_dir),
        patch.object(BaseInstaller, "detect_gpu", return_value=GpuType.NONE),
        patch.object(BaseInstaller, "_probe_and_configure_device", return_value=[]),
        patch.object(RealInstaller, "write_service_file"),
        patch.object(RealInstaller, "load_service", return_value=0),
        patch.object(BaseInstaller, "_wait_for_service", return_value=True),
        patch.object(BaseInstaller, "_is_service_running", return_value=False),
        patch("pathlib.Path.home", return_value=tmp_path),
    ):
        rc = installer.run(non_interactive=True, profile="minimal", skip_preload=True)
    return rc, data_dir


def test_secrets_env_created_for_description_provider_needing_key(
    tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    """A description provider that needs a key creates the file and the message names the feature."""
    rc, data_dir = _run_with_features(
        tmp_path, WizardFeatures(description_enabled=True, description_provider="openai", description_model="gpt-5-mini")
    )

    assert rc == 0
    secrets_path = data_dir / ".secrets.env"
    assert secrets_path.exists()
    out = capsys.readouterr().out
    assert f"  Created:    {secrets_path}\n" in out, out
    assert "for collection descriptions." in out, out
    assert "AI query expansion" not in out, out


def test_secrets_env_created_for_graph_provider_needing_key(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    rc, data_dir = _run_with_features(
        tmp_path, WizardFeatures(graph_provider="anthropic", graph_extraction_model="claude-haiku-4-5")
    )

    assert rc == 0
    assert (data_dir / ".secrets.env").exists()
    assert "for graph enrichment." in capsys.readouterr().out


def test_secrets_env_message_names_every_feature_that_needs_a_key(
    tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    rc, _ = _run_with_features(
        tmp_path,
        WizardFeatures(
            enable_hyde=True,
            description_enabled=True,
            description_provider="anthropic",
            graph_provider="openai",
            graph_extraction_model="gpt-5-mini",
        ),
    )

    assert rc == 0
    assert "for AI query expansion, collection descriptions and graph enrichment." in capsys.readouterr().out


@pytest.mark.parametrize(
    "features",
    [
        WizardFeatures(description_enabled=True, description_provider="ollama", description_model="m1"),
        WizardFeatures(description_enabled=True, description_provider="claude_cli"),
        WizardFeatures(graph_provider="llama_cpp", graph_extraction_model="m1"),
        WizardFeatures(graph_provider="claude_cli"),
        WizardFeatures(description_enabled=False, description_provider="openai", description_model="m"),
        WizardFeatures(graph_provider="openai", graph_extraction_model=""),
    ],
    ids=["description-ollama", "description-claude_cli", "graph-llama_cpp", "graph-claude_cli", "declined", "graph-no-model"],
)
def test_no_secrets_env_when_no_description_or_graph_provider_needs_a_key(
    tmp_path: Path, features: WizardFeatures
) -> None:
    rc, data_dir = _run_with_features(tmp_path, features)

    assert rc == 0
    assert not (data_dir / ".secrets.env").exists()


@pytest.mark.parametrize(
    "features",
    [
        WizardFeatures(description_enabled=True, description_provider="openai", description_model="gpt-5-mini"),
        WizardFeatures(graph_provider="anthropic", graph_extraction_model="claude-haiku-4-5"),
    ],
    ids=["description-openai", "graph-anthropic"],
)
def test_dry_run_creates_no_secrets_env_for_description_or_graph(tmp_path: Path, features: WizardFeatures) -> None:
    rc, data_dir = _run_with_features(tmp_path, features, dry_run=True)

    assert rc == 0
    assert not (data_dir / ".secrets.env").exists()


@pytest.mark.parametrize("provider", _PROVIDER_REGISTRY)
def test_api_key_providers_agree_with_provider_key_available(
    provider: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The wizard's idea of a provider needing a key must not drift from the runtime's key check."""
    from archon_search.install.config_writer import API_KEY_PROVIDERS
    from archon_search.llm_client.provider_key import provider_key_available

    for variable in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY"):
        monkeypatch.delenv(variable, raising=False)

    assert (provider in API_KEY_PROVIDERS) is (not provider_key_available(provider))


_KEY_TIP = "Tip: Set $ANTHROPIC_API_KEY to enable AI query expansion"


@pytest.fixture
def no_anthropic_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)


@pytest.mark.usefixtures("no_anthropic_key")
def test_key_tip_names_every_feature_when_none_is_configured(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    rc, _ = _run_with_features(tmp_path, WizardFeatures())

    assert rc == 0
    out = capsys.readouterr().out
    assert f"{_KEY_TIP} (HyDE + RAG Fusion), collection descriptions or graph enrichment next run." in out


@pytest.mark.usefixtures("no_anthropic_key")
@pytest.mark.parametrize(
    "features",
    [
        WizardFeatures(enable_hyde=True),
        WizardFeatures(description_enabled=True, description_provider="openai", description_model="gpt-5-mini"),
        WizardFeatures(description_enabled=True, description_provider="ollama", description_model="m1"),
        WizardFeatures(graph_provider="llama_cpp", graph_extraction_model="m1"),
    ],
    ids=["hyde", "description-openai", "description-ollama", "graph-llama_cpp"],
)
def test_key_tip_is_not_shown_once_a_provider_feature_is_configured(
    tmp_path: Path, capsys: pytest.CaptureFixture, features: WizardFeatures
) -> None:
    rc, _ = _run_with_features(tmp_path, features)

    assert rc == 0
    assert _KEY_TIP not in capsys.readouterr().out


def test_key_tip_is_not_shown_when_the_key_is_already_set(
    tmp_path: Path, capsys: pytest.CaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")

    rc, _ = _run_with_features(tmp_path, WizardFeatures())

    assert rc == 0
    assert _KEY_TIP not in capsys.readouterr().out


@pytest.mark.usefixtures("no_anthropic_key")
def test_key_tip_is_not_shown_in_a_dry_run(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    rc, _ = _run_with_features(tmp_path, WizardFeatures(), dry_run=True)

    assert rc == 0
    assert _KEY_TIP not in capsys.readouterr().out


def test_secrets_env_message_with_two_features_joins_them_with_and(
    tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    rc, _ = _run_with_features(
        tmp_path,
        WizardFeatures(enable_hyde=True, description_enabled=True, description_provider="openai", description_model="m"),
    )

    assert rc == 0
    assert "for AI query expansion and collection descriptions." in capsys.readouterr().out
