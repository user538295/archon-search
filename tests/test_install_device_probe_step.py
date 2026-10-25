"""Step 9 of the wizard: settle ``[database].device`` (and write ``[section].device``).

Covers ``configure_device`` (the one device writer) and ``_probe_and_configure_device``
(the one probe): a declined or absent GPU writes ``cpu``; an accepted accelerator writes
``auto`` only when PyTorch's ``auto`` lands on an accelerator and both models pass there;
every other outcome writes ``cpu`` with the fallback warning. The CUDA torch swap is
covered in ``test_install_cuda_torch.py``.
"""
from __future__ import annotations

import logging
from pathlib import Path
from unittest.mock import patch

import pytest
import tomlkit

from archon_search.install import create_installer
from archon_search.install.installer import _CUDA_GUIDE_URL
from archon_search.platform.types import GpuType

_VALIDATE = "archon_search.model_validation.validate_device"
_RESOLVE = "archon_search.install.installer.resolve_device"


@pytest.fixture
def off_swap_platform():
    """Run as macOS/arm64, so a CUDA install never takes the linux/x86_64 torch-swap path."""
    with (
        patch("archon_search.install.installer.sys.platform", "darwin"),
        patch("archon_search.install.installer.platform.machine", return_value="arm64"),
    ):
        yield


def _make_toml(tmp_path: Path, body: str = '[server]\nhost = "127.0.0.1"\n') -> Path:
    toml_file = tmp_path / "archon-search.toml"
    toml_file.write_text(body, encoding="utf-8")
    return toml_file


def _device(toml_file: Path, section: str = "database") -> object:
    return tomlkit.parse(toml_file.read_text()).unwrap()[section]["device"]


