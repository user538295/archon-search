"""Validate embedding model names and retrieve their output dimension.

Dimension lookup strategy:
1. Try ``fastembed.TextEmbedding.list_supported_models()`` — O(1), no download.
2. If the model is not in the list (or the method is unavailable on older
   fastembed versions), fall back to instantiating the model with a timeout
   guard so that slow/unreachable downloads do not hang indefinitely.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from fastembed import TextEmbedding

from archon_search.embedder import make_embedder

if TYPE_CHECKING:
    from archon_search.config import SearchConfig

logger = logging.getLogger(__name__)


class ModelValidationError(ValueError):
    """Raised when the embedding model dimension cannot be determined."""


@dataclass
class ModelValidationResult:
    """Outcome of :func:`validate_models_async`.

    A null (``None``) boolean field means the corresponding probe has not run /
    completed. ``validated_at`` is ``None`` while validation is pending and a UTC
    timestamp once it has finished. See ``D6-model-validation-status.tsp`` (C1).
    """

    embedder_ok: bool | None = None
    reranker_ok: bool | None = None
    provider_warnings: list[str] = field(default_factory=list)
    validated_at: datetime | None = None


def _available_providers() -> list[str]:
    """Return the ONNX execution providers available on this host.

    Isolated behind a helper so tests can patch it without importing
    onnxruntime (which is not installed on all systems).
    """
    import onnxruntime  # noqa: PLC0415 — lazy, optional dependency

    return list(onnxruntime.get_available_providers())


def _load_cross_encoder(model_name: str, providers: list[str] | None) -> Any:
    """Instantiate a fastembed ``TextCrossEncoder`` (reranker probe entry point)."""
    from fastembed.rerank.cross_encoder import TextCrossEncoder  # noqa: PLC0415

    return TextCrossEncoder(model_name, providers=providers)


def validate_providers_shared(
    providers: list[str],
    embedding_model: str,
    reranker_model: str,
) -> tuple[bool, bool, list[str]]:
    """Probe the embedder and reranker under *providers* (C3).

    Synchronous and never raises — all exceptions are captured as warnings so
    the caller (wizard or :func:`validate_models_async`) can decide on fallback.

    Returns ``(embedder_ok, reranker_ok, warnings)``:

    - Non-CPU providers are checked against ``onnxruntime.get_available_providers()``
      first. A missing provider is a pre-flight gate: it fails BOTH probes
      (``embedder_ok = reranker_ok = False``) without instantiating either model.
    - ``reranker_model == ""`` disables the reranker: ``reranker_ok = True`` with
      no probe attempted, even when the provider gate fails for the embedder.
    - ``embedding_model == ""`` skips the embedder probe and reports
      ``embedder_ok = True`` (caller has confirmed it is already warm).
    """
    warnings: list[str] = []
    reranker_disabled = reranker_model == ""

    # Pre-flight provider gate (applies to both models when not disabled).
    non_cpu = [p for p in providers if "CPU" not in p]
    if non_cpu:
        try:
            available = _available_providers()
        except Exception as exc:  # never raises
            warnings.append(f"could not query ONNX providers: {exc}")
            return False, (True if reranker_disabled else False), warnings
        missing = [p for p in non_cpu if p not in available]
        if missing:
            warnings.append(
                "configured ONNX providers not available: " + ", ".join(missing)
            )
            # Disabled reranker still wins: nothing to probe → reranker_ok True.
            return False, (True if reranker_disabled else False), warnings

    # Embedder probe (skipped when caller already confirmed warm via "" model).
    # `providers or None`: an empty list (SearchConfig default) becomes None so
    # fastembed selects its own CPU default — matching the empty `non_cpu` gate above.
    if embedding_model == "":
        embedder_ok = True
    else:
        embedder_ok = True
        try:
            model = TextEmbedding(embedding_model, providers=providers or None)
            list(model.embed(["archon search validation probe"]))
        except Exception as exc:  # never raises
            embedder_ok = False
            warnings.append(f"embedder probe failed: {exc}")

    # Reranker probe.
    if reranker_disabled:
        reranker_ok = True
    else:
        reranker_ok = True
        try:
            ce = _load_cross_encoder(reranker_model, providers or None)
            list(ce.rerank("archon search query", ["archon search document"]))
        except Exception as exc:  # never raises
            reranker_ok = False
            warnings.append(f"reranker probe failed: {exc}")

    return embedder_ok, reranker_ok, warnings


async def validate_models_async(
    config: SearchConfig,
    timeout_seconds: float = 60,
    embedder_is_warm: bool = False,
) -> ModelValidationResult:
    """Validate the configured embedder and reranker without blocking startup (C1).

    Runs :func:`validate_providers_shared` in a worker thread guarded by
    ``asyncio.wait_for``. Never raises: a timeout or any other failure (including
    ``CancelledError``) yields a result with both ``ok`` flags ``False`` and a
    descriptive warning, rather than propagating.

    When *embedder_is_warm* is ``True`` the embedder probe is skipped (the caller
    has confirmed the global embedder is already exercised). Note: this is NOT the
    same as ``eager_load_embedders`` — see the brief's S9.
    """
    # Stale-config advisory: CoreML providers set but reranker_providers not written
    # (pre-fix wizard config). Warn once at startup so the operator knows to re-run
    # the wizard to get the split configuration.
    if (
        "CoreMLExecutionProvider" in config.providers
        and config.reranker_providers is None
    ):
        logger.warning(
            "providers=[CoreMLExecutionProvider] is set but reranker_providers is absent "
            "— the reranker may fail under CoreML. Re-run `archon-search wizard` to apply "
            "the split configuration."
        )

    embedding_model = "" if embedder_is_warm else config.embedding_model
    try:
        embedder_ok, reranker_ok, warnings = await asyncio.wait_for(
            asyncio.to_thread(
                validate_providers_shared,
                config.providers,
                embedding_model,
                config.reranker_model,
            ),
            timeout=timeout_seconds,
        )
    except asyncio.TimeoutError:
        return ModelValidationResult(
            embedder_ok=False,
            reranker_ok=False,
            provider_warnings=[f"validation timed out after {timeout_seconds}s"],
            validated_at=datetime.now(UTC),
        )
    except BaseException as exc:  # includes CancelledError — never re-raise
        logger.warning("model validation failed unexpectedly: %s", exc)
        return ModelValidationResult(
            embedder_ok=False,
            reranker_ok=False,
            provider_warnings=[f"validation failed unexpectedly: {exc}"],
            validated_at=datetime.now(UTC),
        )

    return ModelValidationResult(
        embedder_ok=embedder_ok,
        reranker_ok=reranker_ok,
        provider_warnings=warnings,
        validated_at=datetime.now(UTC),
    )


async def validate_embedding_model(
    model_name: str,
    timeout_seconds: float = 30.0,
) -> int:
    """Return the output dimension of *model_name*.

    Steps:
    1. Try ``TextEmbedding.list_supported_models()``; return ``dim`` if found.
    2. Instantiate the model in a thread (timeout-guarded) and call ``embed``
       to populate ``embedding_dim``.

    Raises:
        ModelValidationError: if the model cannot be reached within the timeout.
        Any exception from ``make_embedder`` (e.g. unknown model) propagates as-is.
    """
    # Step 1: fast path via the supported-model registry
    try:
        models = TextEmbedding.list_supported_models()
        for descriptor in models:
            if descriptor.get("name") == model_name:
                return int(descriptor["dim"])
    except AttributeError:
        # Older fastembed without list_supported_models — fall through
        pass

    # Step 2: instantiate then probe with a timeout guard.
    # make_embedder() is non-blocking (no I/O). The first embed() call triggers
    # model download + initialization inside a thread, which is what we guard.
    embedder = make_embedder(model_name)
    try:
        await asyncio.wait_for(embedder.embed(["probe"]), timeout=timeout_seconds)
    except asyncio.TimeoutError:
        raise ModelValidationError(
            "could not determine model output dimension; "
            "verify the model name and ensure it is reachable."
        )
    return embedder.embedding_dim
