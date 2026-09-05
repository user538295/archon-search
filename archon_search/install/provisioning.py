"""Frozen model-artifact provisioning shapes and category constants (contract C4).

Two descriptor shapes, deliberately distinct: :class:`ArtifactSpec` for an artifact
downloaded from a pinned URL and verified against a pinned digest, and
:class:`SizeEstimateSpec` for a pip/uv-resolved extra that has neither a pinned URL
nor a stable byte count across platforms and is therefore display-only.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class LicenseDisposition(StrEnum):
    """The one license rule, applied per descriptor rather than decided per model."""

    prompt_for_acceptance = "prompt_for_acceptance"
    disclose_only = "disclose_only"


class ProvisionFailureKind(StrEnum):
    """Frozen failure categories a provisioning attempt may report.

    Frozen against C4 so the wizard can render every category before the artifacts
    that raise them exist; several members have no raiser today.
    """

    download_failed = "download_failed"
    digest_mismatch = "digest_mismatch"
    size_mismatch = "size_mismatch"
    insufficient_disk = "insufficient_disk"
    extract_failed = "extract_failed"
    smoke_load_failed = "smoke_load_failed"
    relations_not_supported = "relations_not_supported"
    conflicting_onnx_runtimes = "conflicting_onnx_runtimes"


# One MB as the size-estimate descriptors and the wizard's displayed figure both mean it
# (decimal, not MiB), so `declared_mb * BYTES_PER_MB // BYTES_PER_MB` round-trips exactly.
BYTES_PER_MB = 1_000_000

# The torch device a pre-warm probe may resolve to, plus the status it reports when the
# optional pre-warm did not run. Frozen here so the probe and its renderer share one symbol.
TORCH_DEVICE_CPU = "cpu"
TORCH_DEVICE_CUDA = "cuda"
TORCH_DEVICE_MPS = "mps"
DEVICE_PROBE_NOT_YET_VALIDATED = "not_yet_validated"


@dataclass(frozen=True)
class ArtifactSpec:
    """The pinned identity of one downloadable, digest-verified, placed artifact."""

    name: str
    url: str
    revision: str
    sha256: str
    size_bytes: int
    license: str
    license_disposition: LicenseDisposition
    attribution: str


@dataclass(frozen=True)
class SizeEstimateSpec:
    """A declared-size-only estimate: never digest-checked, never placed by this seam."""

    name: str
    declared_mb: int
    license: str | None = None
