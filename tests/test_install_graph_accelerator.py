"""Tests for the graph-model accelerator offer and ``configure_device`` on the
``[graph]`` section (Task FE-5, scenarios S31/S32).

The offer is shown only after BOTH probe stages validate the accelerator —
stage 1 (`probe_device_availability`, free, pre-download) and stage 2
(`probe_device_validation`, riding the optional pre-warm). Pre-warm not having
run, or either stage failing, shows no prompt at all and writes CPU silently.
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import tomlkit
from click.testing import CliRunner

from archon_search.cli.install_cmd import wizard
from archon_search.install import create_installer
from archon_search.install.installer import RealInstaller
from archon_search.install.provisioning import (
    TORCH_DEVICE_CPU,
    TORCH_DEVICE_CUDA,
    TORCH_DEVICE_MPS,
)
from archon_search.install.wizard import (
    _graph_candidate_device,
    _prompt_graph_accelerator,
)
from archon_search.platform.types import GpuType


def _make_config(tmp_path: Path, extra: str = "") -> Path:
    toml_file = tmp_path / "archon-search.toml"
    toml_file.write_text('[server]\nhost = "127.0.0.1"\n' + extra, encoding="utf-8")
    return toml_file


def _graph_device(toml_file: Path) -> object:
    return tomlkit.parse(toml_file.read_text())["graph"]["device"]


# ---------------------------------------------------------------------------
# The offer itself
# ---------------------------------------------------------------------------


def test_offer_appears_only_after_pre_warm_validates_both_stages(tmp_path: Path) -> None:
    """Only a stage-1 available + stage-2 pre-warm-validated accelerator is offered,
    and answering y accepts it."""
    toml_file = _make_config(tmp_path)
    installer = create_installer(config_file=str(toml_file))

    with patch("builtins.input", return_value="y") as prompt:
        installer._configure_graph_device(
            candidate=TORCH_DEVICE_CUDA,
            availability=TORCH_DEVICE_CUDA,
            prewarm_device=TORCH_DEVICE_CUDA,
            non_interactive=False,
        )

    assert prompt.call_count == 1, "the operator must be asked once, after both stages"
    assert _graph_device(toml_file) == "cuda", "y must accept the accelerator"


def test_offer_is_not_shown_when_only_stage_one_passes(tmp_path: Path) -> None:
    """Stage 1 available but stage 2 never validated (pre-warm returned nothing):
    no prompt — stage 1 alone never earns the offer — and cpu is written."""
    toml_file = _make_config(tmp_path)
    installer = create_installer(config_file=str(toml_file))

    with patch("builtins.input", side_effect=AssertionError("must not prompt")):
        installer._configure_graph_device(
            candidate=TORCH_DEVICE_MPS,
            availability=TORCH_DEVICE_MPS,
            prewarm_device=None,
            non_interactive=False,
        )

    assert _graph_device(toml_file) == "cpu"


def test_nothing_requested_and_no_answer_never_overwrites_a_hand_set_device(tmp_path: Path) -> None:
    """"No answer yet" is not evidence: an operator's own [graph].device survives."""
    toml_file = _make_config(tmp_path, '[graph]\ndevice = "cuda"\n')
    installer = create_installer(config_file=str(toml_file))

    installer._configure_graph_device(
        candidate=TORCH_DEVICE_CPU, availability=TORCH_DEVICE_CPU,
        prewarm_device=None, non_interactive=True,
    )

    assert _graph_device(toml_file) == "cuda"


def test_a_proven_stage_two_failure_does_overwrite_a_stale_accelerator(tmp_path: Path) -> None:
    """A validated failure IS evidence — the stale accelerator is stepped down to CPU."""
    toml_file = _make_config(tmp_path, '[graph]\ndevice = "cuda"\n')
    installer = create_installer(config_file=str(toml_file))

    installer._configure_graph_device(
        candidate=TORCH_DEVICE_CUDA, availability=TORCH_DEVICE_CUDA,
        prewarm_device=TORCH_DEVICE_CPU, non_interactive=True,
    )

    assert _graph_device(toml_file) == "cpu"


