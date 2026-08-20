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
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import httpx
from fastembed import TextEmbedding

from archon_search.embedder import make_embedder

if TYPE_CHECKING:
    from archon_search.config import SearchConfig

logger = logging.getLogger(__name__)

# Short, fixed timeout for the llama-server reachability probe (S7/S17) — bounded
# independently of the caller's overall `timeout_seconds` budget for the
# embedder/reranker probe, since this is an unrelated side check.
_LLAMA_CPP_PROBE_TIMEOUT_SECONDS: float = 3.0

# Sanitized wire-facing messages (CLAUDE.md: never put `str(exc)` in a
# wire-facing field) for validate_models_async's two `except BaseException`
# branches — the raw exception is logged with a traceback instead.
_VALIDATION_FAILED_MESSAGE: str = "model validation failed unexpectedly; see server logs for details"
_RERANKER_SPLIT_VALIDATION_FAILED_MESSAGE: str = (
    "reranker split-provider validation failed unexpectedly; see server logs for details"
)


class ModelValidationError(ValueError):
    """Raised when the embedding model dimension cannot be determined."""


@dataclass
class ModelValidationResult:
    """Outcome of :func:`validate_models_async`.

    A null (``None``) boolean field means the corresponding probe has not run /
    completed. ``validated_at`` is ``None`` while validation is pending and a UTC
    timestamp once it has finished. See ``D6-model-validation-status.tsp`` (C1).

    ``llama_cpp_ok`` (S7, S17, S25): ``None`` while the llama-server reachability
    probe has not run, or when ``llama_cpp`` is not configured as the provider in
    any of ``[hyde]``/``[rag_fusion]``/``[graph]`` (no probe is attempted); ``True``
    when the probe reached ``GET /v1/models``; ``False`` otherwise. This probe is
    warn-not-block: it must never feed the ``/ready`` FAIL gate (S25).
    """

    embedder_ok: bool | None = None
    reranker_ok: bool | None = None
    llama_cpp_ok: bool | None = None
    provider_warnings: list[str] = field(default_factory=list)
    # Informational, permanent, not operator-actionable — deliberately NOT
    # graded by `routes_ready._models_check_status` (2026-08-19-030 C1-I-7).
    provider_notes: list[str] = field(default_factory=list)
    validated_at: datetime | None = None


def _llama_cpp_probe_url(config: SearchConfig) -> str | None:
    """Return the llama-server base URL to probe, or ``None`` if ``llama_cpp`` is not
    configured as the provider in any of ``[hyde]``/``[rag_fusion]``/``[graph]``.

    Checked in a fixed section order — the wizard writes the same base URL to every
    section it configures for a given local llama-server, so the first configured
    match is representative.
    """
    if config.hyde.provider == "llama_cpp":
        return config.hyde.llama_cpp_base_url
    if config.rag_fusion.provider == "llama_cpp":
        return config.rag_fusion.llama_cpp_base_url
    if config.graph.provider == "llama_cpp":
        return config.graph.llama_cpp_base_url
    return None


async def _probe_llama_cpp(config: SearchConfig) -> bool | None:
    """Non-blocking llama-server reachability probe (S7, S17).

    Returns ``None`` when ``llama_cpp`` is not configured anywhere (no probe
    attempted). Otherwise issues ``GET {base_url}/v1/models`` under a short, fixed
    timeout and returns ``True`` on any 2xx response, ``False`` on any failure
    (connection error, timeout, non-2xx status). Never raises — a WARNING is
    logged on failure so boot can continue uninterrupted (S7).
    """
    base_url = _llama_cpp_probe_url(config)
    if base_url is None:
        return None
    try:
        async with httpx.AsyncClient(
            base_url=base_url, timeout=_LLAMA_CPP_PROBE_TIMEOUT_SECONDS
        ) as client:
            response = await client.get("/v1/models")
            response.raise_for_status()
        return True
    except Exception as exc:  # never raises — any failure reads as unreachable
        logger.warning("llama-server unreachable at %s: %s", base_url, exc)
        return False


