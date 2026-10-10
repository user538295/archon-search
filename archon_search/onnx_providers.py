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

When ``CUDAExecutionProvider`` is configured on a CUDA build of onnxruntime, the
CUDA/cuDNN libraries are preloaded (``onnxruntime.preload_dlls``) once per process
before onnxruntime is first asked for its providers. onnxruntime-gpu's
``libonnxruntime_providers_cuda.so`` carries no RPATH
to pip's ``site-packages/nvidia/cu13/lib``, so without that preload (or a prior
``import torch``, which archon never does before building ONNX sessions) the CUDA
provider fails to load and models silently run on CPU — see
https://onnxruntime.ai/docs/execution-providers/CUDA-ExecutionProvider.html#preload-dlls.
Plugin-EP builds may also only list ``CUDAExecutionProvider`` after the preload
registered it. Every ONNX session archon builds takes its providers from
:func:`select_onnx_providers`, evaluated before the session, so the preload here
precedes every provider query and session build for a configured CUDA list.

The fallback WARNING is latched per process, keyed by the configured list: the embedder
cache, collection sync and the routes each build their own models, so warning per build
would repeat the same line. Like the per-instance latches elsewhere (e.g.
``GraphExtractor._inference_failure_logged``) the state lives behind one small object;
it is module-level because selection has no owning instance, and
:func:`reset_fallback_warnings` / :func:`reset_cuda_dll_preload` exist so each test can
start as a fresh process.
"""
from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)

# Built into every onnxruntime build, so it needs no availability query.
CPU_PROVIDER = "CPUExecutionProvider"

# The only provider whose native libraries need preloading before the availability query.
CUDA_PROVIDER = "CUDAExecutionProvider"

# Public contract: validation reports and the installer logs `<prefix><names>`, and the
# runtime fallback WARNING starts with it.
MISSING_PROVIDERS_PREFIX = "configured ONNX providers not available: "


def missing_providers_message(unavailable: list[str]) -> str:
    """Return ``configured ONNX providers not available: <names>`` for *unavailable*."""
    return MISSING_PROVIDERS_PREFIX + ", ".join(unavailable)


class _OnceLatch:
    """Thread-safe set of keys already seen in this process."""

    def __init__(self) -> None:
        self._seen: set[tuple[str, ...]] = set()
        self._lock = threading.Lock()

    def first(self, key: tuple[str, ...]) -> bool:
        """True the first time *key* is seen; False on every later call."""
        with self._lock:
            if key in self._seen:
                return False
            self._seen.add(key)
            return True

    def reset(self) -> None:
        with self._lock:
            self._seen.clear()


_fallback_warnings = _OnceLatch()
_cuda_dll_preload = _OnceLatch()
_CUDA_DLL_PRELOAD_KEY = (CUDA_PROVIDER,)


def reset_fallback_warnings() -> None:
    """Forget which configured lists were warned about, as in a fresh process."""
    _fallback_warnings.reset()


def reset_cuda_dll_preload() -> None:
    """Forget that the CUDA DLLs were preloaded, as in a fresh process."""
    _cuda_dll_preload.reset()


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


def _available_providers(preload_cuda: bool) -> list[str]:
    """Return the ONNX execution providers the installed onnxruntime offers.

    With *preload_cuda*, first preloads the CUDA/cuDNN libraries once per process
    (:func:`_preload_cuda_dlls`). Isolated behind a helper so tests can patch it
    without importing onnxruntime.
    """
    import onnxruntime  # noqa: PLC0415 — lazy: keeps onnxruntime off CLI startup

    if preload_cuda and _cuda_dll_preload.first(_CUDA_DLL_PRELOAD_KEY):
        _preload_cuda_dlls(onnxruntime)
    return list(onnxruntime.get_available_providers())


def _preload_cuda_dlls(onnxruntime: Any) -> None:
    """Best-effort ``onnxruntime.preload_dlls(msvc=False)`` on a CUDA build; never raises.

    ``preload_dlls`` exists "Since version 1.21.0"
    (https://onnxruntime.ai/docs/execution-providers/CUDA-ExecutionProvider.html#preload-dlls),
    with the ``msvc`` parameter from that first release
    (https://github.com/microsoft/onnxruntime/blob/v1.21.0/onnxruntime/__init__.py#L241),
    while fastembed 0.9.0 (the floor archon runs on) accepts
    ``onnxruntime>=1.17.0,!=1.20.0`` on Python 3.12 (its PyPI ``requires_dist``), so an
    older onnxruntime simply lacks it and is queried as before.

    Only a CUDA build is preloaded: its module-level ``cuda_version`` is non-empty
    (https://github.com/microsoft/onnxruntime/blob/v1.21.0/onnxruntime/__init__.py#L79,
    https://github.com/microsoft/onnxruntime/blob/v1.23.0/onnxruntime/__init__.py#L90,
    https://github.com/microsoft/onnxruntime/blob/v1.31.0/onnxruntime/__init__.py#L102),
    and empty on CPU/CoreML builds, which have nothing to preload. Skipping them keeps
    the preload side-effect free there: on Windows/Linux, onnxruntime 1.21-1.23
    ``preload_dlls`` prints a "not built with CUDA 12.x support" warning to stdout on a
    build without CUDA (v1.21.0 ``__init__.py:277-281``).

    ``msvc=False``: the Windows ``msvc`` branch loads the VC++ runtime with ``CDLL`` and
    prints to stdout when it is missing (onnxruntime 1.30 ``__init__.py:394-402``).
    It is redundant here: ``import onnxruntime`` has already loaded the pybind
    extension, which links that runtime [inference: from the extension being a
    MSVC-built DLL; not verified on Windows].

    A failure is logged as one WARNING (the caller's latch runs this once per process)
    and model loading continues: onnxruntime then reports CUDA unavailable, or the CUDA
    session falls back on its own, exactly as without the preload.
    """
    preload_dlls = getattr(onnxruntime, "preload_dlls", None)
    if preload_dlls is None or not getattr(onnxruntime, "cuda_version", ""):
        return
    try:
        preload_dlls(msvc=False)
    except Exception as exc:  # best-effort: must never break model loading
        logger.warning(
            "could not preload the CUDA libraries for onnxruntime (%s); the CUDA provider"
            " may fail to load and models may run on CPU.",
            exc,
        )


def _find_unavailable(providers: list[str] | None) -> tuple[list[str], list[str]]:
    """Return ``(unavailable, available)``: the distinct configured accelerators
    onnxruntime lacks, in configured order, and what onnxruntime offers.

    CPU is never unavailable. Without an accelerator onnxruntime is not queried and
    ``available`` is empty. A configured CUDA provider preloads the CUDA libraries
    before the query.
    """
    accelerators = list(dict.fromkeys(p for p in providers or [] if p != CPU_PROVIDER))
    if not accelerators:
        return [], []
    available = _available_providers(preload_cuda=CUDA_PROVIDER in accelerators)
    return [p for p in accelerators if p not in available], available


def unavailable_onnx_providers(providers: list[str] | None) -> list[str]:
    """Return the distinct configured *providers* the installed onnxruntime lacks, in order.

    Logs nothing itself, but on a CUDA build a list naming ``CUDAExecutionProvider``
    first runs the once-per-process CUDA library preload (:func:`_preload_cuda_dlls`),
    which may log one WARNING. A CPU-only or empty list never queries onnxruntime.
    Raises whatever querying onnxruntime raises — callers decide what an unknown answer
    means.
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
