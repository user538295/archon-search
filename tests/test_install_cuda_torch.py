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
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from archon_search.install import (
    BaseInstaller,
    RealInstaller,
    _install_cuda_torch,
    create_installer,
)
from archon_search.install.extras import _CUDA_LOCAL_TAG, _CUDA_TORCH_INDEX_URL
from archon_search.install.installer import _CUDA_GUIDE_DOC, _CUDA_GUIDE_HEADING, _CUDA_GUIDE_URL
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
        segment, so this pins the expected value ("cu126") — a drift guard: if
        the index URL is bumped to a different toolkit, this fails until the
        wheel availability is re-verified for the new tag.
        """
        assert _CUDA_LOCAL_TAG == "cu126"

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
            ({"torch": "2.13.0+cpu", "torchvision": "0.28.0+cpu"}, "torch==2.13.0+cu126", "torchvision==0.28.0+cu126", "torch==2.13.0"),
            ({"torch": "2.14.1", "torchvision": "0.29.1"}, "torch==2.14.1+cu126", "torchvision==0.29.1+cu126", "torch==2.14.1"),
        ],
        ids=["cpu-local-tag", "pypi-no-local-tag"],
    )
    def test_pins_exact_local_cuda_build_not_bare_public_version(self, installed, expected_torch, expected_vision, bare_torch):
        """The swap MUST pin the local +cu126 build, not the bare public version.

        A bare ``torch==2.13.0`` is satisfied by the installed ``2.13.0+cpu``
        (PEP 440 ignores the local label) or by PyPI's plain ``2.14.1`` CUDA
        build, so pip/uv would skip the reinstall and the swap would be a silent
        no-op. The command must carry the local tag, whatever build is installed.
        """
        with patch(_VERSION, side_effect=_versions(installed)):
            with patch(_RUN, return_value=MagicMock(returncode=0)) as mock_run:
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
        with patch(_VERSION, side_effect=_versions({"torch": "2.13.0+cpu", "torchvision": "0.28.0+cpu"})):
            with patch(_RUN, side_effect=[FileNotFoundError("uv not found"), MagicMock(returncode=0)]) as mock_run:
                _install_cuda_torch()
        assert mock_run.call_count == 2
        pip_cmd = mock_run.call_args_list[1][0][0]
        assert pip_cmd[1:4] == ["-m", "pip", "install"]
        assert "torch==2.13.0+cu126" in pip_cmd
        assert "torchvision==0.28.0+cu126" in pip_cmd
        assert "--extra-index-url" in pip_cmd

    def test_uv_permission_error_falls_back_to_pip(self):
        """A non-CalledProcessError OSError from uv (e.g. uv not executable /
        wrong-arch) must still fall back to pip, not escape the best-effort guard."""
        with patch(_VERSION, side_effect=_versions({"torch": "2.13.0+cpu", "torchvision": "0.28.0+cpu"})):
            with patch(_RUN, side_effect=[PermissionError("uv not executable"), MagicMock(returncode=0)]) as mock_run:
                _install_cuda_torch()  # must NOT raise
        assert mock_run.call_count == 2
        assert mock_run.call_args_list[1][0][0][1:4] == ["-m", "pip", "install"]

    def test_uv_permission_error_then_pip_fails_keeps_installed_build(self, capsys):
        """uv OSError + pip failure is still best-effort: warn, do NOT raise."""
        pip_err = subprocess.CalledProcessError(1, "pip", stderr=b"no wheel")
        with patch(_VERSION, side_effect=_versions({"torch": "2.13.0+cpu", "torchvision": "0.28.0+cpu"})):
            with patch(_RUN, side_effect=[PermissionError("uv not executable"), pip_err]):
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
        with patch(_VERSION, side_effect=_versions({"torch": "2.13.0+cpu", "torchvision": "0.28.0+cpu"})):
            with patch(_RUN, side_effect=[uv_err, pip_err]):
                _install_cuda_torch()  # must NOT raise
        err = capsys.readouterr().err
        assert "CUDA torch install failed; keeping the installed build" in err
        assert "cuda wheel not found" in err  # the cause is surfaced


# ---------------------------------------------------------------------------
# Integration tests: run() Step 9 CUDA branch — linux/x86_64 platform gate
# ---------------------------------------------------------------------------


def _run_cuda_install(
    tmp_path: Path,
    *,
    gpu: GpuType,
    machine: str,
    platform_sys: str = "linux",
    cuda_swap: MagicMock | None = None,
    disable_gpu: bool = False,
    dry_run: bool = False,
    gpu_confirm: bool = True,
) -> int:
    """Drive run() with detection/infra mocked; return the exit code.

    ``cuda_swap`` (if given) is patched in at the installer call site so the test
    can assert whether/how the swap ran. Pass ``None`` to exercise the REAL
    ``_install_cuda_torch`` (with extras.subprocess.run mocked separately).
    """
    config_path = tmp_path / "archon-search.toml"
    fake_legacy = tmp_path / "fake.plist"
    target = RealInstaller

    patches = [
        patch("archon_search.install.installer.get_default_config_path", return_value=config_path),
        patch("archon_search.install.installer._legacy_service_path", return_value=fake_legacy),
        patch("archon_search.install.installer._remove_legacy_service"),
        patch("archon_search.install.installer._prewarm_models"),
        patch("archon_search.install.installer._check_disk_space"),
        patch("archon_search.install.installer.platform.machine", return_value=machine),
        patch("archon_search.install.installer.sys.platform", platform_sys),
        patch("archon_search.install.installer._prompt_gpu_confirm", return_value=gpu_confirm),
        patch("builtins.input", MagicMock()),
        patch.object(BaseInstaller, "detect_gpu", return_value=gpu),
        patch.object(BaseInstaller, "validate_providers", return_value=False),
        patch.object(target, "configure_providers"),
        patch.object(target, "_probe_and_configure_coreml", return_value=[]),
        patch.object(target, "write_gpu_providers_disabled"),
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
        return installer.run(
            non_interactive=True,
            profile="minimal",
            skip_preload=True,
            disable_gpu=disable_gpu,
        )


def test_cuda_swap_runs_on_linux_x86_64(tmp_path: Path) -> None:
    """linux + x86_64 + CUDA + confirm (non-dry-run) → swap runs, dry_run=False."""
    swap = MagicMock()
    rc = _run_cuda_install(tmp_path, gpu=GpuType.CUDA, machine="x86_64", cuda_swap=swap)
    assert rc == 0
    swap.assert_called_once_with(dry_run=False)


def test_cuda_swap_skipped_on_windows_amd64(tmp_path: Path) -> None:
    """Windows (win32/AMD64) is out of scope — the CUDA swap targets linux/x86_64 only."""
    swap = MagicMock()
    rc = _run_cuda_install(
        tmp_path, gpu=GpuType.CUDA, machine="AMD64", platform_sys="win32", cuda_swap=swap
    )
    assert rc == 0
    swap.assert_not_called()


def test_cuda_swap_skipped_on_linux_arm(tmp_path: Path) -> None:
    """CUDA on a non-x86_64 (ARM) linux host → no swap (out of scope)."""
    swap = MagicMock()
    rc = _run_cuda_install(tmp_path, gpu=GpuType.CUDA, machine="aarch64", cuda_swap=swap)
    assert rc == 0
    swap.assert_not_called()


def test_cuda_swap_skipped_on_macos_x86(tmp_path: Path) -> None:
    """x86_64 but non-linux (macOS) → no swap: the CUDA swap targets linux/x86_64 only."""
    swap = MagicMock()
    rc = _run_cuda_install(
        tmp_path, gpu=GpuType.CUDA, machine="x86_64", platform_sys="darwin", cuda_swap=swap
    )
    assert rc == 0
    swap.assert_not_called()


def test_cuda_swap_skipped_on_metal(tmp_path: Path) -> None:
    """Apple Silicon (Metal) → no swap."""
    swap = MagicMock()
    rc = _run_cuda_install(
        tmp_path, gpu=GpuType.METAL, machine="arm64", platform_sys="darwin", cuda_swap=swap
    )
    assert rc == 0
    swap.assert_not_called()


def test_cuda_swap_skipped_on_none_gpu(tmp_path: Path) -> None:
    """No GPU → no swap even on linux x86_64."""
    swap = MagicMock()
    rc = _run_cuda_install(tmp_path, gpu=GpuType.NONE, machine="x86_64", cuda_swap=swap)
    assert rc == 0
    swap.assert_not_called()


def test_cuda_swap_skipped_on_user_decline(tmp_path: Path) -> None:
    """User declines the GPU prompt → enable_gpu False → no swap."""
    swap = MagicMock()
    rc = _run_cuda_install(
        tmp_path, gpu=GpuType.CUDA, machine="x86_64", cuda_swap=swap, gpu_confirm=False
    )
    assert rc == 0
    swap.assert_not_called()


def test_cuda_swap_skipped_on_disable_gpu_flag(tmp_path: Path) -> None:
    """--disable-gpu → enable_gpu False → no swap."""
    swap = MagicMock()
    rc = _run_cuda_install(
        tmp_path, gpu=GpuType.CUDA, machine="x86_64", cuda_swap=swap, disable_gpu=True
    )
    assert rc == 0
    swap.assert_not_called()


def test_cuda_swap_dry_run_narrates_and_does_not_install(tmp_path: Path) -> None:
    """Dry-run passes dry_run=True to the swap (which narrates, installs nothing)."""
    swap = MagicMock()
    rc = _run_cuda_install(
        tmp_path, gpu=GpuType.CUDA, machine="x86_64", cuda_swap=swap, dry_run=True
    )
    assert rc == 0
    swap.assert_called_once_with(dry_run=True)


def test_cuda_dry_run_neither_probes_nor_writes_providers(tmp_path: Path) -> None:
    """Dry-run must not load models to probe CUDA, and must leave the config untouched."""
    seed = '[database]\nproviders = ["CoreMLExecutionProvider"]\n'
    probe = MagicMock(return_value=(True, True, []))
    with patch.object(
        BaseInstaller, "validate_providers", autospec=True, side_effect=BaseInstaller.validate_providers
    ) as validate:
        rc, _ = _run_cuda_install_real_providers(
            tmp_path, [_CUDA_EP, _CPU_EP], MagicMock(), dry_run=True, seed=seed, probe=probe
        )

    assert rc == 0
    validate.assert_not_called()
    probe.assert_not_called()
    assert (tmp_path / "archon-search.toml").read_text() == seed


def test_real_swap_failure_keeps_installed_build_and_install_succeeds(tmp_path: Path, capsys) -> None:
    """End-to-end: real gate + real _install_cuda_torch, only subprocess failing.

    Exercises the whole path together (no mock seam): a real install failure must
    warn, keep the installed build, and let the install finish rc==0.
    """
    uv_err = subprocess.CalledProcessError(1, "uv", stderr=b"uv error")
    pip_err = subprocess.CalledProcessError(1, "pip", stderr=b"cuda wheel download failed")
    with (
        patch(_VERSION, side_effect=_versions({"torch": "2.13.0+cpu", "torchvision": "0.28.0+cpu"})),
        patch(_RUN, side_effect=[uv_err, pip_err]),
    ):
        rc = _run_cuda_install(tmp_path, gpu=GpuType.CUDA, machine="x86_64", cuda_swap=None)
    assert rc == 0  # a failed GPU upgrade never fails the install
    assert "keeping the installed build" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# T21: Step 9 must validate CUDA before writing CUDAExecutionProvider (ADR 15).
# Real configure_providers / write_gpu_providers_disabled / validate_providers;
# only onnxruntime's provider list (and the model probe) are faked.
# ---------------------------------------------------------------------------

_CUDA_EP = "CUDAExecutionProvider"
_CPU_EP = "CPUExecutionProvider"


def _run_cuda_install_real_providers(
    tmp_path: Path,
    available: list[str],
    cuda_swap: MagicMock,
    *,
    dry_run: bool = False,
    seed: str | None = None,
    probe: MagicMock | None = None,
    machine: str = "x86_64",
    platform_sys: str = "linux",
) -> tuple[int, dict]:
    """Drive a CUDA-accepted run() (linux/x86_64 by default); return (rc, the config's [database] table).

    *seed* is written as the existing config first; *probe* replaces the model probe
    (``validate_providers_shared``), which passes by default.
    """
    import tomlkit

    config_path = tmp_path / "archon-search.toml"
    if seed is not None:
        config_path.write_text(seed)
    with (
        patch("archon_search.install.installer.get_default_config_path", return_value=config_path),
        patch("archon_search.install.installer._legacy_service_path", return_value=tmp_path / "fake.plist"),
        patch("archon_search.install.installer._remove_legacy_service"),
        patch("archon_search.install.installer._prewarm_models"),
        patch("archon_search.install.installer._check_disk_space"),
        patch("archon_search.install.installer._install_cuda_torch", cuda_swap),
        patch("archon_search.install.installer.platform.machine", return_value=machine),
        patch("archon_search.install.installer.sys.platform", platform_sys),
        patch("archon_search.install.installer._prompt_gpu_confirm", return_value=True),
        patch("archon_search.onnx_providers._available_providers", return_value=available),
        patch(
            "archon_search.model_validation.validate_providers_shared",
            probe if probe is not None else MagicMock(return_value=(True, True, [])),
        ),
        patch.object(BaseInstaller, "detect_gpu", return_value=GpuType.CUDA),
        patch.object(RealInstaller, "write_service_file"),
        patch.object(RealInstaller, "load_service", return_value=0),
        patch.object(BaseInstaller, "_wait_for_service", return_value=True),
        patch.object(BaseInstaller, "_is_service_running", return_value=False),
    ):
        installer = create_installer(config_file=str(config_path), dry_run=dry_run)
        rc = installer.run(non_interactive=True, profile="minimal", skip_preload=True)
    database = tomlkit.parse(config_path.read_text())["database"]
    return rc, database.unwrap()


_FALLBACK_GUIDANCE = [
    "Warning: CUDA validation failed — falling back to CPU.",
    (
        "  Text search and result ranking (ONNX Runtime) will run on CPU: "
        "the installed onnxruntime has no usable CUDAExecutionProvider."
    ),
    (
        "  To run them on the GPU, replace onnxruntime with onnxruntime-gpu "
        "(its PyPI build needs CUDA 13 and an NVIDIA driver >= 580), then re-run the wizard."
    ),
    (
        "  Commands for each install method: https://github.com/user538295/archon-search/blob/main/"
        "Documentation/UserManual/10_installation.md#nvidia-gpu-run-embedding-and-reranking-on-cuda"
    ),
]


def _assert_no_fallback_guidance(out: str) -> None:
    out_lines = out.splitlines()
    assert not any(line in out_lines for line in _FALLBACK_GUIDANCE)
    assert "  Providers:  (CPU default)" not in out_lines


def _assert_fallback_guidance(out: str) -> None:
    out_lines = out.splitlines()
    start = out_lines.index(_FALLBACK_GUIDANCE[0])
    assert out_lines[start : start + len(_FALLBACK_GUIDANCE)] == _FALLBACK_GUIDANCE
    assert "  Providers:  (CPU default)" in out_lines


def test_cuda_without_onnxruntime_cuda_ep_falls_back_to_cpu(tmp_path: Path, capsys, caplog) -> None:
    """CPU-only onnxruntime: the wizard must replace a previous run's CUDA list with CPU
    (the runtime would silently drop CUDA), must warn with the full guidance, and must
    still run the independent torch swap."""
    swap = MagicMock()
    with caplog.at_level("WARNING", logger="archon_search.install.installer"):
        rc, database = _run_cuda_install_real_providers(
            tmp_path, [_CPU_EP], swap, seed=f'[database]\nproviders = ["{_CUDA_EP}"]\n'
        )

    assert rc == 0
    assert database["providers"] == []
    _assert_fallback_guidance(capsys.readouterr().out)
    assert any(
        r.levelname == "WARNING" and r.getMessage() == "CUDA validation failed — falling back to CPU"
        for r in caplog.records
    )
    swap.assert_called_once_with(dry_run=False)


def test_cuda_ep_with_failing_reranker_falls_back_to_cpu_without_split(tmp_path: Path, capsys) -> None:
    """CUDA EP present but only the embedder loads on it: CUDA has no embedder-only split,
    so CPU is written, the fallback warning shows, and the torch swap still runs."""
    swap = MagicMock()
    probe = MagicMock(return_value=(True, False, ["reranker failed on CUDA"]))
    rc, database = _run_cuda_install_real_providers(tmp_path, [_CUDA_EP, _CPU_EP], swap, probe=probe)

    assert rc == 0
    assert database["providers"] == []
    assert "reranker_providers" not in database
    _assert_fallback_guidance(capsys.readouterr().out)
    swap.assert_called_once_with(dry_run=False)


@pytest.mark.parametrize(("platform_sys", "machine"), [("win32", "AMD64"), ("linux", "aarch64")])
def test_cuda_probe_fail_off_swap_platform_falls_back_to_cpu(
    tmp_path: Path, capsys, platform_sys: str, machine: str
) -> None:
    """Off linux/x86_64 a failing probe still writes CPU and warns, and there is no torch swap."""
    swap = MagicMock()
    rc, database = _run_cuda_install_real_providers(
        tmp_path, [_CPU_EP], swap, machine=machine, platform_sys=platform_sys
    )

    assert rc == 0
    assert database["providers"] == []
    _assert_fallback_guidance(capsys.readouterr().out)
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


def test_write_gpu_providers_disabled_adds_a_missing_database_table(tmp_path: Path) -> None:
    """A config without [database] still ends with providers = [] (CPU), other tables kept."""
    import tomlkit

    config_path = tmp_path / "archon-search.toml"
    config_path.write_text('[server]\nport = 9000\n')
    create_installer(config_file=str(config_path)).write_gpu_providers_disabled(config_path)

    doc = tomlkit.parse(config_path.read_text()).unwrap()
    assert doc["database"]["providers"] == []
    assert doc["server"]["port"] == 9000


def test_write_gpu_providers_disabled_without_config_creates_nothing(tmp_path: Path) -> None:
    config_path = tmp_path / "archon-search.toml"
    create_installer(config_file=str(config_path)).write_gpu_providers_disabled(config_path)

    assert not config_path.exists()


def test_cuda_with_onnxruntime_cuda_ep_writes_cuda(tmp_path: Path, capsys) -> None:
    """CUDA EP available and the model probe passes → CUDAExecutionProvider is written."""
    swap = MagicMock()
    rc, database = _run_cuda_install_real_providers(tmp_path, [_CUDA_EP, _CPU_EP], swap)

    assert rc == 0
    assert database["providers"] == [_CUDA_EP]
    out = capsys.readouterr().out
    assert "  Providers:  CUDA" in out.splitlines()
    _assert_no_fallback_guidance(out)
    swap.assert_not_called()


@pytest.mark.parametrize(("platform_sys", "machine"), [("win32", "AMD64"), ("linux", "aarch64")])
def test_cuda_probe_pass_off_swap_platform_writes_cuda_silently(
    tmp_path: Path, capsys, platform_sys: str, machine: str
) -> None:
    """Off linux/x86_64 there is no torch swap to skip, so no 'Keeping…' notice either."""
    swap = MagicMock()
    rc, database = _run_cuda_install_real_providers(
        tmp_path, [_CUDA_EP, _CPU_EP], swap, machine=machine, platform_sys=platform_sys
    )

    assert rc == 0
    assert database["providers"] == [_CUDA_EP]
    swap.assert_not_called()
    out = capsys.readouterr().out
    assert "Keeping the installed PyTorch build" not in out
    _assert_no_fallback_guidance(out)


def test_cuda_probe_pass_removes_a_stale_reranker_providers(tmp_path: Path) -> None:
    """A previous split run left reranker_providers = []; once CUDA validates for both
    models the reranker must follow [database].providers again."""
    rc, database = _run_cuda_install_real_providers(
        tmp_path, [_CUDA_EP, _CPU_EP], MagicMock(), seed="[database]\nreranker_providers = []\n"
    )

    assert rc == 0
    assert database["providers"] == [_CUDA_EP]
    assert "reranker_providers" not in database


def test_cuda_probe_pass_keeps_installed_torch_and_says_why(tmp_path: Path, capsys) -> None:
    """A passing CUDA probe means onnxruntime-gpu is installed; the cu126 torch swap would
    overwrite its shared cuDNN files, so the swap is skipped with a one-line notice."""
    swap = MagicMock()
    rc, _ = _run_cuda_install_real_providers(tmp_path, [_CUDA_EP, _CPU_EP], swap)

    assert rc == 0
    swap.assert_not_called()
    out = capsys.readouterr().out
    assert (
        "Keeping the installed PyTorch build: onnxruntime-gpu shares its CUDA libraries, "
        "which the CUDA 12.6 PyTorch build would replace."
    ) in out
    _assert_no_fallback_guidance(out)
