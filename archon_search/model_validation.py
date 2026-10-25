"""Validate embedding model names and startup model probes.

``validate_embedding_model`` resolves an embedder's output dimension from its
spec (``archon_search.model_specs``) without loading anything. ``validate_device``
loads the configured embedder and reranker on ``[database].device`` and runs one
inference each; ``validate_models_async`` runs it in the background at server startup.
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Final

import httpx

from archon_search.embedder import EmbedderBackend, ModelEmbedder
from archon_search.model_specs import UnsupportedModelError, get_embedder_spec
from archon_search.reranker import ModelReranker, RerankerBackend
from archon_search.torch_runtime import AUTO_DEVICE, CPU_DEVICE, resolve_device

if TYPE_CHECKING:
    from archon_search.config import SearchConfig

logger = logging.getLogger(__name__)

# Short, fixed timeout for the llama-server reachability probe — bounded
# independently of the caller's overall `timeout_seconds` budget for the
# embedder/reranker probe, since this is an unrelated side check.
_LLAMA_CPP_PROBE_TIMEOUT_SECONDS: float = 3.0
# The ``provider`` value that selects a local llama-server in ``[hyde]``/``[rag_fusion]``/``[graph]``.
_LLAMA_CPP_PROVIDER: Final[str] = "llama_cpp"

# Sanitized ``provider_warnings`` texts (CLAUDE.md: never put `str(exc)` in a wire-facing
# field; they reach ``GET /status``). The caught exception can name model files under the
# data dir, so it stays in the log. The "see the log" texts never say "server logs": the
# install wizard runs the same probes with no server (install/installer.py).
#
# validate_models_async's two failure branches (cancelled inside the probe, any other exception;
# both logged with a traceback).
VALIDATION_FAILED_WARNING: Final[str] = "model validation failed unexpectedly; see the log for details"
# validate_models_async's timeout outcome; ``seconds`` is the budget that ran out.
VALIDATION_TIMED_OUT_TEMPLATE: Final[str] = "validation timed out after {seconds}s"
# The lifespan's validation-task crash fallback (server/app.py, through failed_result).
VALIDATION_TASK_FAILED_WARNING: Final[str] = "validation task failed unexpectedly; see the log for details"
# graph_ner_status's probe failing, and failed_result's own graph_ner_status call failing.
GRAPH_NER_UNDETERMINED_WARNING: Final[str] = (
    "graph NER model presence could not be determined; see the log for details"
)
# graph_ner_status's "gliner is not importable" outcome. The install command
# (graph_extractor.gliner_not_installed_message) can replay the uv tool receipt's index
# credentials and local paths, so it goes to the log only.
GLINER_NOT_INSTALLED_WARNING: Final[str] = (
    "graph prose entity extraction is disabled: gliner is not installed. "
    "Install archon-search[graph] to use graph prose entity extraction"
)
# validate_device's per-probe handlers. A failed probe is an expected outcome (e.g. an
# unloadable model), so it logs a WARNING with the exception message and keeps the
# traceback at DEBUG: the wizard configures no logging, and Python's last-resort handler would
# print a WARNING's traceback straight to the user.
EMBEDDER_PROBE_FAILED_WARNING: Final[str] = "embedder probe failed; see the log for details"
RERANKER_PROBE_FAILED_WARNING: Final[str] = "reranker probe failed; see the log for details"
# validate_device's step-down outcome: an explicit accelerator the host lacks. ``requested`` is
# the configured ``mps`` / ``cuda``; the models run on CPU.
DEVICE_UNAVAILABLE_WARNING_TEMPLATE: Final[str] = "device unavailable: {requested}; models loaded on cpu"
# validate_device's fixed probe inputs: one text through the embedder, one pair through the reranker.
_EMBEDDER_PROBE_TEXT: Final[str] = "archon search validation probe"
_RERANKER_PROBE_PAIR: Final[tuple[str, str]] = ("archon search query", "archon search document")

# Fixed, trivial probe inputs for _probe_extraction_model — never derived from real ingest
# content, so a large production prompt's own truncation behaviour can never reach this
# probe's warn/no-warn decision. The probe exercises `summarize_community`, the only
# enrichment method left (the protocol covers community summarisation only); the
# reasoning-model economic disqualifier it guards against applies to that path.
_EXTRACTION_MODEL_PROBE_CHUNK_TEXT: str = "Alpha uses Beta."
_EXTRACTION_MODEL_PROBE_ENTITY_NAMES: tuple[str, ...] = ("Alpha", "Beta")

# The shipped `extraction_timeout_seconds` default (30.0s) is the real disqualifier: a
# reasoning model returns valid content in 111-286s, well past it. A timeout is therefore the
# primary "reasoning model" signal, distinct from any other failure (missing package/API key,
# DNS/connection failure, wrong model name, a 400 rejection, ...), which is plain
# misconfiguration, not a reasoning-model economics problem.
EXTRACTION_MODEL_PROBE_TIMEOUT_WARNING: Final[str] = (
    "graph enrichment: [graph].extraction_model did not return usable content within the "
    "extraction probe's time budget (the smaller of your configured extraction_timeout_seconds "
    "and this project's own fixed economic ceiling) — it may be a reasoning model that is not "
    "economically supportable for per-community summarisation; see OperatorGuide/60_graph_operations.md"
)
EXTRACTION_MODEL_PROBE_ERROR_WARNING: Final[str] = (
    "graph enrichment: [graph].extraction_model startup probe failed (not a timeout) — verify "
    "credentials, connectivity, and that the model accepts this provider's request parameters "
    "(some reasoning models reject standard parameters like max_tokens); see "
    "OperatorGuide/60_graph_operations.md"
)

# A per-chunk call taking on the order of 100+ seconds is uneconomical regardless of the
# operator's own extraction_timeout_seconds. Measured on the project's extraction corpus: a
# reasoning model's fastest failure took 111s for 5 pairs, while a non-reasoning baseline
# (llama3.1:8b) completed the whole 17-file corpus in 120.74s. 75s sits below both figures, so
# the probe still catches an operator-raised extraction_timeout_seconds that would otherwise
# defeat detection entirely.
_EXTRACTION_MODEL_PROBE_ECONOMIC_CEILING_SECONDS: float = 75.0


class ModelValidationError(ValueError):
    """Raised when the requested embedding model is not supported."""


@dataclass(frozen=True)
class ModelValidationResult:
    """Outcome of :func:`validate_models_async`.

    A null (``None``) boolean field means the corresponding probe has not run /
    completed. ``validated_at`` is ``None`` while validation is pending and a UTC
    timestamp once it has finished; a finished validation has resolved both the
    embedder and the reranker probe, which construction enforces. See
    ``D6-model-validation-status.tsp`` (C1).

    ``llama_cpp_ok``: ``None`` while the llama-server reachability
    probe has not run, or when ``llama_cpp`` is not configured as the provider in
    any of ``[hyde]``/``[rag_fusion]``/``[graph]`` (no probe is attempted); ``True``
    when the probe reached ``GET /v1/models``; ``False`` otherwise. This probe is
    warn-not-block: it must never feed the ``checks.models`` FAIL status.
    """

    embedder_ok: bool | None = None
    reranker_ok: bool | None = None
    llama_cpp_ok: bool | None = None
    provider_warnings: list[str] = field(default_factory=list)
    validated_at: datetime | None = None

    def __post_init__(self) -> None:
        if self.validated_at is not None and (self.embedder_ok is None or self.reranker_ok is None):
            raise ValueError("validated_at is set but the embedder and reranker probes are not both resolved")


def _llama_cpp_probe_url(config: SearchConfig) -> str | None:
    """Return the llama-server base URL to probe, or ``None`` if ``llama_cpp`` is not
    configured as the provider in any of ``[hyde]``/``[rag_fusion]``/``[graph]``.

    Checked in a fixed section order — the wizard writes the same base URL to every
    section it configures for a given local llama-server, so the first configured
    match is representative.
    """
    if config.hyde.provider == _LLAMA_CPP_PROVIDER:
        return config.hyde.llama_cpp_base_url
    if config.rag_fusion.provider == _LLAMA_CPP_PROVIDER:
        return config.rag_fusion.llama_cpp_base_url
    if config.graph.provider == _LLAMA_CPP_PROVIDER:
        return config.graph.llama_cpp_base_url
    return None


async def _probe_llama_cpp(config: SearchConfig) -> bool | None:
    """Non-blocking llama-server reachability probe.

    Returns ``None`` when ``llama_cpp`` is not configured anywhere (no probe
    attempted). Otherwise issues ``GET {base_url}/v1/models`` under a short, fixed
    timeout and returns ``True`` on any 2xx response, ``False`` on any failure
    (connection error, timeout, non-2xx status). Never raises — a WARNING is
    logged on failure so boot can continue uninterrupted (S7). It never blocks
    startup or queries, but it runs before the embedder/reranker probe inside
    background validation, so its duration extends the ``/ready`` 503 window
    (``checks.models`` stays ``pending`` until validation finishes).
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
    except Exception as exc:  # noqa: BLE001 — probe reports any failure as "unreachable" (False); it must never raise
        logger.warning("llama-server unreachable at %s: %s", base_url, exc)
        return False