def test_declining_the_offer_writes_cpu(tmp_path: Path) -> None:
    """Stepping down to CPU writes CPU, not the accelerator."""
    toml_file = _make_config(tmp_path)
    installer = create_installer(config_file=str(toml_file))

    with patch("builtins.input", return_value="n"):
        installer._configure_graph_device(
            candidate=TORCH_DEVICE_MPS,
            availability=TORCH_DEVICE_MPS,
            prewarm_device=TORCH_DEVICE_MPS,
            non_interactive=False,
        )

    assert _graph_device(toml_file) == "cpu"


@pytest.mark.parametrize(
    ("candidate", "availability", "prewarm_device", "writes_cpu", "why"),
    [
        (TORCH_DEVICE_CUDA, TORCH_DEVICE_CUDA, None, True, "pre-warm produced nothing for a requested GPU"),
        (TORCH_DEVICE_CUDA, TORCH_DEVICE_CUDA, TORCH_DEVICE_CPU, True, "stage 2 stepped down to CPU"),
        (TORCH_DEVICE_CUDA, TORCH_DEVICE_CPU, TORCH_DEVICE_CUDA, True, "stage 1 found no accelerator"),
        (TORCH_DEVICE_CPU, TORCH_DEVICE_CPU, TORCH_DEVICE_CPU, True, "nothing was ever requested"),
        (TORCH_DEVICE_CPU, TORCH_DEVICE_CPU, None, False, "pre-warm never ran — unset already means CPU"),
    ],
)
def test_pre_warm_not_run_or_either_stage_failing_shows_no_prompt_and_writes_cpu(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    candidate: str,
    availability: str,
    prewarm_device: str | None,
    writes_cpu: bool,
    why: str,
) -> None:
    """No prompt, CPU settled, and the failure never surfaces as an exception message.

    ``input`` raises rather than returning a canned answer, so an implementation that
    prompted on any of these paths would fail here instead of passing silently.
    """
    toml_file = _make_config(tmp_path)
    installer = create_installer(config_file=str(toml_file))
    capsys.readouterr()

    with patch("builtins.input", side_effect=AssertionError("must not prompt")):
        installer._configure_graph_device(
            candidate=candidate,
            availability=availability,
            prewarm_device=prewarm_device,
            non_interactive=False,
        )

    doc = tomlkit.parse(toml_file.read_text())
    if writes_cpu:
        assert doc["graph"]["device"] == "cpu", why
    else:
        assert "graph" not in doc, why
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == ""


def test_non_interactive_never_prompts_but_still_gates_on_validation(tmp_path: Path) -> None:
    """--graph-device is the answer non-interactively; the same gate still decides."""
    toml_file = _make_config(tmp_path)
    installer = create_installer(config_file=str(toml_file))

    with patch("builtins.input", side_effect=AssertionError("must not prompt")):
        installer._configure_graph_device(
            candidate=TORCH_DEVICE_CUDA,
            availability=TORCH_DEVICE_CUDA,
            prewarm_device=TORCH_DEVICE_CUDA,
            non_interactive=True,
        )
    assert _graph_device(toml_file) == "cuda"

    with patch("builtins.input", side_effect=AssertionError("must not prompt")):
        installer._configure_graph_device(
            candidate=TORCH_DEVICE_CUDA,
            availability=TORCH_DEVICE_CUDA,
            prewarm_device=TORCH_DEVICE_CPU,
            non_interactive=True,
        )
    assert _graph_device(toml_file) == "cpu"


# ---------------------------------------------------------------------------
# configure_device on a named section
# ---------------------------------------------------------------------------


