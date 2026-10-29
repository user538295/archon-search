"""Tests for the wizard CUDA-torch upgrade (wizard-cuda-torch-upgrade).

Two layers:
- Unit tests for ``_install_cuda_torch`` (version pin, index, best-effort).
- Integration tests driving ``RealInstaller.run()`` through the Step 9 CUDA
  branch to cover the linux/x86_64 platform gate.

No CUDA wheel is ever downloaded and no GPU is required: GPU detection and the
subprocess/metadata calls are mocked.
"""
from __future__ import annotations

import importlib.metadata
import re
import subprocess
import sys
import types
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import tomlkit

from archon_search.install import (
    BaseInstaller,
    RealInstaller,
    _install_cuda_torch,
    create_installer,
)
from archon_search.install.extras import (
    _CUDA_LOCAL_TAG,
    _CUDA_TORCH_INDEX_URL,
    _cuda_visible_in_subprocess,
    _torch_cuda_available,
)
from archon_search.install.installer import (
    _CUDA_GUIDE_DOC,
    _CUDA_GUIDE_HEADING,
    _CUDA_GUIDE_URL,
)
from archon_search.platform.types import GpuType

pytestmark = pytest.mark.xdist_group("install")

_VERSION = "archon_search.install.extras.importlib.metadata.version"
_RUN = "archon_search.install.extras.subprocess.run"


def _versions(mapping: dict[str, str]):
    """side_effect for importlib.metadata.version keyed by dist name."""
    return lambda name: mapping[name]


# ---------------------------------------------------------------------------
# Unit tests: _install_cuda_torch
# ---------------------------------------------------------------------------