async def failed_result(reason: str, config: SearchConfig) -> ModelValidationResult:
    """Build the ``ModelValidationResult`` for a validation run that died.

    Exists so callers outside :func:`validate_models_async` — notably the
    lifespan's task-crash fallback in ``server/app.py`` — cannot construct a
    result that silently drops the graph probe. The choke point inside
    ``validate_models_async`` only chokes what routes through it
    (a result built outside it would bypass that choke point).

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
    except Exception:
        logger.warning("graph NER status check failed", exc_info=True)
        graph_warnings = [GRAPH_NER_UNDETERMINED_WARNING]
    return ModelValidationResult(
        embedder_ok=False,
        reranker_ok=False,
        provider_warnings=[*graph_warnings, reason],
        validated_at=datetime.now(UTC),
    )


def graph_ner_status(config: SearchConfig) -> list[str]:
    """Report graph prose-NER engine importability at startup as warnings
    (rewired off the prior NER engine onto gliner).

    Everything returned here is operator-actionable, and that is the bar for
    this channel: ``routes_ready`` grades ``checks.models`` on
    ``provider_warnings`` alone, so anything permanent and unactionable put
    there pins the check to ``WARN`` forever, with no operator action that can
    clear it — and ``OperatorGuide/20_monitoring_and_alerts.md`` tells
    operators to alert on exactly that field. There is deliberately no
    second channel for an unactionable disclosure: the shipped
    prose extraction engine (gliner, ``paths.GRAPH_NER_MODEL_NAME`` —
    "knowledgator/gliner-relex-multi-v1.0") is multilingual, so there is no
    English-only disclosure to make, and ADR 12 records the absence as
    intentional rather than deferred.

    Returns ``[]`` when ``[graph]`` is disabled. Otherwise probes gliner
    import-ability the same way ``ensure_graph_engine_importable`` gates
    ``GraphExtractor`` construction (``importlib.util.find_spec``) so a
    missing ``[graph]`` extra surfaces on ``GET /status`` instead of only
    failing every ingest. The wire warning is the fixed
    ``GLINER_NOT_INSTALLED_WARNING``; the exact install command
    (``gliner_not_installed_message``, which can replay the uv tool receipt's index
    credentials and local paths) is logged at WARNING, never sent. Unlike the
    prior NER engine, gliner has no separate "installed but wrong model
    artifact" state to probe here: ``ProseExtractionBackend.load()`` fetches
    the pinned revision from the Hugging Face cache lazily and any load
    failure there degrades per-ingest (``GraphExtractor._ensure_backend``),
    not at startup.

    Never raises: the presence probe is inside a ``try``; an import
    failure other than an absent module, or any other surprise, reads as
    "cannot determine" rather than propagating. Building or logging the install
    command is guarded separately, so its failure never masks a known-missing gliner.
    """
    if not config.graph.enabled:
        return []
    # Known gap: this only checks gliner import-ability. `ProseExtractionBackend.load()`
    # latches a failed model artifact load permanently once it happens, degrading every
    # subsequent ingest for the process lifetime — that state is invisible here.
    try:
        from archon_search.graph_extractor import gliner_absent

        absent = gliner_absent()
    except Exception as exc:  # never raises — validate_models_async must not fail
        logger.warning("graph NER model probe failed: %s", exc, exc_info=True)
        return [GRAPH_NER_UNDETERMINED_WARNING]

    if not absent:
        return []
    _log_gliner_install_command()
    return [GLINER_NOT_INSTALLED_WARNING]


def _log_gliner_install_command() -> None:
    """Log the exact gliner install command at WARNING; never raises."""
    try:
        from archon_search.graph_extractor import gliner_not_installed_message

        logger.warning("graph prose entity extraction is disabled: %s", gliner_not_installed_message())
    except Exception:  # intentional: the wire warning is already decided; a bad hint must not change it
        logger.warning(
            "could not build the gliner install command; install the archon-search[graph] extra", exc_info=True
        )


async def _probe_extraction_model(
    config: SearchConfig, llama_cpp_ok: bool | None = None
) -> list[str]:
    """One-shot startup probe for ``[graph].extraction_model``.

    Reasoning models are economically unsupportable for per-community summarisation: the shipped
    ``extraction_timeout_seconds`` default (30.0s) is the actual disqualifier, as a reasoning
    model needs 111s-286s per community (measured), far past it. Raising inside the enrichment
    clients is inert, because it lands inside ``community_builder.py``'s blanket
    ``except Exception``, which silently falls back to an unsummarised community. The only
    reachable, observable enforcement point is a one-shot startup probe feeding
    ``provider_warnings`` — the same pattern as :func:`graph_ner_status` and
    :func:`_probe_llama_cpp` above. Warn-not-block: never raises, never fails startup, and
    never blocks startup or queries — but it runs before the embedder/reranker probe inside
    background validation, so its duration extends the ``/ready`` 503 window (``checks.models``
    stays ``pending`` until validation finishes). The probe is bounded by
    ``min(extraction_timeout_seconds, _EXTRACTION_MODEL_PROBE_ECONOMIC_CEILING_SECONDS)``, so it
    can never run longer than the caller's own configured budget, AND an operator raising
    ``extraction_timeout_seconds`` to accommodate a slow reasoning model can never defeat
    detection by rising past the fixed economic ceiling.

    Runs a single, fixed, trivial round-trip (never real ingest content) against
    ``summarize_community`` — the only enrichment method left, since relationship labelling was
    removed — through the same
    :class:`~archon_search.enrichment.factory.EnrichmentClientFactory` client production
    enrichment would use. Because the probe input is fixed and small, a genuinely oversized
    *production* prompt hitting ``finish_reason="length"`` on a non-reasoning model (which only
    warns in production) can never reach this probe's decision — that failure mode is structurally
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
    if graph_config.provider == _LLAMA_CPP_PROVIDER and llama_cpp_ok is False:
        return []

    # min(...): bound by the operator's own configured timeout when it is the tighter of the
    # two, but never let an operator-raised extraction_timeout_seconds (e.g. to accommodate a
    # slow reasoning model) defeat detection by rising past the
    # fixed economic ceiling above.
    probe_timeout = min(
        graph_config.extraction_timeout_seconds, _EXTRACTION_MODEL_PROBE_ECONOMIC_CEILING_SECONDS
    )

    try:
        from archon_search.enrichment.factory import EnrichmentClientFactory

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
            return [EXTRACTION_MODEL_PROBE_TIMEOUT_WARNING]
        except Exception as exc:  # never raises — any other failure reads as misconfiguration
            logger.warning(
                "extraction model probe failed for %s/%s: %s",
                graph_config.provider,
                graph_config.extraction_model,
                exc,
                exc_info=True,
            )
            return [EXTRACTION_MODEL_PROBE_ERROR_WARNING]
        finally:
            # Only AnthropicEnrichmentClient defines aclose() (closes its persistent
            # AsyncAnthropic connection pool) — ollama/llama_cpp/openai open+close a fresh
            # httpx.AsyncClient per call and define no such method.
            if hasattr(client, "aclose"):
                try:
                    await client.aclose()
                except Exception:
                    logger.debug("extraction-model probe client aclose() failed", exc_info=True)
    except Exception as exc:  # never raises — a factory/constructor failure is still a probe
        # failure, not a startup crash (EnrichmentClientFactory.build can raise on a malformed
        # config before the try/except below it would otherwise guard).
        logger.warning(
            "extraction model probe could not build a client for %s/%s: %s",
            graph_config.provider,
            graph_config.extraction_model,
            exc,
            exc_info=True,
        )
        return [EXTRACTION_MODEL_PROBE_ERROR_WARNING]

    return []