async def failed_result(reason: str, config: SearchConfig) -> ModelValidationResult:
    """Build the ``ModelValidationResult`` for a validation run that died.

    Exists so callers outside :func:`validate_models_async` — notably the
    lifespan's task-crash fallback in ``server/app.py`` — cannot construct a
    result that silently drops the graph probe or ``llama_cpp_ok``. The choke
    point inside ``validate_models_async`` only chokes what routes through it
    (2026-08-19-030 C2-B-9).
    """
    try:
        graph_warnings, graph_notes = await asyncio.to_thread(graph_ner_status, config)
    except Exception:  # noqa: BLE001 — this fallback must never itself raise
        graph_warnings, graph_notes = ["graph NER model presence could not be determined"], []
    return ModelValidationResult(
        embedder_ok=False,
        reranker_ok=False,
        provider_warnings=[*graph_warnings, reason],
        provider_notes=graph_notes,
        validated_at=datetime.now(UTC),
    )


def graph_ner_status(config: SearchConfig) -> tuple[list[str], list[str]]:
    """Report graph prose-NER state at startup as ``(warnings, notes)`` (2026-08-19-030).

    The split is load-bearing: ``routes_ready`` grades ``checks.models`` on
    ``provider_warnings`` alone, so anything permanent and unactionable put
    there pins the check to ``WARN`` forever, with no operator action that can
    clear it — and ``OperatorGuide/20_monitoring_and_alerts.md`` tells
    operators to alert on exactly that field. Actionable states (missing extra,
    missing or incompatible model) are warnings; the English-only disclosure is
    a permanent property of the chosen engine and is a *note* (C1-I-7).

    Returns ``[]`` when ``[graph]`` is disabled. Otherwise probes the spaCy model
    the same way the extractor resolves it (installed package, the
    wizard-provisioned data-dir path, or "present but incompatible with the
    installed spaCy" — C1-I-3) so a missing or unusable model surfaces on
    ``GET /status`` instead of only in per-file ingest logs. A missing spaCy
    *package* (the ``[graph]`` extra itself not installed) is reported
    distinctly from a missing *model* — reusing
    ``graph_extractor.SPACY_NOT_INSTALLED_MESSAGE`` so the two surfaces agree
    (T6) — rather than blaming the model for spaCy's own absence. Also
    discloses that ``en_core_web_sm`` is English-only when the deployment is
    multilingual, even alongside a missing/incompatible model (T16 — both
    notes are independently true and both are actionable) —
    ``[[2026-08-19-035-multilingual-graph-ner-brief.md]]`` tracks the
    successor engine.

    Never raises (T7): every branch below is inside the ``try``; a broken data
    dir, an import failure other than ``ImportError``, or any other surprise
    reads as "cannot determine" rather than propagating.
    """
    if not config.graph.enabled:
        return [], []
    try:
        try:
            import spacy  # noqa: F401, PLC0415
        except ImportError:
            from archon_search.graph_extractor import (  # noqa: PLC0415
                SPACY_NOT_INSTALLED_MESSAGE,
            )

            return (
                [f"graph prose entity extraction is disabled: {SPACY_NOT_INSTALLED_MESSAGE}"],
                [],
            )

        from archon_search.graph_extractor import (  # noqa: PLC0415
            ENGLISH_ONLY_DISCLOSURE,
            SPACY_MODEL_NAME,
            resolve_spacy_model,
        )

        resolution = resolve_spacy_model()
    except Exception as exc:  # never raises — validate_models_async must not fail
        logger.warning("graph NER model probe failed: %s", exc)
        return ["graph NER model presence could not be determined"], []

    warnings: list[str] = []
    if resolution.target is None:
        if resolution.incompatible_versions:
            warnings.append(
                "graph prose entity extraction is disabled: spaCy model "
                f"{SPACY_MODEL_NAME!r} is present under the data directory "
                f"({', '.join(resolution.incompatible_versions)}) but incompatible "
                "with the installed spaCy version — re-run `archon-search wizard` "
                "to provision a compatible model"
            )
        else:
            warnings.append(
                "graph prose entity extraction is disabled: spaCy model "
                f"{SPACY_MODEL_NAME!r} is neither installed nor provisioned under the "
                "data directory — re-run `archon-search wizard` to provision it"
            )
        return warnings, ([ENGLISH_ONLY_DISCLOSURE] if config.multilingual else [])

    return [], ([ENGLISH_ONLY_DISCLOSURE] if config.multilingual else [])


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

    Also runs the llama-server reachability probe (S7, S17) — see
    :func:`_probe_llama_cpp`. This is an independent, short-timeout side check;
    its outcome (``llama_cpp_ok``) is attached to every returned result, including
    the timeout/failure early-return paths below.

    Likewise :func:`graph_ner_status` (2026-08-19-030) — dispatched to a thread
    since it does filesystem globbing and package-metadata scanning — runs before
    the probe and its output is PREPENDED to ``provider_warnings`` on every return
    path. Every ``ModelValidationResult`` this function returns is built by the
    local ``_result`` closure below, which always does that prepend — so the
    guarantee is structural (2026-08-19-030 C1-B-8), not a convention each return
    site could forget.
    """
    llama_cpp_ok = await _probe_llama_cpp(config)
    graph_warnings, graph_notes = await asyncio.to_thread(graph_ner_status, config)
    embedding_model = "" if embedder_is_warm else config.embedding_model
    _start = time.monotonic()

    def _result(
        embedder_ok: bool | None,
        reranker_ok: bool | None,
        extra_warnings: list[str],
    ) -> ModelValidationResult:
        """Build a result with ``graph_warnings`` PREPENDED — the single choke
        point every return path in this function goes through."""
        return ModelValidationResult(
            embedder_ok=embedder_ok,
            reranker_ok=reranker_ok,
            llama_cpp_ok=llama_cpp_ok,
            provider_warnings=graph_warnings + extra_warnings,
            provider_notes=list(graph_notes),
            validated_at=datetime.now(UTC),
        )

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
        return _result(False, False, [f"validation timed out after {timeout_seconds}s"])
    except BaseException as exc:  # includes CancelledError — never re-raise
        logger.warning("model validation failed unexpectedly: %s", exc, exc_info=True)
        return _result(False, False, [_VALIDATION_FAILED_MESSAGE])

    # If split config, re-validate reranker under its actual providers.
    # Use remaining budget so total wall time stays within timeout_seconds.
    if config.reranker_providers is not None and config.reranker_model:
        _remaining = timeout_seconds - (time.monotonic() - _start)
        if _remaining <= 0:
            reranker_ok = False
            warnings = warnings + ["reranker split-provider validation skipped: timeout budget exhausted"]
        else:
            try:
                _, reranker_ok_actual, split_warns = await asyncio.wait_for(
                    asyncio.to_thread(
                        validate_providers_shared,
                        config.reranker_providers,
                        "",  # skip embedder (already validated above)
                        config.reranker_model,
                    ),
                    timeout=_remaining,
                )
                reranker_ok = reranker_ok_actual
                warnings = warnings + split_warns
            except asyncio.TimeoutError:
                reranker_ok = False
                warnings = warnings + [f"reranker split-provider validation timed out after {_remaining:.1f}s"]
            except BaseException as exc:  # includes CancelledError — never re-raise
                logger.warning(
                    "reranker split-provider validation failed: %s", exc, exc_info=True
                )
                return _result(
                    embedder_ok, False, warnings + [_RERANKER_SPLIT_VALIDATION_FAILED_MESSAGE]
                )

    result = _result(embedder_ok, reranker_ok, warnings)

    # Stale-config advisory: CoreML set, no split written, reranker enabled, AND
    # CoreML actually failed for the reranker (distinguishes stale from both-pass)
    if (
        "CoreMLExecutionProvider" in config.providers
        and config.reranker_providers is None
        and config.reranker_model != ""
        and not reranker_ok
    ):
        logger.warning(
            "providers=[CoreMLExecutionProvider] is set but reranker_providers is absent "
            "and the reranker failed under CoreML — re-run `archon-search wizard` to apply "
            "the split configuration."
        )

    return result


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
        ModelValidationError: if the model cannot be reached within the timeout,
            or if the backend fails to load it (e.g. an unknown model name).
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
    # No `providers=` on purpose: this probe answers "what output dimension does
    # this model name have?", which is provider-independent. Running it under an
    # accelerator would only make a PATCH slower and could fail for provider
    # reasons that say nothing about the model name's validity.
    embedder = make_embedder(model_name)
    try:
        await asyncio.wait_for(embedder.embed(["probe"]), timeout=timeout_seconds)
    except asyncio.TimeoutError:
        raise ModelValidationError(
            "could not determine model output dimension; "
            "verify the model name and ensure it is reachable."
        )
    except Exception as exc:
        # An unknown/unsupported model name makes the backend raise (fastembed
        # raises ValueError). Surface it as ModelValidationError so callers can
        # map it to 422 rather than letting it escape as an unhandled 500.
        raise ModelValidationError(
            f"could not load embedding model {model_name!r}: {exc}"
        ) from exc
    return embedder.embedding_dim