class TestConfigureDevice:
    def test_writes_the_device_into_the_database_section_by_default(self, tmp_path: Path) -> None:
        toml_file = _make_toml(tmp_path)
        create_installer(config_file=str(toml_file)).configure_device("auto")

        assert _device(toml_file) == "auto"

    def test_writes_the_graph_section_when_asked_and_keeps_other_tables(self, tmp_path: Path) -> None:
        toml_file = _make_toml(tmp_path, '[server]\nport = 9000\n')
        create_installer(config_file=str(toml_file)).configure_device("cuda", section="graph")

        doc = tomlkit.parse(toml_file.read_text()).unwrap()
        assert doc["graph"]["device"] == "cuda"
        assert doc["server"]["port"] == 9000
        assert "database" not in doc

    def test_replaces_a_previous_value(self, tmp_path: Path) -> None:
        toml_file = _make_toml(tmp_path, '[database]\ndevice = "auto"\n')
        create_installer(config_file=str(toml_file)).configure_device("cpu")

        assert _device(toml_file) == "cpu"

    def test_an_unchanged_value_does_not_rewrite_the_file(self, tmp_path: Path) -> None:
        toml_file = _make_toml(tmp_path, '# my comment\n[database]\ndevice   =   "cpu"\n')
        before = toml_file.read_text()
        mtime = toml_file.stat().st_mtime_ns
        create_installer(config_file=str(toml_file)).configure_device("cpu")

        assert toml_file.read_text() == before
        assert toml_file.stat().st_mtime_ns == mtime

    @pytest.mark.parametrize("value", ["gpu", "CoreMLExecutionProvider", "", "CPU"])
    def test_a_value_outside_the_device_settings_is_rejected(self, tmp_path: Path, value: str) -> None:
        toml_file = _make_toml(tmp_path)
        before = toml_file.read_text()

        with pytest.raises(ValueError, match="expected one of: auto, cpu, mps, cuda"):
            create_installer(config_file=str(toml_file)).configure_device(value)

        assert toml_file.read_text() == before

    def test_a_missing_config_file_logs_and_creates_nothing(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        toml_file = tmp_path / "archon-search.toml"
        with caplog.at_level(logging.WARNING):
            create_installer(config_file=str(toml_file)).configure_device("cpu")

        assert not toml_file.exists()
        assert f"Config file {toml_file} not found" in caplog.text

    def test_a_section_that_is_not_a_table_is_skipped(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        toml_file = _make_toml(tmp_path, 'database = "oops"\n')
        before = toml_file.read_text()
        with caplog.at_level(logging.WARNING):
            create_installer(config_file=str(toml_file)).configure_device("cpu")

        assert toml_file.read_text() == before
        assert "[database]" in caplog.text
        assert "is not a table" in caplog.text

    def test_dry_run_narrates_and_writes_nothing(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        toml_file = _make_toml(tmp_path)
        before = toml_file.read_text()
        create_installer(config_file=str(toml_file), dry_run=True).configure_device("auto", section="graph")

        assert toml_file.read_text() == before
        assert capsys.readouterr().out == "[DRY RUN] Would set [graph] device = 'auto'.\n"

    def test_dry_run_rejects_a_value_outside_the_device_settings(self, tmp_path: Path) -> None:
        toml_file = _make_toml(tmp_path)
        with pytest.raises(ValueError):
            create_installer(config_file=str(toml_file), dry_run=True).configure_device("gpu")


class TestProbeAndConfigureDevice:
    def test_no_gpu_writes_cpu_without_probing(self, tmp_path: Path) -> None:
        toml_file = _make_toml(tmp_path)
        installer = create_installer(config_file=str(toml_file))

        with patch(_VALIDATE) as validate:
            labels = installer._probe_and_configure_device(GpuType.NONE)

        assert labels == ["CPU"]
        assert _device(toml_file) == "cpu"
        validate.assert_not_called()

    def test_a_rerun_without_a_gpu_replaces_a_stale_auto(self, tmp_path: Path) -> None:
        toml_file = _make_toml(tmp_path, '[database]\ndevice = "auto"\n')
        create_installer(config_file=str(toml_file))._probe_and_configure_device(GpuType.NONE)

        assert _device(toml_file) == "cpu"

    def test_metal_passing_both_models_on_mps_writes_auto(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        toml_file = _make_toml(tmp_path)
        installer = create_installer(config_file=str(toml_file))

        with patch(_RESOLVE, return_value="mps"), patch(_VALIDATE, return_value=(True, True, [])) as validate:
            labels = installer._probe_and_configure_device(GpuType.METAL)

        assert labels == ["Apple GPU (mps)"]
        assert _device(toml_file) == "auto"
        validate.assert_called_once_with(
            "auto", installer.cfg.embedding_model, installer.cfg.reranker_model
        )
        assert "validation failed" not in capsys.readouterr().out

    def test_cuda_passing_both_models_on_cuda_writes_auto(self, tmp_path: Path, off_swap_platform: None) -> None:
        toml_file = _make_toml(tmp_path)
        installer = create_installer(config_file=str(toml_file))

        with patch(_RESOLVE, return_value="cuda"), patch(_VALIDATE, return_value=(True, True, [])):
            labels = installer._probe_and_configure_device(GpuType.CUDA)

        assert labels == ["CUDA"]
        assert _device(toml_file) == "auto"

    @pytest.mark.parametrize("probe", [(False, True, []), (True, False, []), (False, False, ["x"])])
    def test_a_failing_model_writes_cpu_with_the_warning(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str], probe: tuple[bool, bool, list[str]]
    ) -> None:
        toml_file = _make_toml(tmp_path, '[database]\ndevice = "auto"\n')
        installer = create_installer(config_file=str(toml_file))

        with patch(_RESOLVE, return_value="mps"), patch(_VALIDATE, return_value=probe):
            labels = installer._probe_and_configure_device(GpuType.METAL)

        assert labels == ["CPU"]
        assert _device(toml_file) == "cpu"
        assert capsys.readouterr().out == "Warning: Apple GPU validation failed — falling back to CPU.\n"

    def test_auto_resolving_to_cpu_means_the_build_lacks_the_accelerator_and_probes_nothing(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        toml_file = _make_toml(tmp_path)
        installer = create_installer(config_file=str(toml_file))

        with patch(_RESOLVE, return_value="cpu"), patch(_VALIDATE) as validate:
            labels = installer._probe_and_configure_device(GpuType.METAL)

        assert labels == ["CPU"]
        assert _device(toml_file) == "cpu"
        validate.assert_not_called()
        assert "Warning: Apple GPU validation failed — falling back to CPU." in capsys.readouterr().out

    def test_a_probe_that_raises_writes_cpu(self, tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
        toml_file = _make_toml(tmp_path)
        installer = create_installer(config_file=str(toml_file))

        with patch(_RESOLVE, return_value="mps"), patch(_VALIDATE, side_effect=RuntimeError("boom")), caplog.at_level(logging.WARNING):
            labels = installer._probe_and_configure_device(GpuType.METAL)

        assert labels == ["CPU"]
        assert _device(toml_file) == "cpu"
        assert "device validation failed: boom" in caplog.text

    def test_the_probe_warnings_are_logged(self, tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
        installer = create_installer(config_file=str(_make_toml(tmp_path)))

        with (
            patch(_RESOLVE, return_value="mps"),
            patch(_VALIDATE, return_value=(True, True, ["reranker probe failed"])),
            caplog.at_level(logging.WARNING),
        ):
            installer._probe_and_configure_device(GpuType.METAL)

        assert "device validation: reranker probe failed" in caplog.text

    def test_cuda_failure_names_the_reason_and_points_at_the_install_guide(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
        caplog: pytest.LogCaptureFixture,
        off_swap_platform: None,
    ) -> None:
        toml_file = _make_toml(tmp_path)
        installer = create_installer(config_file=str(toml_file))

        with (
            patch(_RESOLVE, return_value="cpu"),
            caplog.at_level(logging.WARNING, logger="archon_search.install.installer"),
        ):
            labels = installer._probe_and_configure_device(GpuType.CUDA)

        assert labels == ["CPU"]
        assert _device(toml_file) == "cpu"
        assert capsys.readouterr().out.splitlines() == [
            "Warning: CUDA validation failed — falling back to CPU.",
            ("  Text search and result ranking will run on CPU: PyTorch found no usable CUDA device "
            "(a CPU-only PyTorch build, or no compatible NVIDIA driver), or a model failed its check."),
            ("  To run them on the GPU, install a CUDA build of PyTorch and an NVIDIA driver it supports, "
            "then re-run the wizard."),
            f"  Commands for each install method: {_CUDA_GUIDE_URL}",
        ]
        assert any(
            r.levelname == "WARNING" and r.getMessage() == "CUDA validation failed — falling back to CPU"
            for r in caplog.records
        )

    def test_dry_run_probes_nothing_writes_nothing_and_narrates(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        toml_file = _make_toml(tmp_path)
        before = toml_file.read_text()
        installer = create_installer(config_file=str(toml_file), dry_run=True)

        with patch(_VALIDATE) as validate, patch(_RESOLVE) as resolve:
            labels = installer._probe_and_configure_device(GpuType.METAL)

        assert labels == ["Apple GPU (mps)"]
        validate.assert_not_called()
        resolve.assert_not_called()
        assert toml_file.read_text() == before
        assert capsys.readouterr().out == "[DRY RUN] Would set [database] device = 'auto'.\n"

    def test_dry_run_without_a_gpu_narrates_the_cpu_write(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        installer = create_installer(config_file=str(_make_toml(tmp_path)), dry_run=True)

        assert installer._probe_and_configure_device(GpuType.NONE) == ["CPU"]
        assert capsys.readouterr().out == "[DRY RUN] Would set [database] device = 'cpu'.\n"