def _device_step_down_warning(device: str) -> str | None:
    """The warning for an explicit accelerator this host lacks, else ``None``.

    ``auto`` and ``cpu`` never step down, so they skip resolution (and the PyTorch
    import it implies). A resolution failure yields ``None``: the model probes that
    follow resolve the device themselves and report their own failure.
    """
    if device in (AUTO_DEVICE, CPU_DEVICE):
        return None
    try:
        resolved = resolve_device(device)
    except Exception:  # never raises
        logger.warning("could not resolve device %r: model probes will report any failure", device, exc_info=True)
        return None
    if resolved == device:
        return None
    return DEVICE_UNAVAILABLE_WARNING_TEMPLATE.format(requested=device)


def _probe_embedder(model_name: str, device: str, backend: EmbedderBackend | None) -> bool:
    """Embed one text; ``False`` (logged) on any failure.

    Runs *backend* when given (the serving instance), else a fresh ``ModelEmbedder`` for
    *model_name* on *device*.
    """
    try:
        (backend if backend is not None else ModelEmbedder(model_name, device=device)).encode([_EMBEDDER_PROBE_TEXT])
    except Exception as exc:  # never raises
        logger.warning("embedder probe failed for %s on device %s: %s", model_name, device, exc)
        logger.debug("embedder probe failed for %s on device %s", model_name, device, exc_info=True)
        return False
    return True


