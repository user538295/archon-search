"""Shared torch-device resolution: map ONNX-style provider strings to a torch device.

Pure, reusable logic only — no model loading. Shared by the ingest-time runtime
resolution (``ProseExtractionBackend._resolve_device``) and the install-time
device probe (``archon_search.install.device_probe``), so the "map providers ->
torch device, check availability" computation exists in exactly one place
rather than being duplicated across the two call sites (Task BE-14).
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

# [graph].providers values are ONNX-style execution provider strings (see
# archon-search.toml.example's [database] section, which the reranker/embedder
# share) — never the literal "cuda"/"mps" torch device names.
CUDA_PROVIDER_MARKER = "cuda"
MPS_PROVIDER_MARKER = "coreml"


def requests_accelerator(providers: list[str] | None) -> bool:
    """True when *providers* names a non-CPU accelerator — config intent, not host availability.

    Pure string check over the first provider entry; never imports torch, never
    fails. Distinct from ``resolve_torch_device``, which steps a configured-but-
    unavailable accelerator down to CPU: this reports what was *asked for*, so a
    silent step-down on a host lacking the device is still detectable.
    """
    if not providers:
        return False
    first = providers[0].lower()
    return CUDA_PROVIDER_MARKER in first or MPS_PROVIDER_MARKER in first


def resolve_torch_device(providers: list[str] | None) -> str:
    """Map [graph].providers onto a torch device: cpu/cuda/mps.

    Recognises this repo's real ONNX provider-string vocabulary
    (``CUDAExecutionProvider``, ``CoreMLExecutionProvider``, ...), not
    literal "cuda"/"mps". Steps down to CPU — logging a WARNING rather than
    raising — when the resolved device is unavailable at runtime, so a
    stale/misconfigured providers list cannot latch a permanent failure.

    Never fails: this is safe to call both from ingest-time code (which must
    never raise on a bad provider config) and from an install-time probe.
    """
    if not providers:
        return "cpu"
    first = providers[0].lower()
    if CUDA_PROVIDER_MARKER in first:
        try:
            import torch  # noqa: PLC0415 — lazy; not installed at import time
        except ImportError:
            logger.warning(
                "[graph].providers=%r requests CUDA but torch is not installed "
                "(graph extra absent) — falling back to CPU.",
                providers,
            )
            return "cpu"
        if torch.cuda.is_available():
            return "cuda"
        logger.warning(
            "[graph].providers=%r requests CUDA but torch.cuda.is_available() "
            "is False — falling back to CPU.",
            providers,
        )
        return "cpu"
    if MPS_PROVIDER_MARKER in first:
        try:
            import torch  # noqa: PLC0415 — lazy; not installed at import time
        except ImportError:
            logger.warning(
                "[graph].providers=%r requests CoreML/MPS but torch is not installed "
                "(graph extra absent) — falling back to CPU.",
                providers,
            )
            return "cpu"
        if torch.backends.mps.is_available():
            return "mps"
        logger.warning(
            "[graph].providers=%r requests CoreML/MPS but "
            "torch.backends.mps.is_available() is False — falling back to CPU.",
            providers,
        )
        return "cpu"
    return "cpu"
