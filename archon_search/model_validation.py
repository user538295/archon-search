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

# BE-23 (2026-08-19-035 K3): fixed, trivial probe inputs for _probe_extraction_model — never
# derived from real ingest content, so a large production prompt's own truncation behaviour can
# never reach this probe's warn/no-warn decision. Since BE-17 narrowed the enrichment protocol to
# community summarisation only, the probe exercises the surviving `summarize_community` method —
# the reasoning-model economic disqualifier it guards against applies to that path too.
_EXTRACTION_MODEL_PROBE_CHUNK_TEXT: str = "Alpha uses Beta."
_EXTRACTION_MODEL_PROBE_ENTITY_NAMES: tuple[str, ...] = ("Alpha", "Beta")

# K3's actual finding: the shipped `extraction_timeout_seconds` default (30.0s) is the real
# disqualifier — a reasoning model returns valid content in 111-286s, well past it. A timeout
# is therefore the primary "reasoning model" signal, distinct from any other failure (missing
# package/API key, DNS/connection failure, wrong model name, a 400 rejection, ...), which is
# plain misconfiguration, not a reasoning-model economics problem.
_EXTRACTION_MODEL_PROBE_TIMEOUT_WARNING: str = (
    "graph enrichment: [graph].extraction_model did not return usable content within the "
    "extraction probe's time budget (the smaller of your configured extraction_timeout_seconds "
    "and this project's own fixed economic ceiling) — it may be a reasoning model that is not "
    "economically supportable for per-community summarisation; see OperatorGuide/60_graph_operations.md"
)
_EXTRACTION_MODEL_PROBE_ERROR_WARNING: str = (
    "graph enrichment: [graph].extraction_model startup probe failed (not a timeout) — verify "
    "credentials, connectivity, and that the model accepts this provider's request parameters "
    "(some reasoning models reject standard parameters like max_tokens); see "
    "OperatorGuide/60_graph_operations.md"
)

