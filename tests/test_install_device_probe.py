"""Tests for the two-stage graph-model device probe (Task BE-14).

Stage 1 (`probe_device_availability`) is the free pre-download check; stage 2
(`probe_device_validation`) rides the optional pre-warm (BE-8) and reports the
device that pre-warm actually landed on — or a neutral "not yet validated"
status when pre-warm did not run. `torch`/`gliner` are mocked via sys.modules
injection because their imports are deferred inside the functions under test.
"""
from __future__ import annotations

import sys
import types
from unittest.mock import MagicMock, patch

import pytest

from archon_search.install.device_probe import (
    DeviceProbeResult,
    probe_device_availability,
    probe_device_validation,
)
from archon_search.install.prewarm import _prewarm_graph_model
from archon_search.install.provisioning import (
    DEVICE_PROBE_NOT_YET_VALIDATED,
    TORCH_DEVICE_CPU,
    TORCH_DEVICE_CUDA,
    TORCH_DEVICE_MPS,
    ProvisionFailureKind,
)
from archon_search.torch_device import resolve_torch_device


def _fake_torch(cuda_available: bool = False, mps_available: bool = False) -> types.ModuleType:
    """Build a fake `torch` exposing only the availability checks the probe reads."""
    torch_mod = types.ModuleType("torch")
    torch_mod.cuda = types.SimpleNamespace(  # type: ignore[attr-defined]
        is_available=MagicMock(return_value=cuda_available)
    )
    torch_mod.backends = types.SimpleNamespace(  # type: ignore[attr-defined]
        mps=types.SimpleNamespace(is_available=MagicMock(return_value=mps_available))
    )
    return torch_mod


# ---------------------------------------------------------------------------
# #unit_test — a configured device torch cannot actually use is never silently
# downgraded: stage 2 reports its own failure category, not a CPU pretending
# to be the requested accelerator.
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_unavailable_torch_device_reports_its_own_category() -> None:
    # The REAL unavailable path: CUDA is configured and pre-warm produced
    # nothing (None). The probe keys off configured INTENT (requests_accelerator),
    # not host availability, so it must NOT read a missing device as "nothing
    # configured" and return a benign not_validated — it must report a failure.
    unavailable = probe_device_validation(["CUDAExecutionProvider"], None)
    assert unavailable.failure is ProvisionFailureKind.conflicting_onnx_runtimes
    assert unavailable.device is None  # never a silent "cpu"
    assert unavailable.not_yet_validated is False

    # Pre-warm ran but stepped the CUDA request down to CPU — a real
    # unavailable-device outcome, reported as its own category.
    stepped_down = probe_device_validation(["CUDAExecutionProvider"], TORCH_DEVICE_CPU)
    assert stepped_down.failure is ProvisionFailureKind.conflicting_onnx_runtimes
    assert stepped_down.device is None  # never a silent "cpu"
    assert stepped_down.not_yet_validated is False

    # A configured accelerator with nothing to show from pre-warm (None)
    # is a failure, not a benign "not yet validated".
    nothing = probe_device_validation(["CoreMLExecutionProvider"], None)
    assert nothing.failure is ProvisionFailureKind.conflicting_onnx_runtimes
    assert nothing.device is None


# ---------------------------------------------------------------------------
# #unit_test — the resolved device carries the real answer: a step-down to CPU
# is what `resolve_torch_device` returns, not the configured intent inferred
# back.
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_step_down_to_cpu_is_reported_not_inferred(caplog) -> None:
    with patch.dict(sys.modules, {"torch": _fake_torch(cuda_available=False)}):
        with caplog.at_level("WARNING"):
            resolved = resolve_torch_device(["CUDAExecutionProvider"])
    assert resolved == TORCH_DEVICE_CPU  # the real runtime answer, not "cuda"
    assert any("CUDA" in r.message for r in caplog.records)

    with patch.dict(sys.modules, {"torch": _fake_torch(cuda_available=True)}):
        assert resolve_torch_device(["CUDAExecutionProvider"]) == TORCH_DEVICE_CUDA
    with patch.dict(sys.modules, {"torch": _fake_torch(mps_available=True)}):
        assert resolve_torch_device(["CoreMLExecutionProvider"]) == TORCH_DEVICE_MPS


