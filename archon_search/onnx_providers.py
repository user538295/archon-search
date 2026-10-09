"""Shared ONNX execution-provider selection for every fastembed model archon builds.

fastembed 0.9.0 rejects a provider list that names anything the installed onnxruntime
lacks — ``ValueError("Provider … is not available")`` even when CPU is listed too
(``fastembed/common/onnx_model.py:109-117``). A ``[database].providers`` written for an
accelerator the host's onnxruntime build does not have (CUDA without onnxruntime-gpu,
CoreML on a non-Apple build) would therefore fail every model load. Instead, the
runtime backends and the startup/wizard validation drop the unavailable names here and
load on what remains, falling back to CPU, with one WARNING per configured list
(S635) — the ONNX counterpart of ``torch_device.resolve_torch_device``.

onnxruntime is queried only when a model is about to load, never at import, so CLI
startup does not pay for it.

The fallback WARNING is latched per process, keyed by the configured list: the embedder
cache, collection sync and the routes each build their own models, so warning per build
would repeat the same line. Like the per-instance latches elsewhere (e.g.
``GraphExtractor._inference_failure_logged``) the state lives behind one small object;
it is module-level because selection has no owning instance, and
:func:`reset_fallback_warnings` exists so each test can start as a fresh process.
"""
from __future__ import annotations

import logging
import threading
from dataclasses import dataclass

logger = logging.getLogger(__name__)

# Built into every onnxruntime build, so it needs no availability query.
CPU_PROVIDER = "CPUExecutionProvider"

# Public contract: validation reports and the installer logs `<prefix><names>`, and the
# runtime fallback WARNING starts with it.
MISSING_PROVIDERS_PREFIX = "configured ONNX providers not available: "


def missing_providers_message(unavailable: list[str]) -> str:
    """Return ``configured ONNX providers not available: <names>`` for *unavailable*."""
    return MISSING_PROVIDERS_PREFIX + ", ".join(unavailable)


class _WarnOnceLatch:
    """Thread-safe set of keys already warned about in this process."""

    def __init__(self) -> None:
        self._warned: set[tuple[str, ...]] = set()
        self._lock = threading.Lock()

    def first(self, key: tuple[str, ...]) -> bool:
        """True the first time *key* is seen; False on every later call."""
        with self._lock:
            if key in self._warned:
                return False
            self._warned.add(key)
            return True

    def reset(self) -> None:
        with self._lock:
            self._warned.clear()


_fallback_warnings = _WarnOnceLatch()


def reset_fallback_warnings() -> None:
    """Forget which configured lists were warned about, as in a fresh process."""
    _fallback_warnings.reset()


@dataclass(frozen=True)
class OnnxProviderSelection:
    """What to hand fastembed for a configured provider list, and what was dropped.

    ``providers`` is ``None`` for an unset list (fastembed's own default: CPU, or CUDA
    when the onnxruntime build has it — ``onnx_model.py:101-107``); otherwise the
    distinct configured providers onnxruntime has, in configured order, or
    ``[CPU_PROVIDER]`` when none of them are available. ``unavailable`` names each
    configured provider onnxruntime lacks once, in configured order.
    """

    providers: list[str] | None
    unavailable: list[str]


def _available_providers() -> list[str]:
    """Return the ONNX execution providers the installed onnxruntime offers.

    Isolated behind a helper so tests can patch it without importing onnxruntime.
    """
    import onnxruntime  # noqa: PLC0415 — lazy: keeps onnxruntime off CLI startup

    return list(onnxruntime.get_available_providers())


def _find_unavailable(providers: list[str] | None) -> tuple[list[str], list[str]]:
    """Return ``(unavailable, available)``: the distinct configured accelerators
    onnxruntime lacks, in configured order, and what onnxruntime offers.

    CPU is never unavailable. Without an accelerator onnxruntime is not queried and
    ``available`` is empty.
    """
    accelerators = list(dict.fromkeys(p for p in providers or [] if p != CPU_PROVIDER))
    if not accelerators:
        return [], []
    available = _available_providers()
    return [p for p in accelerators if p not in available], available


def unavailable_onnx_providers(providers: list[str] | None) -> list[str]:
    """Return the distinct configured *providers* the installed onnxruntime lacks, in order.

    Pure: logs nothing. A CPU-only or empty list never queries onnxruntime. Raises
    whatever querying onnxruntime raises — callers decide what an unknown answer means.
    """
    unavailable, _available = _find_unavailable(providers)
    return unavailable


def select_onnx_providers(providers: list[str] | None) -> OnnxProviderSelection:
    """Return the providers to load a fastembed model with for configured *providers*.

    Drops every provider onnxruntime lacks and falls back to an explicit
    ``[CPU_PROVIDER]`` when none remain — explicit because ``None`` would let fastembed
    pick CUDA on a CUDA build (``onnx_model.py:101-107``). Duplicate entries are loaded
    once. When anything is dropped, logs one WARNING per distinct configured list per
    process naming the dropped and the used providers. Raises whatever querying
    onnxruntime raises: fastembed queries
    the same onnxruntime call before building a session (``onnx_model.py:86``), so the
    load could not have succeeded either.
    """
    if not providers:
        return OnnxProviderSelection(providers=None, unavailable=[])
    unavailable, available = _find_unavailable(providers)
    distinct = list(dict.fromkeys(providers))
    if not unavailable:
        return OnnxProviderSelection(providers=distinct, unavailable=[])
    usable = [p for p in distinct if p not in unavailable] or [CPU_PROVIDER]
    selection = OnnxProviderSelection(providers=usable, unavailable=unavailable)
    _warn_once(distinct, selection, available)
    return selection


def _warn_once(
    configured: list[str], selection: OnnxProviderSelection, available: list[str]
) -> None:
    if not _fallback_warnings.first(tuple(configured)):
        return
    logger.warning(
        "%s — loading models on %s instead (this onnxruntime build provides: %s). Install"
        " the onnxruntime build for the accelerator, or change the providers in the"
        " [database] config section.",
        missing_providers_message(selection.unavailable),
        ", ".join(selection.providers or []),
        ", ".join(available),
    )