class TestInstallCudaTorch:
    """Unit tests for _install_cuda_torch()."""

    def test_local_tag_is_coupled_to_index_url(self):
        """The pinned local tag must equal the CUDA toolkit the index serves.

        `_CUDA_LOCAL_TAG` is derived from `_CUDA_TORCH_INDEX_URL`'s last path
        segment, so this pins the expected value ("cu132") — a drift guard: if
        the index URL is bumped to a different toolkit, this fails until the
        wheel availability is re-verified for the new tag.
        """
        assert _CUDA_LOCAL_TAG == "cu132"

    def test_dry_run_no_subprocess(self):
        """dry_run=True must not call any subprocess or read metadata."""
        with patch(_RUN) as mock_run, patch(_VERSION) as mock_version:
            _install_cuda_torch(dry_run=True)
            mock_run.assert_not_called()
            mock_version.assert_not_called()

    def test_dry_run_prints_expected_message(self, capsys):
        """dry_run=True prints the exact [DRY RUN] line with the index URL."""
        _install_cuda_torch(dry_run=True)
        captured = capsys.readouterr()
        assert f"[DRY RUN] Would install CUDA torch from {_CUDA_TORCH_INDEX_URL}" in captured.out

    @pytest.mark.parametrize(
        ("installed", "expected_torch", "expected_vision", "bare_torch"),
        [
            ({"torch": "2.15.0+cpu", "torchvision": "0.29.2+cpu"}, "torch==2.15.0+cu132", "torchvision==0.29.2+cu132", "torch==2.15.0"),
            ({"torch": "2.15.0", "torchvision": "0.29.2"}, "torch==2.15.0+cu132", "torchvision==0.29.2+cu132", "torch==2.15.0"),
        ],
        ids=["cpu-local-tag", "pypi-no-local-tag"],
    )
    def test_pins_exact_local_cuda_build_not_bare_public_version(self, installed, expected_torch, expected_vision, bare_torch):
        """The swap MUST pin the local +cu132 build, not the bare public version.

        A bare ``torch==2.15.0`` is satisfied by the installed ``2.15.0+cpu``
        (PEP 440 ignores the local label) or by PyPI's plain ``2.15.0`` CUDA
        build, so pip/uv would skip the reinstall and the swap would be a silent
        no-op. The command must carry the local tag, whatever build is installed.
        """
        with patch(_VERSION, side_effect=_versions(installed)), patch(_RUN, return_value=MagicMock(returncode=0)) as mock_run:
            _install_cuda_torch()
        assert mock_run.call_count == 1
        cmd = mock_run.call_args_list[0][0][0]
        assert expected_torch in cmd
        assert expected_vision in cmd
        # a bare public specifier would be a silent no-op — it must NOT be an arg
        assert bare_torch not in cmd
        # PyPI stays available for transitive deps (extra-index, not index)
        assert "--extra-index-url" in cmd
        assert _CUDA_TORCH_INDEX_URL in cmd
        assert "--index-url" not in cmd
        assert cmd[:4] == ["uv", "pip", "install", "--python"]

    def test_uv_absent_falls_back_to_pip_with_local_pin(self):
        """FileNotFoundError from uv triggers a pip fallback keeping the local pin."""
        with (
            patch(_VERSION, side_effect=_versions({"torch": "2.15.0+cpu", "torchvision": "0.29.2+cpu"})),
            patch(_RUN, side_effect=[FileNotFoundError("uv not found"), MagicMock(returncode=0)]) as mock_run,
        ):
            _install_cuda_torch()
        assert mock_run.call_count == 2
        pip_cmd = mock_run.call_args_list[1][0][0]
        assert pip_cmd[1:4] == ["-m", "pip", "install"]
        assert "torch==2.15.0+cu132" in pip_cmd
        assert "torchvision==0.29.2+cu132" in pip_cmd
        assert "--extra-index-url" in pip_cmd

    def test_uv_permission_error_falls_back_to_pip(self):
        """A non-CalledProcessError OSError from uv (e.g. uv not executable /
        wrong-arch) must still fall back to pip, not escape the best-effort guard."""
        with (
            patch(_VERSION, side_effect=_versions({"torch": "2.13.0+cpu", "torchvision": "0.28.0+cpu"})),
            patch(_RUN, side_effect=[PermissionError("uv not executable"), MagicMock(returncode=0)]) as mock_run,
        ):
            _install_cuda_torch()  # must NOT raise
        assert mock_run.call_count == 2
        assert mock_run.call_args_list[1][0][0][1:4] == ["-m", "pip", "install"]

    def test_uv_permission_error_then_pip_fails_keeps_installed_build(self, capsys):
        """uv OSError + pip failure is still best-effort: warn, do NOT raise."""
        pip_err = subprocess.CalledProcessError(1, "pip", stderr=b"no wheel")
        with (
            patch(_VERSION, side_effect=_versions({"torch": "2.13.0+cpu", "torchvision": "0.28.0+cpu"})),
            patch(_RUN, side_effect=[PermissionError("uv not executable"), pip_err]),
        ):
            _install_cuda_torch()  # must NOT raise
        assert "keeping the installed build" in capsys.readouterr().err

    def test_missing_torch_metadata_warns_and_skips(self, capsys):
        """torch not installed → warn to stderr, no subprocess, no raise."""
        with patch(_VERSION, side_effect=importlib.metadata.PackageNotFoundError("torch")):
            with patch(_RUN) as mock_run:
                _install_cuda_torch()  # must NOT raise
            mock_run.assert_not_called()
        assert "skipping the CUDA swap" in capsys.readouterr().err

    def test_both_uv_and_pip_fail_warns_and_keeps_installed_build(self, capsys):
        """Both uv and pip failing is best-effort: warn to stderr, do NOT raise."""
        uv_err = subprocess.CalledProcessError(1, "uv", stderr=b"uv error")
        pip_err = subprocess.CalledProcessError(1, "pip", stderr=b"cuda wheel not found")
        with patch(_VERSION, side_effect=_versions({"torch": "2.13.0+cpu", "torchvision": "0.28.0+cpu"})), patch(_RUN, side_effect=[uv_err, pip_err]):
            _install_cuda_torch()  # must NOT raise
        err = capsys.readouterr().err
        assert "CUDA torch install failed; keeping the installed build" in err
        assert "cuda wheel not found" in err  # the cause is surfaced


# ---------------------------------------------------------------------------
# Unit tests: the CUDA probes the swap relies on
# ---------------------------------------------------------------------------

_SUBPROCESS_RUN = "archon_search.install.extras.subprocess.run"
_PROBE_COMMAND = [sys.executable, "-c", "import torch; print(torch.cuda.is_available())"]


def _completed(stdout: str, returncode: int = 0) -> MagicMock:
    return MagicMock(stdout=stdout, returncode=returncode)


class TestCudaVisibleInSubprocess:
    """``_cuda_visible_in_subprocess`` asks a fresh interpreter, never this process."""

    def test_runs_the_torch_probe_in_a_new_interpreter(self) -> None:
        with patch(_SUBPROCESS_RUN, return_value=_completed("True\n")) as run:
            assert _cuda_visible_in_subprocess() is True

        assert run.call_args.args[0] == _PROBE_COMMAND
        assert run.call_args.kwargs["capture_output"] is True
        assert run.call_args.kwargs["text"] is True
        assert run.call_args.kwargs["timeout"] == 120

    def test_false_output_means_no_cuda(self) -> None:
        with patch(_SUBPROCESS_RUN, return_value=_completed("False\n")):
            assert _cuda_visible_in_subprocess() is False

    def test_a_crashing_interpreter_means_no_cuda(self) -> None:
        with patch(_SUBPROCESS_RUN, return_value=_completed("True\n", returncode=1)):
            assert _cuda_visible_in_subprocess() is False

    @pytest.mark.parametrize(
        "error", [OSError("no interpreter"), subprocess.TimeoutExpired("python", 120)], ids=["os-error", "timeout"]
    )
    def test_an_unrunnable_probe_means_no_cuda(self, error: Exception) -> None:
        with patch(_SUBPROCESS_RUN, side_effect=error):
            assert _cuda_visible_in_subprocess() is False


