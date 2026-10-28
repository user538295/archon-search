"""S768: the summary's `Device:` line names a pending accelerator check — in a dry run too.

UserManual/20_wizard.md documents that, with the GPU accepted, the summary reads
`Device:     Apple GPU (mps) — checked after you confirm; CPU if it fails` because the check
runs only after `Proceed?`. `--dry-run` runs no check at all, so the note is just as true there.
"""
from __future__ import annotations

import tomllib
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

import pytest

from archon_search.install import BaseInstaller, RealInstaller, create_installer
from archon_search.platform.types import GpuType

_PENDING_LINE = "Device:     Apple GPU (mps) — checked after you confirm; CPU if it fails"
_WOULD_SET_LINE = "[DRY RUN] Would set [database] device = 'auto'."


def _run_wizard(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    *,
    dry_run: bool,
    non_interactive: bool,
    proceed_answer: str = "n",
) -> tuple[int, str, Path]:
    config_path = tmp_path / "archon-search.toml"

    def fake_input(prompt: str = "") -> str:
        if prompt == "Proceed? [y/n]: ":
            return proceed_answer
        assert "[y/n]" in prompt, f"unexpected non-yes/no prompt: {prompt!r}"
        return "y" if prompt.startswith("Keep reranker") else "n"

    patches = [
        patch("archon_search.install.installer.get_default_config_path", return_value=config_path),
        patch("archon_search.install.installer._legacy_service_path", return_value=tmp_path / "fake.plist"),
        patch("archon_search.install.installer._remove_legacy_service"),
        patch("archon_search.install.installer._prewarm_models"),
        patch("archon_search.install.installer._check_disk_space"),
        patch("archon_search.install.installer._prompt_gpu_confirm", return_value=True),
        patch("builtins.input", fake_input),
        patch.object(BaseInstaller, "detect_gpu", return_value=GpuType.METAL),
        patch.object(RealInstaller, "_probe_and_configure_device", side_effect=AssertionError("probe ran")),
        patch.object(RealInstaller, "create_data_dir"),
        patch.object(RealInstaller, "write_service_file"),
        patch.object(RealInstaller, "load_service", return_value=0),
        patch.object(BaseInstaller, "_wait_for_service", return_value=True),
        patch.object(BaseInstaller, "_is_service_running", return_value=False),
    ]
    with ExitStack() as stack:
        for cm in patches:
            stack.enter_context(cm)
        installer = create_installer(config_file=str(config_path), dry_run=dry_run)
        rc = installer.run(
            non_interactive=non_interactive,
            profile="minimal",
            multilingual=False,
            skip_preload=True,
            install_code=False,
            disable_reranker=None,
            enable_watch=False,
            enable_telemetry=False,
            eager_load=False,
            routing_strategy="centroid",
            log_format="text",
        )
    return rc, capsys.readouterr().out, config_path


def test_dryRunNonInteractive_acceptedGpu_summaryDeviceLineIsFlaggedPending(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    rc, out, _ = _run_wizard(tmp_path, capsys, dry_run=True, non_interactive=True)

    assert rc == 0
    assert [line for line in out.splitlines() if line.strip().startswith("Device:")] == [f"  {_PENDING_LINE}"]
    assert _WOULD_SET_LINE in out


def test_interactiveDecline_acceptedGpu_summaryFlaggedPendingAndNothingSettled(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    rc, out, config_path = _run_wizard(tmp_path, capsys, dry_run=False, non_interactive=False)

    assert rc == 1
    assert _PENDING_LINE in out
    assert "Installation aborted." in out
    assert [line for line in out.splitlines() if line.strip().startswith("Device:")] == [f"  {_PENDING_LINE}"]
    assert "validation failed" not in out
    if config_path.exists():
        assert "device" not in tomllib.loads(config_path.read_text()).get("database", {})
