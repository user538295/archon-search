"""Startup memory estimate for the configured models, compared with the memory limit.

Models load as fp32. Several cached embedders plus a reranker can exceed a container's
memory limit, and the OOM kill that follows names no cause. :func:`warn_if_models_exceed_memory`
logs one WARNING at startup naming the estimate and what to change. It never refuses to start:
the estimate is a planning figure, not a measurement.

The limit is the tighter of the cgroup limit (v2 ``memory.max``, else v1
``memory.limit_in_bytes``) and the host's physical RAM. On Apple Silicon the GPU shares system
RAM, so the same figure applies. A CUDA device has its own memory pool, which this check does
not model.
"""
from __future__ import annotations

import logging
import os
from collections.abc import Iterable
from pathlib import Path
from typing import TYPE_CHECKING

from archon_search.model_specs import EMBEDDER_SPECS, RERANKER_SPECS

if TYPE_CHECKING:
    from archon_search.config import SearchConfig

logger = logging.getLogger(__name__)

#: Resident memory per MB of fp32 weights: activations, the allocator and tokenizer buffers.
_WEIGHTS_HEADROOM_FACTOR = 1.5
#: Fixed cost besides the model weights, in MB: the Python + torch runtime, LanceDB caches and
#: one document-parse worker (about 1 GB). A conservative planning figure, not a measurement.
_RUNTIME_BASELINE_MB = 1536.0
_BYTES_PER_MB = 1024 * 1024
#: cgroup v2 then v1 limit files. v2 reads ``max`` and v1 a huge number when unlimited.
_CGROUP_LIMIT_FILES = (
    Path("/sys/fs/cgroup/memory.max"),
    Path("/sys/fs/cgroup/memory/memory.limit_in_bytes"),
)
#: Any cgroup limit at or above this is "no limit" (v1 reports 2**63 rounded to a page).
_CGROUP_UNLIMITED_BYTES = 1 << 60
_CGROUP_SOURCE = "cgroup memory limit"
_SYSTEM_RAM_SOURCE = "system RAM"


def estimate_model_memory_mb(
    embedder_models: Iterable[str], reranker_model: str, *, pinned_model: str, cache_size: int
) -> float:
    """Return the planning estimate, in MB, of the memory the configured models need.

    *pinned_model* (the default embedder, shared with the pipeline) is always resident and sits outside
    the cache's *cache_size* slots, so it counts in full; of the other *embedder_models* only the
    *cache_size* largest can be cached at once. A name outside the registry adds nothing (it is
    rejected when loaded).
    """
    others = sorted(
        (EMBEDDER_SPECS[name].weight_mb for name in set(embedder_models) - {pinned_model} if name in EMBEDDER_SPECS),
        reverse=True,
    )[:cache_size]
    pinned = EMBEDDER_SPECS.get(pinned_model)
    reranker = RERANKER_SPECS.get(reranker_model)
    weights_mb = (
        sum(others)
        + (pinned.weight_mb if pinned is not None else 0.0)
        + (reranker.weight_mb if reranker is not None else 0.0)
    )
    return weights_mb * _WEIGHTS_HEADROOM_FACTOR + _RUNTIME_BASELINE_MB


def _cgroup_limit_bytes() -> int | None:
    """Return the tightest cgroup memory limit in bytes, or ``None`` when unlimited or unreadable."""
    limits: list[int] = []
    for path in _CGROUP_LIMIT_FILES:
        try:
            limit = int(path.read_text().strip())
        except (OSError, ValueError):  # absent, unreadable, ``max`` or garbage: not a limit
            continue
        if 0 < limit < _CGROUP_UNLIMITED_BYTES:
            limits.append(limit)
    return min(limits, default=None)


def _system_ram_bytes() -> int | None:
    """Return physical RAM in bytes, or ``None`` where ``os.sysconf`` cannot say (Windows)."""
    try:
        return os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES")
    except (AttributeError, ValueError, OSError):
        return None


def read_memory_limit() -> tuple[float, str] | None:
    """Return ``(limit_mb, source)`` of the tighter of the cgroup limit and system RAM, else ``None``."""
    candidates = [
        (limit, source)
        for limit, source in (
            (_cgroup_limit_bytes(), _CGROUP_SOURCE),
            (_system_ram_bytes(), _SYSTEM_RAM_SOURCE),
        )
        if limit is not None
    ]
    if not candidates:
        return None
    limit_bytes, source = min(candidates)
    return limit_bytes / _BYTES_PER_MB, source


def warn_if_models_exceed_memory(config: SearchConfig, embedder_models: Iterable[str]) -> None:
    """Log a WARNING when the estimated model memory exceeds the memory limit.

    *embedder_models* are the embedders the server warms at startup. Never raises.
    """
    estimate = estimate_model_memory_mb(
        embedder_models,
        config.reranker_model,
        pinned_model=config.embedding_model,
        cache_size=config.embedder_cache_size,
    )
    limit = read_memory_limit()
    if limit is None:
        logger.debug("memory limit not detectable; skipping the model memory check")
        return
    limit_mb, source = limit
    if estimate <= limit_mb:
        return
    logger.warning(
        "estimated model memory %.0f MB (fp32 weights x %.1f + %.0f MB runtime) exceeds the %s of "
        "%.0f MB; the process may be OOM-killed under load. Use a smaller profile or a smaller "
        "embedding_model / reranker_model, lower embedder_cache_size (now %d), or raise the limit.",
        estimate,
        _WEIGHTS_HEADROOM_FACTOR,
        _RUNTIME_BASELINE_MB,
        source,
        limit_mb,
        config.embedder_cache_size,
    )
