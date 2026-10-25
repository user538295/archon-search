"""Two-stage device probe for the graph NER model (Task BE-14).

Stage 1 is a free, pre-download availability check: it reports what torch
device ``[graph].device`` *would* resolve to, without loading any model.
Stage 2 is the real validation — it rides the optional pre-warm entry (BE-8)
rather than a mandatory smoke-load, and reports the ACTUAL resolved device
that pre-warm landed on (or a neutral "not yet validated" status if pre-warm
never ran).

The wizard-facing offer built on top of this lives in ``installer.py``; this
module only produces the probe result it consumes.
"""
from __future__ import annotations

from dataclasses import dataclass

from archon_search.torch_runtime import resolve_device

from .provisioning import (
    DEVICE_PROBE_NOT_YET_VALIDATED,
    TORCH_DEVICE_CPU,
    ProvisionFailureKind,
)


def probe_device_availability(device: str) -> str:
    """Stage 1 — free pre-download check: the torch device *device* would resolve to.

    Never loads a model. *device* is a ``[graph].device`` setting
    (``cpu|auto|mps|cuda``); an accelerator this host lacks resolves to ``cpu``.
    """
    return resolve_device(device)


@dataclass(frozen=True)
class DeviceProbeResult:
    """Stage 2's outcome — exactly one of a resolved device, a failure category,
    or the neutral "not yet validated" status. FE-5 consumes this to decide
    whether/what to offer; it is not built here."""

    device: str | None
    failure: ProvisionFailureKind | None
    not_yet_validated: bool

    def __post_init__(self) -> None:
        resolved = (
            self.device is not None
            and self.device != DEVICE_PROBE_NOT_YET_VALIDATED
            and self.failure is None
            and not self.not_yet_validated
        )
        failed = (
            self.failure is not None
            and self.device is None
            and not self.not_yet_validated
        )
        not_validated = (
            self.not_yet_validated
            and self.failure is None
            and self.device == DEVICE_PROBE_NOT_YET_VALIDATED
        )
        if resolved + failed + not_validated != 1:
            raise ValueError(
                "DeviceProbeResult must hold exactly one state (resolved / failed / "
                f"not_validated), got device={self.device!r}, failure={self.failure!r}, "
                f"not_yet_validated={self.not_yet_validated!r}"
            )

    @staticmethod
    def resolved(device: str) -> DeviceProbeResult:
        return DeviceProbeResult(device=device, failure=None, not_yet_validated=False)

    @staticmethod
    def failed(kind: ProvisionFailureKind) -> DeviceProbeResult:
        return DeviceProbeResult(device=None, failure=kind, not_yet_validated=False)

    @staticmethod
    def not_validated() -> DeviceProbeResult:
        return DeviceProbeResult(
            device=DEVICE_PROBE_NOT_YET_VALIDATED, failure=None, not_yet_validated=True
        )


def probe_device_validation(
    device: str, prewarm_resolved_device: str | None
) -> DeviceProbeResult:
    """Stage 2 — real validation riding the optional pre-warm (BE-8).

    ``device`` is the ``[graph].device`` candidate (``cpu|auto|mps|cuda``).
    ``prewarm_resolved_device`` is whatever ``_prewarm_graph_model`` (via
    ``_prewarm_models``) actually returned: the real torch device it landed on,
    or ``None`` if pre-warm did not run at all, was skipped, or failed.

    - Pre-warm did not run/was skipped/failed: reports ``not_yet_validated``
      only if *device* is ``cpu`` (nothing to protect against) — but an
      accelerator requested while pre-warm produced nothing is itself treated
      as an unavailable-device failure (see below), because the
      accelerator-offer principle must never be silently skipped when a
      failure actually happened.
    - Pre-warm ran and resolved to CPU while an accelerator was requested
      (a step-down), or pre-warm failed outright while one was requested:
      reports ``ProvisionFailureKind.device_unavailable`` — never silently CPU.
    - Otherwise (pre-warm ran and resolved to whatever was actually requested,
      including plain CPU with nothing requested): reports the literal
      resolved device.
    """
    # Config INTENT, not host availability: resolve_device already steps a
    # requested-but-unavailable accelerator down to CPU, so keying off it would
    # silently treat "host lacks the device" as "nothing requested" and skip the
    # failure. The setting itself reports what was asked for.
    requested_accelerator = device != TORCH_DEVICE_CPU

    if prewarm_resolved_device is None:
        if requested_accelerator:
            return DeviceProbeResult.failed(ProvisionFailureKind.device_unavailable)
        return DeviceProbeResult.not_validated()

    if requested_accelerator and prewarm_resolved_device == TORCH_DEVICE_CPU:
        return DeviceProbeResult.failed(ProvisionFailureKind.device_unavailable)

    return DeviceProbeResult.resolved(prewarm_resolved_device)