def test_configure_device_writes_the_named_section(tmp_path: Path) -> None:
    """The section parameter is real, not reused: a non-[database] section lands in the file."""
    toml_file = _make_config(tmp_path)
    installer = create_installer(config_file=str(toml_file))

    installer.configure_device("mps", section="graph")

    doc = tomlkit.parse(toml_file.read_text())
    assert doc["graph"]["device"] == "mps"
    assert "device" not in doc.get("database", {}), "[database] must be untouched"


def test_configure_device_defaults_to_the_database_section(tmp_path: Path) -> None:
    toml_file = _make_config(tmp_path)
    installer = create_installer(config_file=str(toml_file))

    installer.configure_device("cpu")

    doc = tomlkit.parse(toml_file.read_text())
    assert doc["database"]["device"] == "cpu"
    assert "graph" not in doc


def test_configure_device_rejects_an_unknown_device(tmp_path: Path) -> None:
    toml_file = _make_config(tmp_path)
    installer = create_installer(config_file=str(toml_file))

    with pytest.raises(ValueError, match="tpu"):
        installer.configure_device("tpu", section="graph")

    assert "graph" not in tomlkit.parse(toml_file.read_text())


def test_configure_device_missing_config_skips_the_named_section(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """No config file: warn and return, never create one."""
    missing = tmp_path / "absent.toml"
    installer = RealInstaller(config_file=str(missing))

    with caplog.at_level("WARNING", logger="archon_search.install.installer"):
        installer.configure_device("cuda", section="graph")

    assert not missing.exists()
    assert any("not found" in r.message for r in caplog.records), caplog.records


def test_configure_device_skips_a_section_that_is_not_a_table(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A hand-edited scalar where a table belongs must warn, not raise TypeError."""
    toml_file = _make_config(tmp_path)
    toml_file.write_text('graph = "oops"\n', encoding="utf-8")
    installer = create_installer(config_file=str(toml_file))

    with caplog.at_level("WARNING", logger="archon_search.install.installer"):
        installer.configure_device("cuda", section="graph")

    assert toml_file.read_text() == 'graph = "oops"\n'
    assert any("not a table" in r.message for r in caplog.records), caplog.records


# ---------------------------------------------------------------------------
# Candidate derivation + the prompt helper
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("gpu", "enable_gpu", "flag", "non_interactive", "expected"),
    [
        (GpuType.CUDA, True, None, False, "cuda"),
        (GpuType.METAL, True, None, False, "mps"),
        (GpuType.NONE, True, None, False, "cpu"),
        (GpuType.CUDA, False, None, False, "cpu"),
        (GpuType.NONE, True, "cuda", False, "cuda"),
        (GpuType.CUDA, True, "cpu", False, "cpu"),
        (GpuType.NONE, True, "auto", False, "auto"),
        # --disable-gpu (enable_gpu=False) outranks an explicit --graph-device.
        (GpuType.CUDA, False, "cuda", True, "cpu"),
        # Non-interactive without the flag never opts into an unmeasured accelerator.
        (GpuType.CUDA, True, None, True, "cpu"),
        # ...but the flag remains the non-interactive answer (Q7).
        (GpuType.CUDA, True, "cuda", True, "cuda"),
    ],
)
def test_graph_candidate_device(
    gpu: GpuType, enable_gpu: bool, flag: str | None,
    non_interactive: bool, expected: str,
) -> None:
    assert _graph_candidate_device(gpu, enable_gpu, flag, non_interactive) == expected


@pytest.mark.parametrize(("answer", "accepted"), [("y", True), ("Y", True), ("n", False), ("N", False)])
def test_prompt_graph_accelerator_accepts_only_y_or_n(answer: str, accepted: bool) -> None:
    with patch("builtins.input", return_value=answer):
        assert _prompt_graph_accelerator(False, TORCH_DEVICE_CUDA) is accepted


@pytest.mark.parametrize("invalid", ["", "no", "yes"])
def test_prompt_graph_accelerator_reasks_on_invalid_answer(invalid: str) -> None:
    """Enter / yes / no are rejected and the same question is asked again."""
    with patch("builtins.input", side_effect=[invalid, "n"]) as prompt:
        assert _prompt_graph_accelerator(False, TORCH_DEVICE_CUDA) is False
    assert prompt.call_count == 2


def test_prompt_graph_accelerator_accepts_on_eof() -> None:
    """No input (EOF) keeps the default — the accelerator."""
    with patch("builtins.input", side_effect=EOFError):
        assert _prompt_graph_accelerator(False, TORCH_DEVICE_MPS) is True


# ---------------------------------------------------------------------------
# Pre-warm placement + the CLI flag
# ---------------------------------------------------------------------------


def test_prewarm_graph_model_places_the_model_on_the_resolved_device() -> None:
    """The pre-warm must place the model where the runtime will, so the device
    stage 2 probes is the real one (mirrors _load_sync)."""
    from archon_search.install import prewarm

    placed = MagicMock()
    placed.device.type = TORCH_DEVICE_CUDA
    model = MagicMock()
    model.to.return_value = placed
    gliner_mod = MagicMock()
    gliner_mod.GLiNER.from_pretrained.return_value = model

    with patch.dict("sys.modules", {"gliner": gliner_mod}), \
         patch.object(prewarm, "resolve_device", return_value=TORCH_DEVICE_CUDA) as resolve:
        device = prewarm._prewarm_graph_model("auto")

    resolve.assert_called_once_with("auto")
    model.to.assert_called_once_with(TORCH_DEVICE_CUDA)
    assert device == TORCH_DEVICE_CUDA


def test_prewarm_models_forwards_graph_device() -> None:
    from archon_search.config import SearchConfig
    from archon_search.install import prewarm

    graph_mock = MagicMock(return_value=TORCH_DEVICE_MPS)
    with patch.object(prewarm, "_prewarm_graph_model", graph_mock), \
         patch.object(prewarm, "ensure_model"), \
         patch.object(prewarm, "ModelEmbedder"), \
         patch.object(prewarm, "ModelReranker"):
        device = prewarm._prewarm_models(
            SearchConfig(), 300.0, install_graph_extra=True, graph_device="mps",
        )

    graph_mock.assert_called_once_with("mps")
    assert device == TORCH_DEVICE_MPS


@pytest.mark.parametrize("value", ["cpu", "auto", "mps", "cuda"])
def test_graph_device_flag_accepts_each_device_and_forwards_it(value: str) -> None:
    installer = MagicMock()
    installer.run.return_value = 0
    with patch("archon_search.cli.install_cmd.create_installer", return_value=installer):
        result = CliRunner().invoke(wizard, ["--graph-device", value])

    assert result.exit_code == 0, result.output
    assert installer.run.call_args.kwargs["graph_device"] == value


def test_graph_device_flag_defaults_to_none() -> None:
    installer = MagicMock()
    installer.run.return_value = 0
    with patch("archon_search.cli.install_cmd.create_installer", return_value=installer):
        result = CliRunner().invoke(wizard, [])

    assert result.exit_code == 0, result.output
    assert installer.run.call_args.kwargs["graph_device"] is None


@pytest.mark.parametrize("value", ["CPUExecutionProvider", "rm -rf /", "tpu", ""])
def test_graph_device_flag_rejects_unknown_values(value: str) -> None:
    with patch("archon_search.cli.install_cmd.create_installer") as create:
        result = CliRunner().invoke(wizard, ["--graph-device", value])

    assert result.exit_code == 2
    assert "Invalid value for '--graph-device'" in result.output
    create.assert_not_called()


def test_graph_device_flag_help_lists_the_choices() -> None:
    result = CliRunner().invoke(wizard, ["--help"])

    assert result.exit_code == 0
    assert "--graph-device [auto|cpu|mps|cuda]" in result.output
