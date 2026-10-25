"""The wizard asks `Proceed?` BEFORE its Apple GPU/CUDA check downloads and loads the models.

Before this ordering the check ran first: it pulled ~3 GB, and only then did the summary announce
`Download: ~4586 MB` and ask for confirmation. A "no" must now cost nothing.
"""
from __future__ import annotations

from contextlib import ExitStack
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from archon_search.install import BaseInstaller, RealInstaller, create_installer
from archon_search.install.installer import _planned_device_labels
from archon_search.install.render import _render_summary as real_render_summary
from archon_search.platform.types import GpuType

_PROCEED_PROMPT = "Proceed? [y/n]: "
_APPLE_GPU_LABEL = "Apple GPU (mps)"


class _Run:
    """Everything an interactive Metal install did, in order, with the transcript."""

    def __init__(self) -> None:
        self.events: list[str] = []
        self.proceed_prompts = 0
        self.rc = -1
        self.out = ""
        self.probe = MagicMock()
        self.configure_device = MagicMock()
        self.create_data_dir = MagicMock()


def _run_interactive_metal_install(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    proceed_answers: list[str],
    *,
    probe_labels: list[str] | None = None,
) -> _Run:
    run = _Run()
    config_path = tmp_path / "archon-search.toml"
    answers = iter(proceed_answers)

    def fake_input(prompt: str = "") -> str:
        if prompt == _PROCEED_PROMPT:
            run.proceed_prompts += 1
            run.events.append("proceed")
            return next(answers)
        assert "[y/n]" in prompt, f"unexpected non-yes/no prompt: {prompt!r}"
        return "y" if prompt.startswith("Keep reranker") else "n"

    def recording_summary(*args: object, **kwargs: object) -> str:
        run.events.append("summary")
        return real_render_summary(*args, **kwargs)  # type: ignore[arg-type]

    def recording_probe(*_args: object, **_kwargs: object) -> list[str]:
        run.events.append("probe")
        return list(probe_labels if probe_labels is not None else [_APPLE_GPU_LABEL])

    run.probe.side_effect = recording_probe
    run.create_data_dir.side_effect = lambda *_a, **_k: run.events.append("data_dir")

    patches = [
        patch("archon_search.install.installer.get_default_config_path", return_value=config_path),
        patch("archon_search.install.installer._legacy_service_path", return_value=tmp_path / "fake.plist"),
        patch("archon_search.install.installer._remove_legacy_service"),
        patch("archon_search.install.installer._prewarm_models"),
        patch("archon_search.install.installer._check_disk_space"),
        patch("archon_search.install.installer._render_summary", recording_summary),
        patch("archon_search.install.installer._prompt_gpu_confirm", return_value=True),
        patch("builtins.input", fake_input),
        patch.object(BaseInstaller, "detect_gpu", return_value=GpuType.METAL),
        patch.object(RealInstaller, "_probe_and_configure_device", run.probe),
        patch.object(RealInstaller, "configure_device", run.configure_device),
        patch.object(RealInstaller, "create_data_dir", run.create_data_dir),
        patch.object(RealInstaller, "write_service_file"),
        patch.object(RealInstaller, "load_service", return_value=0),
        patch.object(BaseInstaller, "_wait_for_service", return_value=True),
        patch.object(BaseInstaller, "_is_service_running", return_value=False),
    ]
    with ExitStack() as stack:
        for cm in patches:
            stack.enter_context(cm)
        installer = create_installer(config_file=str(config_path))
        run.rc = installer.run(
            non_interactive=False,
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
    run.out = capsys.readouterr().out
    return run


def test_run_declinedProceed_neverProbesDownloadsOrWritesTheDevice(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    run = _run_interactive_metal_install(tmp_path, capsys, ["n"])

    assert run.rc == 1
    assert "Installation aborted." in run.out
    run.probe.assert_not_called()
    run.configure_device.assert_not_called()
    run.create_data_dir.assert_not_called()
    assert run.events == ["summary", "proceed"]


def test_run_confirmedProceed_probesOnlyAfterSummaryAndConfirmation(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    run = _run_interactive_metal_install(tmp_path, capsys, ["y"])

    assert run.rc == 0
    assert run.events == ["summary", "proceed", "probe", "data_dir"]


def test_run_proceedPrompt_acceptsOnlyYOrN_andNeverAbortsOnWrongInput(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Enter, a word, and a typed-ahead escape sequence are all refused and the question is asked again."""
    run = _run_interactive_metal_install(tmp_path, capsys, ["", "yes", "\x1bb\x1bfy", "maybe", "y"])

    assert run.rc == 0
    assert run.proceed_prompts == 5
    assert run.out.count("Please answer y or n.") == 4
    assert run.events.index("probe") > run.events.index("proceed")


def test_run_summaryBeforeProbe_namesAPendingCheckNotAResult(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    run = _run_interactive_metal_install(tmp_path, capsys, ["y"], probe_labels=["CPU"])

    pending = f"Device:     {_APPLE_GPU_LABEL} — checked after you confirm; CPU if it fails"
    assert pending in run.out
    # The outcome is announced once the check has actually run: here it fell back to CPU.
    assert run.out.index(pending) < run.out.index("Device:     CPU")


def test_run_summaryBeforeProbe_theOutcomeLineNamesWhatTheCheckSettledOn(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    run = _run_interactive_metal_install(tmp_path, capsys, ["y"], probe_labels=[_APPLE_GPU_LABEL])

    assert f"\n  Device:     {_APPLE_GPU_LABEL}\n" in run.out


@pytest.mark.parametrize(
    ("gpu", "enable_gpu", "dry_run", "expected"),
    [
        (GpuType.METAL, True, False, [f"{_APPLE_GPU_LABEL} — checked after you confirm; CPU if it fails"]),
        (GpuType.CUDA, True, False, ["CUDA — checked after you confirm; CPU if it fails"]),
        (GpuType.CUDA, True, True, ["CUDA"]),
        (GpuType.METAL, True, True, [_APPLE_GPU_LABEL]),
        (GpuType.METAL, False, False, ["CPU"]),
        (GpuType.CUDA, False, False, ["CPU"]),
        (GpuType.NONE, True, False, ["CPU"]),
    ],
)
def test_plannedDeviceLabels_namesOnlyWhatWillBeChecked(
    gpu: GpuType, enable_gpu: bool, dry_run: bool, expected: list[str]
) -> None:
    assert _planned_device_labels(gpu, enable_gpu, dry_run) == expected
