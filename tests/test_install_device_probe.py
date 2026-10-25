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

from archon_search import torch_runtime
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


@pytest.fixture(autouse=True)
def _fresh_step_down_warnings(monkeypatch: pytest.MonkeyPatch) -> None:
    """resolve_device warns once per process per setting; each test starts un-warned."""
    monkeypatch.setattr(torch_runtime, "_warned_settings", set())


# ---------------------------------------------------------------------------
# #unit_test — a configured device torch cannot actually use is never silently
# downgraded: stage 2 reports its own failure category, not a CPU pretending
# to be the requested accelerator.
# ---------------------------------------------------------------------------


def test_device_unavailable_failure_kind_value() -> None:
    assert ProvisionFailureKind.device_unavailable.value == "device_unavailable"


@pytest.mark.unit
@pytest.mark.parametrize(
    ("device", "prewarm_device"),
    [
        # The REAL unavailable path: an accelerator is configured and pre-warm produced
        # nothing (None). The probe keys off configured INTENT, not host availability,
        # so it must NOT read a missing device as "nothing configured".
        ("cuda", None),
        ("mps", None),
        ("auto", None),
        # Pre-warm ran but stepped the accelerator request down to CPU.
        ("cuda", TORCH_DEVICE_CPU),
        ("mps", TORCH_DEVICE_CPU),
    ],
)
def test_unavailable_torch_device_reports_its_own_category(
    device: str, prewarm_device: str | None
) -> None:
    result = probe_device_validation(device, prewarm_device)
    assert result.failure is ProvisionFailureKind.device_unavailable
    assert result.device is None  # never a silent "cpu"
    assert result.not_yet_validated is False


# ---------------------------------------------------------------------------
# #unit_test — the resolved device carries the real answer: a step-down to CPU
# is what `resolve_device` returns, not the configured intent inferred back.
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_step_down_to_cpu_is_reported_not_inferred(caplog: pytest.LogCaptureFixture) -> None:
    with patch.dict(sys.modules, {"torch": _fake_torch(cuda_available=False)}):
        with caplog.at_level("WARNING"):
            resolved = probe_device_availability("cuda")
    assert resolved == TORCH_DEVICE_CPU  # the real runtime answer, not "cuda"
    assert any("'cuda' is not available" in r.getMessage() for r in caplog.records)

    with patch.dict(sys.modules, {"torch": _fake_torch(cuda_available=True)}):
        assert probe_device_availability("cuda") == TORCH_DEVICE_CUDA
    with patch.dict(sys.modules, {"torch": _fake_torch(mps_available=True)}):
        assert probe_device_availability("mps") == TORCH_DEVICE_MPS


@pytest.mark.unit
@pytest.mark.parametrize(
    ("torch_kwargs", "expected"),
    [
        ({"cuda_available": True, "mps_available": True}, TORCH_DEVICE_CUDA),
        ({"mps_available": True}, TORCH_DEVICE_MPS),
        ({}, TORCH_DEVICE_CPU),
    ],
    ids=["cuda-wins", "mps", "cpu-only"],
)
def test_auto_resolves_to_the_best_available_device(
    torch_kwargs: dict[str, bool], expected: str
) -> None:
    with patch.dict(sys.modules, {"torch": _fake_torch(**torch_kwargs)}):
        assert probe_device_availability("auto") == expected


@pytest.mark.unit
def test_cpu_never_touches_torch() -> None:
    with patch.dict(sys.modules, {"torch": None}):
        assert probe_device_availability("cpu") == TORCH_DEVICE_CPU


@pytest.mark.unit
def test_unknown_device_is_rejected() -> None:
    with pytest.raises(ValueError, match="unknown device 'tpu'"):
        probe_device_availability("tpu")


# ---------------------------------------------------------------------------
# #unit_test — pre-warm not run is "not yet validated", distinct from an
# unavailable-device failure.
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_prewarm_not_run_reports_not_yet_validated_not_a_failure() -> None:
    # Plain CPU configured and pre-warm never ran: nothing to validate, nothing to
    # protect — neutral, not a failure.
    result = probe_device_validation("cpu", None)
    assert result.not_yet_validated is True
    assert result.failure is None
    assert result.device == DEVICE_PROBE_NOT_YET_VALIDATED
    # Stage 1 agrees the request was plain CPU.
    assert probe_device_availability("cpu") == TORCH_DEVICE_CPU


# ---------------------------------------------------------------------------
# #unit_test — the accelerator-SUCCESS happy path: a device BOTH configured AND
# validated by pre-warm falls through to a resolved result, not a failure.
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_configured_and_validated_accelerator_reports_resolved() -> None:
    cuda = probe_device_validation("cuda", TORCH_DEVICE_CUDA)
    assert cuda == DeviceProbeResult.resolved(TORCH_DEVICE_CUDA)

    mps = probe_device_validation("mps", TORCH_DEVICE_MPS)
    assert mps == DeviceProbeResult.resolved(TORCH_DEVICE_MPS)

    auto = probe_device_validation("auto", TORCH_DEVICE_MPS)
    assert auto == DeviceProbeResult.resolved(TORCH_DEVICE_MPS)


# ---------------------------------------------------------------------------
# #unit_test — DeviceProbeResult rejects contradictory tri-state construction.
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_contradictory_device_probe_result_raises() -> None:
    with pytest.raises(ValueError):
        DeviceProbeResult(
            device=TORCH_DEVICE_CUDA,
            failure=ProvisionFailureKind.device_unavailable,
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
    # FE-5 places the model before reading its device; .to() returns the model.
    placements: list[str] = []
    fake_model.to = lambda device: (placements.append(device), fake_model)[1]  # type: ignore[attr-defined]
    gliner_cls = MagicMock()
    gliner_cls.from_pretrained.return_value = fake_model
    gliner_mod = types.ModuleType("gliner")
    gliner_mod.GLiNER = gliner_cls  # type: ignore[attr-defined]

    with patch.dict(sys.modules, {"gliner": gliner_mod}):
        prewarmed_device = _prewarm_graph_model()

    assert placements == [TORCH_DEVICE_CPU], "the default device must place the model on CPU"
    assert prewarmed_device == TORCH_DEVICE_CUDA
    result = probe_device_validation("cpu", prewarmed_device)
    assert result == DeviceProbeResult.resolved(TORCH_DEVICE_CUDA)