# K3 (2026-08-19-035 tasks.md ~365-374): a per-chunk call taking on the order of 100+ seconds is
# uneconomical regardless of the operator's own extraction_timeout_seconds — K3 measured 111s at
# 5 pairs as the fastest observed reasoning-model failure, and the non-reasoning baseline
# (llama3.1:8b) completed the FULL 17-file corpus in 120.74s total. 75s sits below both figures,
# so the probe still catches an operator-raised extraction_timeout_seconds that would otherwise
# defeat detection entirely.
_EXTRACTION_MODEL_PROBE_ECONOMIC_CEILING_SECONDS: float = 75.0


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
    result that silently drops the graph probe. The choke point inside
    ``validate_models_async`` only chokes what routes through it
    (2026-08-19-030 C2-B-9).

    Deliberately does NOT re-run :func:`_probe_extraction_model`: this fallback fires
    when ``validate_models_async`` failed unexpectedly before committing any result
    (including ``llama_cpp_ok``) to ``app.state`` — there is no already-known llama_cpp
    reachability signal available here to gate a re-probe on (the "skip when llama_cpp
    already known down" gate needs that signal to be meaningful; passing ``None`` would
    silently bypass it and risk a second, confusing warning). Running the probe blind
    would also risk an extra billed API call (anthropic/openai) during error handling.
    The crash-path fallback intentionally stays cheap and fast rather than doing more
    network I/O.
    """
    try:
        graph_warnings = await asyncio.to_thread(graph_ner_status, config)
    except Exception:  # noqa: BLE001 — this fallback must never itself raise
        graph_warnings = ["graph NER model presence could not be determined"]
    return ModelValidationResult(
        embedder_ok=False,
        reranker_ok=False,
        provider_warnings=[*graph_warnings, reason],
        validated_at=datetime.now(UTC),
    )


def graph_ner_status(config: SearchConfig) -> list[str]:
    """Report graph prose-NER engine importability at startup as warnings
    (2026-08-19-030, rewired off the prior NER engine in cycle-2 C2-A-01/C2-B-1/C1-B-1).

    Everything returned here is operator-actionable, and that is the bar for
    this channel: ``routes_ready`` grades ``checks.models`` on
    ``provider_warnings`` alone, so anything permanent and unactionable put
    there pins the check to ``WARN`` forever, with no operator action that can
    clear it — and ``OperatorGuide/20_monitoring_and_alerts.md`` tells
    operators to alert on exactly that field (C1-I-7). There is deliberately no
    second channel for an unactionable disclosure (BE-18, C6): the shipped
    prose extraction engine (gliner, ``paths.GRAPH_NER_MODEL_NAME`` —
    "knowledgator/gliner-relex-multi-v1.0") is multilingual, so there is no
    English-only disclosure to make, and ADR 12 records the absence as
    intentional rather than deferred.

    Returns ``[]`` when ``[graph]`` is disabled. Otherwise probes gliner
    import-ability the same way ``ensure_graph_engine_importable`` gates
    ``GraphExtractor`` construction (``importlib.util.find_spec``, BE-11) so a
    missing ``[graph]`` extra surfaces on ``GET /status`` instead of only
    failing every ingest — reusing
    ``graph_extractor.GLINER_NOT_INSTALLED_MESSAGE`` so the two surfaces agree
    (T6). Unlike the prior NER engine, gliner has no separate "installed but wrong model
    artifact" state to probe here: ``ProseExtractionBackend.load()`` fetches
    the pinned revision from the Hugging Face cache lazily and any load
    failure there degrades per-ingest (``GraphExtractor._ensure_backend``),
    not at startup.

    Never raises (T7): every branch below is inside the ``try``; an import
    failure other than an absent module, or any other surprise, reads as
    "cannot determine" rather than propagating.
    """
    if not config.graph.enabled:
        return []
    # Gap (not fixed here — BE-12/BE-15 territory): this only checks gliner
    # import-ability. `ProseExtractionBackend.load()` latches a failed model
    # artifact load permanently once it happens, degrading every subsequent
    # ingest for the process lifetime — that state is invisible here.
    try:
        from archon_search.graph_extractor import (  # noqa: PLC0415
            GLINER_NOT_INSTALLED_MESSAGE,
            gliner_absent,
        )

        if gliner_absent():
            return [f"graph prose entity extraction is disabled: {GLINER_NOT_INSTALLED_MESSAGE}"]
    except Exception as exc:  # never raises — validate_models_async must not fail
        logger.warning("graph NER model probe failed: %s", exc)
        return ["graph NER model presence could not be determined"]

    return []


async def _probe_extraction_model(
    config: SearchConfig, llama_cpp_ok: bool | None = None
) -> list[str]:
    """One-shot startup probe for ``[graph].extraction_model`` (BE-23, acting on K3's outcome).

    K3 (2026-08-19-035 team plan) found reasoning models economically unsupportable via the
    per-community summarisation path — the shipped ``extraction_timeout_seconds`` default (30.0s)
    is the actual disqualifier: a reasoning model needs 111s-286s per community (measured), far
    past it, and raising inside the enrichment clients is inert because it lands inside
    ``community_builder.py``'s blanket ``except Exception``, which silently falls back to an
    unsummarised community. The only reachable, observable enforcement point is a one-shot
    startup probe feeding ``provider_warnings`` — the same pattern as :func:`graph_ner_status`
    and :func:`_probe_llama_cpp` above. Warn-not-block: never raises, never fails startup, and
    never blocks it either — the probe is bounded by
    ``min(extraction_timeout_seconds, _EXTRACTION_MODEL_PROBE_ECONOMIC_CEILING_SECONDS)``, so it
    can never run longer than the caller's own configured budget, AND an operator raising
    ``extraction_timeout_seconds`` to accommodate a slow reasoning model (K3's own experiment
    needed exactly this) can never defeat detection by rising past the fixed economic ceiling.

    Runs a single, fixed, trivial round-trip (never real ingest content) against
    ``summarize_community`` — the one surviving enrichment path since BE-17 removed
    relationship labelling — through the same
    :class:`~archon_search.enrichment.factory.EnrichmentClientFactory` client production
    enrichment would use. Because the probe input is fixed and small, a genuinely oversized
    *production* prompt hitting ``finish_reason="length"`` on a non-reasoning model (BE-22's
    warn-only case) can never reach this probe's decision — that failure mode is structurally
    isolated to production traffic.

    A timed-out call — ``asyncio.TimeoutError``/``TimeoutError`` from our own
    ``asyncio.wait_for``, or ``httpx.ReadTimeout`` (the server accepted the connection and is
    still generating) — is the primary, distinctly-worded "reasoning model" signal.
    ``httpx.ConnectTimeout``/``httpx.PoolTimeout`` (siblings of ``httpx.TimeoutException`` that
    mean the server was unreachable or the connection pool was exhausted, not that a model is
    slow) fall through with everything else — missing package/API key, DNS/connection failure,
    wrong model name, OpenAI's ``max_tokens`` 400 rejection, a client-construction failure inside
    ``EnrichmentClientFactory.build`` — to the plain-misconfiguration warning, distinctly worded
    and never claiming "reasoning model", never leaking ``str(exc)``. A call that completes
    within budget passes regardless of content (an empty result is a legitimate answer, not a
    disqualifier — only *slow* or *broken* disqualifies).

    Returns ``[]`` (skipped, no probe attempted) when ``[graph].enabled`` is False,
    ``extraction_model`` is unset, or ``provider`` has no v1 enrichment client
    (``EnrichmentClientFactory.build`` returns ``None`` — covers both ``provider`` unset and
    ``claude_cli``) — the same gate ingest itself uses, so this never probes a client ingest
    would never build. Also skipped when ``provider == "llama_cpp"`` and *llama_cpp_ok* is
    ``False`` — :func:`_probe_llama_cpp` already found the server unreachable, and re-probing
    here would only produce a second, confusing warning for the same root cause.
    """
    graph_config = config.graph
    if not graph_config.enabled or not graph_config.extraction_model:
        return []
    if graph_config.provider == "llama_cpp" and llama_cpp_ok is False:
        return []

    # min(...): bound by the operator's own configured timeout when it is the tighter of the
    # two, but never let an operator-raised extraction_timeout_seconds (e.g. to accommodate a
    # slow reasoning model, as K3's own experiment needed) defeat detection by rising past the
    # fixed economic ceiling above.
    probe_timeout = min(
        graph_config.extraction_timeout_seconds, _EXTRACTION_MODEL_PROBE_ECONOMIC_CEILING_SECONDS
    )

    try:
        from archon_search.enrichment.factory import EnrichmentClientFactory  # noqa: PLC0415

        client = EnrichmentClientFactory.build(graph_config)
        if client is None:
            return []

        probe_start = time.monotonic()
        try:
            await asyncio.wait_for(
                client.summarize_community(
                    [_EXTRACTION_MODEL_PROBE_CHUNK_TEXT],
                    list(_EXTRACTION_MODEL_PROBE_ENTITY_NAMES),
                ),
                timeout=probe_timeout,
            )
            logger.info(
                "extraction model probe for %s/%s passed in %.1fs (budget %.1fs)",
                graph_config.provider,
                graph_config.extraction_model,
                time.monotonic() - probe_start,
                probe_timeout,
            )
        except (TimeoutError, httpx.ReadTimeout):
            # asyncio.TimeoutError/TimeoutError: our own asyncio.wait_for bound. httpx.ReadTimeout:
            # the server accepted the connection and is still generating — a reasoning model times
            # out here exactly as it would per-chunk. httpx.ConnectTimeout/PoolTimeout (siblings of
            # httpx.TimeoutException) mean the server was unreachable or the pool was exhausted —
            # an infrastructure/config problem, not a reasoning-model economics one — so those fall
            # through to the generic error branch below instead.
            logger.warning(
                "extraction model probe timed out for %s/%s after %.1fs",
                graph_config.provider,
                graph_config.extraction_model,
                probe_timeout,
            )
            return [_EXTRACTION_MODEL_PROBE_TIMEOUT_WARNING]
        except Exception as exc:  # never raises — any other failure reads as misconfiguration
            logger.warning(
                "extraction model probe failed for %s/%s: %s",
                graph_config.provider,
                graph_config.extraction_model,
                exc,
            )
            return [_EXTRACTION_MODEL_PROBE_ERROR_WARNING]
        finally:
            # Only AnthropicEnrichmentClient defines aclose() (closes its persistent
            # AsyncAnthropic connection pool) — ollama/llama_cpp/openai open+close a fresh
            # httpx.AsyncClient per call and define no such method.
            if hasattr(client, "aclose"):
                try:
                    await client.aclose()
                except Exception:  # noqa: BLE001 — cleanup must never mask the probe's outcome
                    pass
    except Exception as exc:  # never raises — a factory/constructor failure is still a probe
        # failure, not a startup crash (EnrichmentClientFactory.build can raise on a malformed
        # config before the try/except below it would otherwise guard).
        logger.warning(
            "extraction model probe could not build a client for %s/%s: %s",
            graph_config.provider,
            graph_config.extraction_model,
            exc,
        )
        return [_EXTRACTION_MODEL_PROBE_ERROR_WARNING]

    return []


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
    since it does filesystem globbing and package-metadata scanning — and
    :func:`_probe_extraction_model` (2026-08-19-035 BE-23) both run before the probe
    below, and their combined output is PREPENDED to ``provider_warnings`` on every
    return path. Every ``ModelValidationResult`` this function returns is built by the
    local ``_result`` closure below, which always does that prepend — so the
    guarantee is structural (2026-08-19-030 C1-B-8), not a convention each return
    site could forget.
    """
    llama_cpp_ok = await _probe_llama_cpp(config)
    graph_warnings = await asyncio.to_thread(graph_ner_status, config)
    graph_warnings = graph_warnings + await _probe_extraction_model(
        config, llama_cpp_ok=llama_cpp_ok
    )
    embedding_model = "" if embedder_is_warm else config.embedding_model
    _start = time.monotonic()

    def _result(
        embedder_ok: bool | None,
        reranker_ok: bool | None,
        extra_warnings: list[str],
    ) -> ModelValidationResult:
        """Build a result with ``graph_warnings`` (incl. the BE-23 extraction-model probe)
        PREPENDED — the single choke point every return path in this function goes through."""
        return ModelValidationResult(
            embedder_ok=embedder_ok,
            reranker_ok=reranker_ok,
            llama_cpp_ok=llama_cpp_ok,
            provider_warnings=graph_warnings + extra_warnings,
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
