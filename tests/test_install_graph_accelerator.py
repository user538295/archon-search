"""Tests for the graph-model accelerator offer and the generalised
``configure_providers`` (Task FE-5, scenarios S31/S32).

The offer is shown only after BOTH probe stages validate the accelerator —
stage 1 (`probe_device_availability`, free, pre-download) and stage 2
(`probe_device_validation`, riding the optional pre-warm). Pre-warm not having
run, or either stage failing, shows no prompt at all and writes CPU silently.
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import click
import pytest
import tomlkit

from archon_search.install import create_installer
from archon_search.install.installer import RealInstaller
from archon_search.install.provisioning import (
    TORCH_DEVICE_CPU,
    TORCH_DEVICE_CUDA,
    TORCH_DEVICE_MPS,
)
from archon_search.install.wizard import (
    _graph_candidate_providers,
    _prompt_graph_accelerator,
)
from archon_search.platform.types import GpuType

_CUDA = "CUDAExecutionProvider"
_COREML = "CoreMLExecutionProvider"


def _make_config(tmp_path: Path) -> Path:
    toml_file = tmp_path / "archon-search.toml"
    toml_file.write_text('[server]\nhost = "127.0.0.1"\n', encoding="utf-8")
    return toml_file


def _graph_providers(toml_file: Path) -> object:
    return tomlkit.parse(toml_file.read_text())["graph"]["providers"]


# ---------------------------------------------------------------------------
# The offer itself
# ---------------------------------------------------------------------------


def test_offer_appears_only_after_pre_warm_validates_both_stages(tmp_path: Path) -> None:
    """Only a stage-1 available + stage-2 pre-warm-validated accelerator is offered,
    and a bare Enter accepts it (the accelerator is the default)."""
    toml_file = _make_config(tmp_path)
    installer = create_installer(config_file=str(toml_file))

    with patch("builtins.input", return_value="") as prompt:
        installer._configure_graph_providers(
            candidates=[_CUDA],
            availability=TORCH_DEVICE_CUDA,
            prewarm_device=TORCH_DEVICE_CUDA,
            non_interactive=False,
        )

    assert prompt.call_count == 1, "the operator must be asked once, after both stages"
    assert _graph_providers(toml_file) == [_CUDA], "bare Enter must accept the accelerator"


def test_offer_is_not_shown_when_only_stage_one_passes(tmp_path: Path) -> None:
    """Stage 1 available but stage 2 never validated (pre-warm returned nothing):
    no prompt — stage 1 alone never earns the offer."""
    toml_file = _make_config(tmp_path)
    installer = create_installer(config_file=str(toml_file))

    with patch("builtins.input", side_effect=AssertionError("must not prompt")):
        installer._configure_graph_providers(
            candidates=[_COREML],
            availability=TORCH_DEVICE_MPS,
            prewarm_device=None,
            non_interactive=False,
        )

    assert _graph_providers(toml_file) == []


def test_nothing_requested_and_no_answer_never_overwrites_a_hand_set_list(tmp_path: Path) -> None:
    """"No answer yet" is not evidence: an operator's own [graph].providers survives."""
    toml_file = _make_config(tmp_path)
    toml_file.write_text(f'[graph]\nproviders = ["{_CUDA}"]\n', encoding="utf-8")
    installer = create_installer(config_file=str(toml_file))

    installer._configure_graph_providers(
        candidates=[], availability=TORCH_DEVICE_CPU,
        prewarm_device=None, non_interactive=True,
    )

    assert _graph_providers(toml_file) == [_CUDA]


def test_a_proven_stage_two_failure_does_overwrite_a_stale_accelerator(tmp_path: Path) -> None:
    """A validated failure IS evidence — the stale accelerator is stepped down to CPU."""
    toml_file = _make_config(tmp_path)
    toml_file.write_text(f'[graph]\nproviders = ["{_CUDA}"]\n', encoding="utf-8")
    installer = create_installer(config_file=str(toml_file))

    installer._configure_graph_providers(
        candidates=[_CUDA], availability=TORCH_DEVICE_CUDA,
        prewarm_device=TORCH_DEVICE_CPU, non_interactive=True,
    )

    assert _graph_providers(toml_file) == []