def _probe_reranker(model_name: str, device: str, backend: RerankerBackend | None) -> bool:
    """Score one pair; ``False`` (logged) on any failure.

    Runs *backend* when given (the serving instance), else a fresh ``ModelReranker`` for
    *model_name* on *device*.
    """
    try:
        (backend if backend is not None else ModelReranker(model_name, device=device)).predict([_RERANKER_PROBE_PAIR])
    except Exception as exc:  # never raises
        logger.warning("reranker probe failed for %s on device %s: %s", model_name, device, exc)
        logger.debug("reranker probe failed for %s on device %s", model_name, device, exc_info=True)
        return False
    return True


def validate_device(
    device: str,
    embedding_model: str,
    reranker_model: str,
    *,
    embedder: EmbedderBackend | None = None,
    reranker: RerankerBackend | None = None,
) -> tuple[bool, bool, list[str]]:
    """Probe the embedder and reranker on *device* (C3).

    Synchronous and never raises — all exceptions are captured as warnings so
    the caller (wizard or :func:`validate_models_async`) can decide on fallback.

    Returns ``(embedder_ok, reranker_ok, warnings)``:

    - Each non-empty model is probed with one inference through the supplied *embedder* /
      *reranker* backend — the serving instances, so no second copy of the weights is
      loaded — or, when none is supplied (the install wizard), through a fresh
      ``ModelEmbedder`` / ``ModelReranker`` the probe loads itself.
    - An explicit ``mps`` / ``cuda`` the host lacks runs on CPU and is reported as the
      warning ``device unavailable: <requested>; models loaded on cpu`` — so a
      successful probe yields ``(True, True, [that warning])``. ``auto`` and ``cpu``
      never warn.
    - A failed probe (including a model name outside the registry) is logged at WARNING
      with the exception message and at DEBUG with its traceback; its warning is fixed
      text, never the exception message (it reaches ``GET /status``).
    - ``reranker_model == ""`` disables the reranker: ``reranker_ok = True`` with
      no probe attempted.
    - ``embedding_model == ""`` skips the embedder probe and reports
      ``embedder_ok = True``.
    """
    warnings: list[str] = []
    step_down = _device_step_down_warning(device)
    if step_down is not None:
        warnings.append(step_down)

    embedder_ok = embedding_model == "" or _probe_embedder(embedding_model, device, embedder)
    if not embedder_ok:
        warnings.append(EMBEDDER_PROBE_FAILED_WARNING)

    reranker_ok = reranker_model == "" or _probe_reranker(reranker_model, device, reranker)
    if not reranker_ok:
        warnings.append(RERANKER_PROBE_FAILED_WARNING)

    return embedder_ok, reranker_ok, warnings


