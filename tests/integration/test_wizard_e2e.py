"""Task 4.2 — Wizard configurability e2e tests.

Exercises the wizard CLI with configurability flags: --db-path writability
check, --enable-hyde, --enable-rag-fusion, and the post-install "Next steps"
output block.

All tests run without real model downloads, service starts, or disk access
beyond the tmp_path — heavy operations are patched to no-ops or stubs.

Run with:
    uv run pytest tests/integration/test_wizard_e2e.py -v
"""
from __future__ import annotations

import contextlib
from pathlib import Path
from typing import Generator
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

pytestmark = pytest.mark.integration


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


@contextlib.contextmanager
def _noop_install_lock() -> Generator[None, None, None]:
    """No-op replacement for _acquire_install_lock.

    The real lock writes to ~/.archon-search/.install.lock.  Without this
    patch, parallel xdist workers running wizard tests concurrently with the
    install xdist_group can deadlock on the shared advisory lock.
    """
    yield


def _base_wizard_patches() -> dict:
    """Return patches that prevent real service/disk/network operations."""
    from archon_search.platform.types import GpuType

    return {
        "detect_gpu": MagicMock(return_value=GpuType.NONE),
        "validate_providers": MagicMock(return_value=False),
        "configure_providers": MagicMock(),
        "write_service_file": MagicMock(),
        "load_service": MagicMock(return_value=0),
        "_wait_for_service": MagicMock(return_value=True),
        "_is_service_running": MagicMock(return_value=False),
    }


def _run_wizard(
    tmp_path: Path,
    extra_args: list[str] | None = None,
    *,
    dry_run: bool = True,
) -> object:
    """Invoke wizard via CliRunner on a clean tmp_path.

    Patches _acquire_install_lock, _prewarm_models, _check_disk_space, and
    heavy SearchInstaller methods so tests run without real model downloads
    or service operations.

    When dry_run=False a mock for load_or_generate_key is also applied so
    Step 17 does not try to read a real key file.

    Callers that need env var isolation (e.g., ANTHROPIC_API_KEY) must call
    monkeypatch.setenv() in the test function itself before invoking this helper.
    """
    from archon_search.cli.main import main
    from archon_search.install import SearchInstaller

    config_path = tmp_path / "archon-search.toml"
    runner = CliRunner()

    args = [
        "wizard",
        "--non-interactive",
        "--profile", "minimal",
        "--skip-preload",
        "--config", str(config_path),
    ]
    if dry_run:
        args.append("--dry-run")
    if extra_args:
        args.extend(extra_args)

    # Patch load_or_generate_key used in Step 17 (non-dry-run only)
    _mock_key = ("fake_api_key_for_tests", "file")

    install_patches: dict = {
        "_prewarm_models": MagicMock(),
        "_check_disk_space": MagicMock(),
        "_legacy_service_path": MagicMock(return_value=tmp_path / "fake.plist"),
        "_remove_legacy_service": MagicMock(),
        "_acquire_install_lock": _noop_install_lock,
        "load_or_generate_key": MagicMock(return_value=_mock_key),
    }

    with patch.multiple("archon_search.install", **install_patches):
        with patch.multiple(SearchInstaller, **_base_wizard_patches()):
            result = runner.invoke(main, args)
    return result


# ---------------------------------------------------------------------------
# Test 1 — --db-path not writable exits non-zero
# ---------------------------------------------------------------------------


@pytest.mark.xdist_group("install")
def test_wizard_db_path_not_writable_exits_nonzero(tmp_path: Path) -> None:
    """wizard --db-path /nonexistent/path/db exits non-zero with an error message.

    The wizard attempts to create and write-check the provided db_path.
    A path under /nonexistent is not creatable, so mkdir raises OSError and
    the wizard prints an error and returns exit code 1.
    """
    from archon_search.cli.main import main
    from archon_search.install import SearchInstaller

    config_path = tmp_path / "archon-search.toml"
    runner = CliRunner()

    # /nonexistent/path/db cannot be created — OS will reject the mkdir.
    bad_db_path = "/nonexistent/path/db"

    with patch.multiple(
        "archon_search.install",
        _prewarm_models=MagicMock(),
        _check_disk_space=MagicMock(),
        _legacy_service_path=MagicMock(return_value=tmp_path / "fake.plist"),
        _remove_legacy_service=MagicMock(),
        _acquire_install_lock=_noop_install_lock,
        load_or_generate_key=MagicMock(return_value=("fake_key", "file")),
    ):
        with patch.multiple(SearchInstaller, **_base_wizard_patches()):
            result = runner.invoke(main, [
                "wizard",
                "--non-interactive",
                "--profile", "minimal",
                "--skip-preload",
                "--dry-run",
                "--config", str(config_path),
                "--db-path", bad_db_path,
            ])

    assert result.exit_code != 0, (
        f"expected non-zero exit for non-writable --db-path, got {result.exit_code}.\n"
        f"Output:\n{result.output}"
    )
    combined = result.output + (str(result.exception) if result.exception else "")
    assert "Error" in combined or "error" in combined or "nonexistent" in combined, (
        f"expected error message referencing the bad path, got:\n{combined}"
    )