class TestTorchCudaAvailable:
    """``_torch_cuda_available`` reads this process's torch and never raises."""

    @pytest.mark.parametrize("available", [True, False])
    def test_reports_what_torch_says(self, available: bool) -> None:
        fake_torch = types.SimpleNamespace(cuda=types.SimpleNamespace(is_available=lambda: available))
        with patch.dict(sys.modules, {"torch": fake_torch}):
            assert _torch_cuda_available() is available

    def test_an_unimportable_torch_reads_as_no_cuda(self) -> None:
        with patch.dict(sys.modules, {"torch": None}):
            assert _torch_cuda_available() is False


# ---------------------------------------------------------------------------
# Integration tests: run() Step 9 CUDA branch — the swap is the enabling step
# ---------------------------------------------------------------------------


def _as_mock(outcome: bool | MagicMock) -> MagicMock:
    """A probe stand-in: *outcome* itself when it is already a mock, else one returning it."""
    return outcome if isinstance(outcome, MagicMock) else MagicMock(return_value=outcome)


def _run_cuda_install(
    tmp_path: Path,
    *,
    gpu: GpuType = GpuType.CUDA,
    machine: str = "x86_64",
    platform_sys: str = "linux",
    cuda_swap: MagicMock | None = None,
    disable_gpu: bool = False,
    dry_run: bool = False,
    gpu_confirm: bool = True,
    torch_has_cuda: bool = False,
    cuda_after_swap: bool | MagicMock = True,
    models_validate: bool | MagicMock = True,
    seed: str | None = None,
) -> tuple[int, str | None]:
    """Drive run() with detection/infra mocked; return ``(exit code, [database].device or None)``.

    ``cuda_swap`` (if given) is patched in at the installer call site so the test can
    assert whether/how the swap ran; ``None`` exercises the REAL ``_install_cuda_torch``
    (with ``extras.subprocess.run`` mocked separately). ``torch_has_cuda`` is this process's
    torch, ``cuda_after_swap`` what a fresh interpreter reports after the swap, and
    ``models_validate`` the in-process model probe used when no swap runs; each of the last
    two may be a MagicMock to assert on its calls.
    """
    config_path = tmp_path / "archon-search.toml"
    if seed is not None:
        config_path.write_text(seed)
    target = RealInstaller

    patches = [
        patch("archon_search.install.installer.get_default_config_path", return_value=config_path),
        patch("archon_search.install.installer._legacy_service_path", return_value=tmp_path / "fake.plist"),
        patch("archon_search.install.installer._remove_legacy_service"),
        patch("archon_search.install.installer._prewarm_models"),
        patch("archon_search.install.installer._check_disk_space"),
        patch("archon_search.install.installer.platform.machine", return_value=machine),
        patch("archon_search.install.installer.sys.platform", platform_sys),
        patch("archon_search.install.installer._prompt_gpu_confirm", return_value=gpu_confirm),
        patch("archon_search.install.installer._torch_cuda_available", return_value=torch_has_cuda),
        patch("archon_search.install.installer._cuda_visible_in_subprocess", _as_mock(cuda_after_swap)),
        patch("builtins.input", MagicMock()),
        patch.object(BaseInstaller, "detect_gpu", return_value=gpu),
        patch.object(BaseInstaller, "_accelerator_validated", _as_mock(models_validate)),
        patch.object(target, "write_service_file"),
        patch.object(target, "load_service", return_value=0),
        patch.object(BaseInstaller, "_wait_for_service", return_value=True),
        patch.object(BaseInstaller, "_is_service_running", return_value=False),
    ]
    if cuda_swap is not None:
        patches.append(patch("archon_search.install.installer._install_cuda_torch", cuda_swap))

    with ExitStack() as stack:
        for cm in patches:
            stack.enter_context(cm)
        installer = create_installer(config_file=str(config_path), dry_run=dry_run)
        rc = installer.run(
            non_interactive=True,
            profile="minimal",
            skip_preload=True,
            disable_gpu=disable_gpu,
        )
    if not config_path.exists():
        return rc, None
    return rc, tomlkit.parse(config_path.read_text()).unwrap()["database"].get("device")