# ---------------------------------------------------------------------------
# #unit_test — pre-warm not run is "not yet validated", distinct from an
# unavailable-device failure.
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_prewarm_not_run_reports_not_yet_validated_not_a_failure() -> None:
    # No non-CPU device configured (providers=None) and pre-warm never ran:
    # nothing to validate, nothing to protect — neutral, not a failure.
    result = probe_device_validation(None, None)
    assert result.not_yet_validated is True
    assert result.failure is None
    assert result.device == DEVICE_PROBE_NOT_YET_VALIDATED
    # Stage 1 agrees the request was plain CPU.
    assert probe_device_availability(None) == TORCH_DEVICE_CPU


# ---------------------------------------------------------------------------
# #unit_test — the accelerator-SUCCESS happy path: a device BOTH configured AND
# validated by pre-warm falls through to a resolved result, not a failure.
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_configured_and_validated_accelerator_reports_resolved() -> None:
    cuda = probe_device_validation(["CUDAExecutionProvider"], TORCH_DEVICE_CUDA)
    assert cuda == DeviceProbeResult.resolved(TORCH_DEVICE_CUDA)

    mps = probe_device_validation(["CoreMLExecutionProvider"], TORCH_DEVICE_MPS)
    assert mps == DeviceProbeResult.resolved(TORCH_DEVICE_MPS)


# ---------------------------------------------------------------------------
# #unit_test — resolve_torch_device honors its "never fails" contract: a CUDA
# provider configured while torch is unimportable steps down to CPU, not raise.
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_resolve_torch_device_returns_cpu_when_torch_absent(caplog) -> None:
    # sys.modules["torch"] = None makes `import torch` raise ImportError — the
    # real graph-extra-absent condition reachable from the stage-1 probe.
    with patch.dict(sys.modules, {"torch": None}):
        with caplog.at_level("WARNING"):
            resolved = resolve_torch_device(["CUDAExecutionProvider"])
    assert resolved == TORCH_DEVICE_CPU
    assert any("torch is not installed" in r.getMessage() for r in caplog.records)


# ---------------------------------------------------------------------------
# #unit_test — DeviceProbeResult rejects contradictory tri-state construction.
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_contradictory_device_probe_result_raises() -> None:
    with pytest.raises(ValueError):
        DeviceProbeResult(
            device=TORCH_DEVICE_CUDA,
            failure=ProvisionFailureKind.conflicting_onnx_runtimes,
            not_yet_validated=True,
        )


# ---------------------------------------------------------------------------
# #integration_test — stage 2 validates against a real pre-warmed load, not
# against host availability alone: the device is read off the model the
# pre-warm actually produced.
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_stage_two_validates_against_a_real_pre_warmed_load() -> None:
    # A fake gliner whose loaded model reports CUDA — a device this host cannot
    # provide, proving the answer comes from the pre-warmed load, not the host.
    fake_model = types.SimpleNamespace(device=types.SimpleNamespace(type=TORCH_DEVICE_CUDA))
    gliner_cls = MagicMock()
    gliner_cls.from_pretrained.return_value = fake_model
    gliner_mod = types.ModuleType("gliner")
    gliner_mod.GLiNER = gliner_cls  # type: ignore[attr-defined]

    with patch.dict(sys.modules, {"gliner": gliner_mod}):
        prewarmed_device = _prewarm_graph_model()

    assert prewarmed_device == TORCH_DEVICE_CUDA
    result = probe_device_validation(None, prewarmed_device)
    assert result == DeviceProbeResult.resolved(TORCH_DEVICE_CUDA)