# ---------------------------------------------------------------------------
# Test 2 — --enable-hyde writes [hyde] enabled = true to TOML
# ---------------------------------------------------------------------------


@pytest.mark.xdist_group("install")
def test_wizard_non_interactive_hyde_accepted_writes_toml(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """wizard --non-interactive --enable-hyde writes [hyde] enabled = true.

    Verifies that the wizard's --enable-hyde flag propagates through
    WizardFeatures → _apply_wizard_features_to_toml → written TOML file.
    ANTHROPIC_API_KEY must be set to pass the CLI validation guard.
    """
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key-for-wizard-tests")

    result = _run_wizard(
        tmp_path,
        extra_args=["--enable-hyde"],
        dry_run=False,  # config file must be written to disk
    )

    assert result.exit_code == 0, (
        f"wizard --enable-hyde exited {result.exit_code}:\n{result.output}"
    )

    config_path = tmp_path / "archon-search.toml"
    assert config_path.exists(), (
        f"TOML config not written at {config_path}. Output:\n{result.output}"
    )

    import tomlkit
    doc = tomlkit.parse(config_path.read_text())
    assert "hyde" in doc, (
        f"Expected [hyde] section in TOML, got sections: {list(doc.keys())}\n"
        f"TOML contents:\n{config_path.read_text()}"
    )
    assert doc["hyde"].get("enabled") is True, (
        f"Expected [hyde] enabled = true, got: {dict(doc['hyde'])}"
    )


# ---------------------------------------------------------------------------
# Test 3 — without --enable-hyde, [hyde] section is absent from TOML
# ---------------------------------------------------------------------------


@pytest.mark.xdist_group("install")
def test_wizard_non_interactive_hyde_declined_omits_toml_key(
    tmp_path: Path,
) -> None:
    """wizard --non-interactive without --enable-hyde omits [hyde] from TOML.

    Verifies that HyDE is disabled by default and the wizard does not write
    [hyde] to the config when the flag is absent.
    """
    result = _run_wizard(
        tmp_path,
        dry_run=False,  # config file must be written to disk
    )

    assert result.exit_code == 0, (
        f"wizard (no --enable-hyde) exited {result.exit_code}:\n{result.output}"
    )

    config_path = tmp_path / "archon-search.toml"
    assert config_path.exists(), (
        f"TOML config not written at {config_path}. Output:\n{result.output}"
    )

    import tomlkit
    doc = tomlkit.parse(config_path.read_text())
    assert "hyde" not in doc, (
        f"Expected no [hyde] section in TOML (hyde not requested), "
        f"but found: {dict(doc.get('hyde', {}))}\n"
        f"TOML contents:\n{config_path.read_text()}"
    )


# ---------------------------------------------------------------------------
# Test 4 — --enable-rag-fusion writes [rag_fusion] enabled = true to TOML
# ---------------------------------------------------------------------------


@pytest.mark.xdist_group("install")
def test_wizard_non_interactive_rag_fusion_accepted_writes_toml(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """wizard --non-interactive --enable-rag-fusion writes [rag_fusion] enabled = true.

    Verifies that the wizard's --enable-rag-fusion flag propagates through
    WizardFeatures → _apply_wizard_features_to_toml → written TOML file.
    ANTHROPIC_API_KEY must be set to pass the CLI validation guard.
    """
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key-for-wizard-tests")

    result = _run_wizard(
        tmp_path,
        extra_args=["--enable-rag-fusion"],
        dry_run=False,  # config file must be written to disk
    )

    assert result.exit_code == 0, (
        f"wizard --enable-rag-fusion exited {result.exit_code}:\n{result.output}"
    )

    config_path = tmp_path / "archon-search.toml"
    assert config_path.exists(), (
        f"TOML config not written at {config_path}. Output:\n{result.output}"
    )

    import tomlkit
    doc = tomlkit.parse(config_path.read_text())
    assert "rag_fusion" in doc, (
        f"Expected [rag_fusion] section in TOML, got sections: {list(doc.keys())}\n"
        f"TOML contents:\n{config_path.read_text()}"
    )
    assert doc["rag_fusion"].get("enabled") is True, (
        f"Expected [rag_fusion] enabled = true, got: {dict(doc['rag_fusion'])}"
    )


# ---------------------------------------------------------------------------
# Test 5 — successful wizard run output contains "Next steps" block
# ---------------------------------------------------------------------------


@pytest.mark.xdist_group("install")
def test_wizard_summary_contains_next_steps_block(
    tmp_path: Path,
) -> None:
    """Any successful non-dry-run wizard run includes a 'Next steps' section.

    _print_next_steps() (called at Step 16b) prints 'Next steps:' followed
    by example CLI commands. This verifies the onboarding block is present
    in the wizard output after a successful install.
    """
    result = _run_wizard(
        tmp_path,
        dry_run=False,  # _print_next_steps is only called in non-dry-run mode
    )

    assert result.exit_code == 0, (
        f"wizard exited {result.exit_code} (expected 0):\n{result.output}"
    )
    assert "Next steps" in result.output, (
        f"Expected 'Next steps' block in wizard output, but not found.\n"
        f"Full output:\n{result.output}"
    )