def test_declining_the_offer_writes_cpu(tmp_path: Path) -> None:
    """Stepping down to CPU writes CPU, not the accelerator."""
    toml_file = _make_config(tmp_path)
    installer = create_installer(config_file=str(toml_file))

    with patch("builtins.input", return_value="n"):
        installer._configure_graph_providers(
            candidates=[_COREML],
            availability=TORCH_DEVICE_MPS,
            prewarm_device=TORCH_DEVICE_MPS,
            non_interactive=False,
        )

    assert _graph_providers(toml_file) == []


@pytest.mark.parametrize(
    ("candidates", "availability", "prewarm_device", "writes_empty", "why"),
    [
        ([_CUDA], TORCH_DEVICE_CUDA, None, True, "pre-warm produced nothing for a requested GPU"),
        ([_CUDA], TORCH_DEVICE_CUDA, TORCH_DEVICE_CPU, True, "stage 2 stepped down to CPU"),
        ([_CUDA], TORCH_DEVICE_CPU, TORCH_DEVICE_CUDA, True, "stage 1 found no accelerator"),
        ([], TORCH_DEVICE_CPU, TORCH_DEVICE_CPU, True, "nothing was ever requested"),
        ([], TORCH_DEVICE_CPU, None, False, "pre-warm never ran — unset already means CPU"),
    ],
)
def test_pre_warm_not_run_or_either_stage_failing_shows_no_prompt_and_writes_cpu(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    candidates: list[str],
    availability: str,
    prewarm_device: str | None,
    writes_empty: bool,
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
        installer._configure_graph_providers(
            candidates=candidates,
            availability=availability,
            prewarm_device=prewarm_device,
            non_interactive=False,
        )

    doc = tomlkit.parse(toml_file.read_text())
    if writes_empty:
        assert doc["graph"]["providers"] == [], why
    else:
        assert "graph" not in doc, why
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == ""


def test_non_interactive_never_prompts_but_still_gates_on_validation(tmp_path: Path) -> None:
    """--graph-providers is the answer non-interactively; the same gate still decides."""
    toml_file = _make_config(tmp_path)
    installer = create_installer(config_file=str(toml_file))

    with patch("builtins.input", side_effect=AssertionError("must not prompt")):
        installer._configure_graph_providers(
            candidates=[_CUDA],
            availability=TORCH_DEVICE_CUDA,
            prewarm_device=TORCH_DEVICE_CUDA,
            non_interactive=True,
        )
    assert _graph_providers(toml_file) == [_CUDA]

    with patch("builtins.input", side_effect=AssertionError("must not prompt")):
        installer._configure_graph_providers(
            candidates=[_CUDA],
            availability=TORCH_DEVICE_CUDA,
            prewarm_device=TORCH_DEVICE_CPU,
            non_interactive=True,
        )
    assert _graph_providers(toml_file) == []


# ---------------------------------------------------------------------------
# The generalised configure_providers
# ---------------------------------------------------------------------------


def test_configure_providers_writes_the_named_section(tmp_path: Path) -> None:
    """The section and provider-list parameters are real, not reused: a non-[database]
    section with an explicitly supplied list actually lands in the file."""
    toml_file = _make_config(tmp_path)
    installer = create_installer(config_file=str(toml_file))

    installer.configure_providers(GpuType.NONE, section="graph", providers=[_COREML])

    doc = tomlkit.parse(toml_file.read_text())
    assert doc["graph"]["providers"] == [_COREML], (
        "GpuType.NONE must not short-circuit an explicit provider list"
    )
    assert "providers" not in doc.get("database", {}), "[database] must be untouched"


def test_configure_providers_explicit_empty_list_writes_cpu(tmp_path: Path) -> None:
    """The explicit CPU-write branch: an empty list is written, not skipped."""
    toml_file = _make_config(tmp_path)
    installer = create_installer(config_file=str(toml_file))

    installer.configure_providers(GpuType.CUDA, section="graph", providers=[])

    doc = tomlkit.parse(toml_file.read_text())
    assert doc["graph"]["providers"] == []


def test_configure_providers_legacy_gpu_path_is_unchanged(tmp_path: Path) -> None:
    """Callers passing only a GpuType keep today's [database] behaviour."""
    toml_file = _make_config(tmp_path)
    installer = create_installer(config_file=str(toml_file))

    installer.configure_providers(GpuType.METAL)

    doc = tomlkit.parse(toml_file.read_text())
    assert doc["database"]["providers"] == [_COREML]
    assert "graph" not in doc


def test_configure_providers_missing_config_skips_the_named_section(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """No config file: warn and return, never create one."""
    missing = tmp_path / "absent.toml"
    installer = RealInstaller(config_file=str(missing))

    with caplog.at_level("WARNING", logger="archon_search.install.installer"):
        installer.configure_providers(section="graph", providers=[_CUDA])

    assert not missing.exists()
    assert any("not found" in r.message for r in caplog.records), caplog.records


def test_configure_providers_skips_a_section_that_is_not_a_table(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A hand-edited scalar where a table belongs must warn, not raise TypeError."""
    toml_file = _make_config(tmp_path)
    toml_file.write_text('graph = "oops"\n', encoding="utf-8")
    installer = create_installer(config_file=str(toml_file))

    with caplog.at_level("WARNING", logger="archon_search.install.installer"):
        installer.configure_providers(section="graph", providers=[_CUDA])

    assert toml_file.read_text() == 'graph = "oops"\n'
    assert any("not a table" in r.message for r in caplog.records), caplog.records


# ---------------------------------------------------------------------------
# Candidate derivation + the prompt helper
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("gpu", "enable_gpu", "flag", "non_interactive", "expected"),
    [
        (GpuType.CUDA, True, None, False, [_CUDA]),
        (GpuType.METAL, True, None, False, [_COREML]),
        (GpuType.NONE, True, None, False, []),
        (GpuType.CUDA, False, None, False, []),
        (GpuType.NONE, True, [_CUDA], False, [_CUDA]),
        (GpuType.CUDA, True, [], False, []),
        # --disable-gpu (enable_gpu=False) outranks an explicit --graph-providers.
        (GpuType.CUDA, False, [_CUDA], True, []),
        # Non-interactive without the flag never opts into an unmeasured accelerator.
        (GpuType.CUDA, True, None, True, []),
        # ...but the flag remains the non-interactive answer (Q7).
        (GpuType.CUDA, True, [_CUDA], True, [_CUDA]),
    ],
)
def test_graph_candidate_providers(
    gpu: GpuType, enable_gpu: bool, flag: list[str] | None,
    non_interactive: bool, expected: list[str],
) -> None:
    assert _graph_candidate_providers(gpu, enable_gpu, flag, non_interactive) == expected


@pytest.mark.parametrize(("answer", "accepted"), [("", True), ("y", True), ("n", False), ("no", False)])
def test_prompt_graph_accelerator_defaults_to_the_accelerator(answer: str, accepted: bool) -> None:
    with patch("builtins.input", return_value=answer):
        assert _prompt_graph_accelerator(False, TORCH_DEVICE_CUDA) is accepted


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
         patch.object(prewarm, "resolve_torch_device", return_value=TORCH_DEVICE_CUDA):
        device = prewarm._prewarm_graph_model([_CUDA])

    model.to.assert_called_once_with(TORCH_DEVICE_CUDA)
    assert device == TORCH_DEVICE_CUDA


def test_prewarm_models_forwards_graph_providers() -> None:
    from archon_search.install import prewarm
    from archon_search.profiles import get_profile

    graph_mock = MagicMock(return_value=TORCH_DEVICE_MPS)
    with patch.object(prewarm, "_prewarm_graph_model", graph_mock), \
         patch.dict("sys.modules", {"fastembed": MagicMock()}):
        device = prewarm._prewarm_models(
            get_profile("minimal", False), timeout=300,
            install_graph_extra=True, graph_providers=[_COREML],
        )

    graph_mock.assert_called_once_with([_COREML])
    assert device == TORCH_DEVICE_MPS


def test_graph_providers_flag_parses_and_rejects_unknown_providers() -> None:
    from archon_search.cli.install_cmd import _validate_graph_providers

    assert _validate_graph_providers(None, None, None) is None
    assert _validate_graph_providers(None, None, "") == []
    assert _validate_graph_providers(None, None, f" {_CUDA} ") == [_CUDA]
    with pytest.raises(click.BadParameter):
        _validate_graph_providers(None, None, "rm -rf /")