def test_cuda_swap_runs_on_linux_x86_64_when_torch_lacks_cuda(tmp_path: Path) -> None:
    """linux + x86_64 + CUDA accepted + torch.cuda unavailable (non-dry-run) → swap runs, dry_run=False."""
    swap = MagicMock()
    rc, _ = _run_cuda_install(tmp_path, cuda_swap=swap)
    assert rc == 0
    swap.assert_called_once_with(dry_run=False)


def test_swap_runs_only_when_cuda_unavailable_after_accepting_gpu(tmp_path: Path) -> None:
    """A torch that already sees CUDA keeps its build: no swap, the in-process model probe decides."""
    swap = MagicMock()
    rc, device = _run_cuda_install(tmp_path, cuda_swap=swap, torch_has_cuda=True)
    assert rc == 0
    swap.assert_not_called()
    assert device == "auto"


def test_cuda_swap_skipped_on_windows_amd64(tmp_path: Path) -> None:
    """Windows (win32/AMD64) is out of scope — the CUDA swap targets linux/x86_64 only."""
    swap = MagicMock()
    rc, _ = _run_cuda_install(tmp_path, machine="AMD64", platform_sys="win32", cuda_swap=swap)
    assert rc == 0
    swap.assert_not_called()


def test_cuda_swap_skipped_on_linux_arm(tmp_path: Path) -> None:
    """CUDA on a non-x86_64 (ARM) linux host → no swap (out of scope)."""
    swap = MagicMock()
    rc, _ = _run_cuda_install(tmp_path, machine="aarch64", cuda_swap=swap)
    assert rc == 0
    swap.assert_not_called()


def test_cuda_swap_skipped_on_macos_x86(tmp_path: Path) -> None:
    """x86_64 but non-linux (macOS) → no swap: the CUDA swap targets linux/x86_64 only."""
    swap = MagicMock()
    rc, _ = _run_cuda_install(tmp_path, machine="x86_64", platform_sys="darwin", cuda_swap=swap)
    assert rc == 0
    swap.assert_not_called()


def test_cuda_swap_skipped_on_metal(tmp_path: Path) -> None:
    """Apple Silicon (Metal) → no swap."""
    swap = MagicMock()
    rc, _ = _run_cuda_install(tmp_path, gpu=GpuType.METAL, machine="arm64", platform_sys="darwin", cuda_swap=swap)
    assert rc == 0
    swap.assert_not_called()


def test_cuda_swap_skipped_on_none_gpu(tmp_path: Path) -> None:
    """No GPU → no swap even on linux x86_64."""
    swap = MagicMock()
    rc, device = _run_cuda_install(tmp_path, gpu=GpuType.NONE, cuda_swap=swap)
    assert rc == 0
    swap.assert_not_called()
    assert device == "cpu"


def test_cuda_swap_skipped_on_user_decline(tmp_path: Path) -> None:
    """User declines the GPU prompt → enable_gpu False → no swap, CPU written."""
    swap = MagicMock()
    rc, device = _run_cuda_install(tmp_path, cuda_swap=swap, gpu_confirm=False)
    assert rc == 0
    swap.assert_not_called()
    assert device == "cpu"


def test_cuda_swap_skipped_on_disable_gpu_flag(tmp_path: Path) -> None:
    """--disable-gpu → enable_gpu False → no swap, CPU written."""
    swap = MagicMock()
    rc, device = _run_cuda_install(tmp_path, cuda_swap=swap, disable_gpu=True)
    assert rc == 0
    swap.assert_not_called()
    assert device == "cpu"


def test_cuda_swap_dry_run_narrates_and_does_not_install(tmp_path: Path) -> None:
    """Dry-run passes dry_run=True to the swap (which narrates, installs nothing)."""
    swap = MagicMock()
    rc, _ = _run_cuda_install(tmp_path, cuda_swap=swap, dry_run=True)
    assert rc == 0
    swap.assert_called_once_with(dry_run=True)


def test_cuda_dry_run_neither_probes_nor_writes_the_device(tmp_path: Path) -> None:
    """Dry-run must not load models or spawn the CUDA check, and must leave the config untouched."""
    seed = '[database]\ndevice = "cpu"\n'
    models_probe, subprocess_probe = MagicMock(), MagicMock()
    rc, _ = _run_cuda_install(
        tmp_path,
        cuda_swap=MagicMock(),
        dry_run=True,
        seed=seed,
        models_validate=models_probe,
        cuda_after_swap=subprocess_probe,
    )

    assert rc == 0
    models_probe.assert_not_called()
    subprocess_probe.assert_not_called()
    assert (tmp_path / "archon-search.toml").read_text() == seed