async def validate_models_async(
    config: SearchConfig,
    timeout_seconds: float = 60.0,
    *,
    embedder: EmbedderBackend | None = None,
    reranker: RerankerBackend | None = None,
) -> ModelValidationResult:
    """Validate the configured embedder and reranker without blocking startup (C1).

    Runs :func:`validate_device` in a worker thread guarded by
    ``asyncio.wait_for``. Never raises, with one exception: a timeout or any other
    failure of the embedder/reranker probe yields a result with both ``ok`` flags
    ``False`` and a descriptive warning, rather than propagating. Cancelling the
    calling task (server shutdown) is the exception: ``CancelledError`` propagates,
    from the probe wait and from the pre-probes below alike.

    *embedder* / *reranker* are the serving backends (the pipeline's instances): the probe
    runs those rather than loading throwaway copies, so startup never holds the weights
    twice. A cold backend loads here, serialised on its own load lock with any warm-up
    already building it.

    Also runs the llama-server reachability probe — see
    :func:`_probe_llama_cpp`. This is an independent, short-timeout side check;
    its outcome (``llama_cpp_ok``) is attached to every returned result, including
    the timeout/failure early-return paths below.

    Likewise :func:`graph_ner_status` — dispatched to a thread
    since it does filesystem globbing and package-metadata scanning — and
    :func:`_probe_extraction_model` both run before the probe
    below, and their combined output is PREPENDED to ``provider_warnings`` on every
    return path. Every ``ModelValidationResult`` this function returns is built by the
    local ``_result`` closure below, which always does that prepend — so the
    guarantee is structural, not a convention each return
    site could forget.
    """
    llama_cpp_ok = await _probe_llama_cpp(config)
    graph_warnings = await asyncio.to_thread(graph_ner_status, config)
    graph_warnings = graph_warnings + await _probe_extraction_model(
        config, llama_cpp_ok=llama_cpp_ok
    )

    def _result(
        embedder_ok: bool | None,
        reranker_ok: bool | None,
        extra_warnings: list[str],
    ) -> ModelValidationResult:
        """Build a result with ``graph_warnings`` (incl. the extraction-model probe)
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
                validate_device,
                config.device,
                config.embedding_model,
                config.reranker_model,
                embedder=embedder,
                reranker=reranker,
            ),
            timeout=timeout_seconds,
        )
    except TimeoutError:
        return _result(False, False, [VALIDATION_TIMED_OUT_TEMPLATE.format(seconds=f"{timeout_seconds:.15g}")])
    except asyncio.CancelledError:
        # A cancel requested of THIS task (server shutdown mid-probe) must end the task: the
        # caller (the lifespan's validation task) logs it as a clean shutdown. A
        # CancelledError that did not come from cancelling this task is a failed probe.
        current = asyncio.current_task()
        if current is not None and current.cancelling():
            raise
        logger.warning("model validation failed unexpectedly: cancelled inside the probe", exc_info=True)
        return _result(False, False, [VALIDATION_FAILED_WARNING])
    except Exception as exc:
        logger.warning("model validation failed unexpectedly: %s", exc, exc_info=True)
        return _result(False, False, [VALIDATION_FAILED_WARNING])

    return _result(embedder_ok, reranker_ok, warnings)


async def validate_embedding_model(model_name: str) -> int:
    """Return the output dimension of *model_name* from its spec; nothing is loaded.

    Raises:
        ModelValidationError: ``unsupported model ...; supported: ...`` when
            *model_name* is not in the embedder registry (REST 422 / MCP validation error).
    """
    try:
        return get_embedder_spec(model_name).dim
    except UnsupportedModelError as exc:
        raise ModelValidationError(str(exc)) from exc
