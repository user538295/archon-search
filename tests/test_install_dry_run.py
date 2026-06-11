"""TDD tests for --dry-run correctness across all install branches (C14 Task 1.1).

Covers:
- Branch B (fresh install) dry-run: no config written, no .bak created,
  self.cfg reflects selected profile, [DRY RUN] prefix printed, exit code 0.
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from archon_search.install import SearchInstaller
from archon_search.platform.types import GpuType

pytestmark = pytest.mark.xdist_group("install")

# ---------------------------------------------------------------------------
# Shared infrastructure
# ---------------------------------------------------------------------------


def _run_dry_run_fresh(tmp_path: Path, profile: str = "balanced", **run_kwargs):
    """Run wizard with --dry-run on a fresh install (no pre-existing config).

    Returns (installer, rc, config_path).
    """
    config_path = tmp_path / "archon-search.toml"
    fake_legacy = tmp_path / "fake.plist"  # does NOT exist

    installer = SearchInstaller(config_file=str(config_path), dry_run=True)

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
        rc = installer.run(
            non_interactive=True,
            profile=profile,
            skip_preload=True,
            **run_kwargs,
        )

    return installer, rc, config_path


# ---------------------------------------------------------------------------
# Task 1.1 — Branch B (fresh install) dry-run tests
# ---------------------------------------------------------------------------


def test_dry_run_branch_b_no_config_written(tmp_path: Path) -> None:
    """--dry-run on fresh install must NOT create the config file."""
    _, rc, config_path = _run_dry_run_fresh(tmp_path)
    assert rc == 0
    assert not config_path.exists(), "config file must NOT be created in dry-run mode"


def test_dry_run_branch_b_no_bak_written(tmp_path: Path) -> None:
    """--dry-run on fresh install must NOT create a .toml.bak file."""
    _, rc, config_path = _run_dry_run_fresh(tmp_path)
    bak = config_path.with_suffix(".toml.bak")
    assert not bak.exists(), ".toml.bak must NOT be created in dry-run mode"


def test_dry_run_branch_b_cfg_reflects_profile(tmp_path: Path) -> None:
    """After dry-run, installer.cfg must reflect the selected profile, not stale defaults."""
    from archon_search.profiles import get_profile

    installer, rc, _ = _run_dry_run_fresh(tmp_path, profile="balanced")
    assert rc == 0
    expected_model = get_profile("balanced", False).embedder
    assert installer.cfg.embedding_model == expected_model, (
        f"Expected embedding_model={expected_model!r}, got {installer.cfg.embedding_model!r}"
    )


def test_dry_run_branch_b_cfg_not_stale_defaults(tmp_path: Path) -> None:
    """installer.cfg.embedding_model must NOT be the SearchConfig() default after balanced dry-run."""
    from archon_search.config import SearchConfig
    from archon_search.profiles import get_profile

    installer, rc, _ = _run_dry_run_fresh(tmp_path, profile="balanced")
    assert rc == 0
    stale_default = SearchConfig().embedding_model
    expected_model = get_profile("balanced", False).embedder
    # These differ — confirm the test is meaningful
    assert expected_model != stale_default, "Test assumption violated: profiles must differ from default"
    assert installer.cfg.embedding_model != stale_default, (
        "cfg must reflect the selected profile, not stale SearchConfig defaults"
    )


def test_dry_run_branch_b_prints_dry_run_prefix(tmp_path: Path, capsys) -> None:
    """--dry-run must print [DRY RUN] prefix so ops users see what would happen."""
    _run_dry_run_fresh(tmp_path)
    captured = capsys.readouterr()
    assert "[DRY RUN]" in captured.out, "Expected [DRY RUN] in stdout for dry-run fresh install"


def test_dry_run_branch_b_exits_zero(tmp_path: Path) -> None:
    """--dry-run on a clean fresh install must return exit code 0."""
    _, rc, _ = _run_dry_run_fresh(tmp_path)
    assert rc == 0