def test_swap_that_gives_a_cuda_torch_writes_auto_and_reports_cuda(tmp_path: Path, capsys) -> None:
    """The swap is the enabling step: a fresh interpreter that sees CUDA afterwards → ``auto``."""
    rc, device = _run_cuda_install(tmp_path, cuda_swap=MagicMock(), cuda_after_swap=True)

    assert rc == 0
    assert device == "auto"
    out = capsys.readouterr().out
    assert "  Device:     CUDA" in out.splitlines()
    assert "validation failed" not in out
    assert "Keeping the installed PyTorch build" not in out


def test_swap_that_leaves_torch_without_cuda_writes_cpu_with_the_guidance(tmp_path: Path, capsys) -> None:
    """Still no CUDA in the fresh interpreter: CPU replaces a previous run's ``auto`` and the guide is printed."""
    rc, device = _run_cuda_install(
        tmp_path, cuda_swap=MagicMock(), cuda_after_swap=False, seed='[database]\ndevice = "auto"\n'
    )

    assert rc == 0
    assert device == "cpu"
    out = capsys.readouterr().out.splitlines()
    assert "Warning: CUDA validation failed — falling back to CPU." in out
    assert f"  Commands for each install method: {_CUDA_GUIDE_URL}" in out
    assert "  Device:     CPU" in out


def test_real_swap_failure_keeps_installed_build_and_install_succeeds(tmp_path: Path, capsys) -> None:
    """End-to-end: real gate + real _install_cuda_torch, only subprocess failing.

    A failed swap warns, keeps the installed build, finds no CUDA afterwards, writes CPU
    and lets the install finish rc==0.
    """
    uv_err = subprocess.CalledProcessError(1, "uv", stderr=b"uv error")
    pip_err = subprocess.CalledProcessError(1, "pip", stderr=b"cuda wheel download failed")
    with (
        patch(_VERSION, side_effect=_versions({"torch": "2.13.0+cpu", "torchvision": "0.28.0+cpu"})),
        patch(_RUN, side_effect=[uv_err, pip_err]),
    ):
        rc, device = _run_cuda_install(tmp_path, cuda_swap=None, cuda_after_swap=False)
    assert rc == 0  # a failed GPU upgrade never fails the install
    assert device == "cpu"
    assert "keeping the installed build" in capsys.readouterr().err


@pytest.mark.parametrize(("platform_sys", "machine"), [("win32", "AMD64"), ("linux", "aarch64")])
def test_cuda_off_swap_platform_is_decided_by_the_model_probe(
    tmp_path: Path, capsys, platform_sys: str, machine: str
) -> None:
    """Off linux/x86_64 there is no swap: a passing model probe writes ``auto``, a failing one CPU."""
    swap = MagicMock()
    rc, passed = _run_cuda_install(
        tmp_path, cuda_swap=swap, machine=machine, platform_sys=platform_sys, models_validate=True
    )
    assert (rc, passed) == (0, "auto")
    assert "validation failed" not in capsys.readouterr().out

    second = tmp_path / "second"
    second.mkdir()
    rc, failed = _run_cuda_install(
        second, cuda_swap=swap, machine=machine, platform_sys=platform_sys, models_validate=False
    )
    assert (rc, failed) == (0, "cpu")
    assert "Warning: CUDA validation failed — falling back to CPU." in capsys.readouterr().out
    swap.assert_not_called()


def _github_slug(heading: str) -> str:
    """GitHub's heading anchor: lowercase, drop punctuation except hyphens/underscores/spaces, spaces → hyphens."""
    return re.sub(r"[^\w\- ]", "", heading.lower()).replace(" ", "-")


def test_cuda_fallback_guidance_url_points_at_an_existing_doc_heading() -> None:
    """The fallback prints a GitHub URL; a moved file, renamed heading or wrong anchor
    would send the operator nowhere."""
    url_path, anchor = _CUDA_GUIDE_URL.split("#")
    assert url_path.endswith(f"/blob/main/{_CUDA_GUIDE_DOC}")
    doc = Path(__file__).resolve().parents[1] / _CUDA_GUIDE_DOC

    assert doc.is_file()
    heading = re.compile(rf"#{{1,6}} {re.escape(_CUDA_GUIDE_HEADING)}")
    assert any(heading.fullmatch(line) for line in doc.read_text(encoding="utf-8").splitlines())
    assert anchor == _github_slug(_CUDA_GUIDE_HEADING)
