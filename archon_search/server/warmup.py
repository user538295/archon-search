"""Background startup model warm-up, run by the server lifespan (S184/S279/S632)."""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Iterator
from typing import TYPE_CHECKING

from archon_search.server.schemas import WarmupResult

if TYPE_CHECKING:
    from archon_search.config import SearchConfig
    from archon_search.embedder_cache import EmbedderCache
    from archon_search.pipeline import SearchPipeline

logger = logging.getLogger(__name__)

# Terminal ceiling for the model warm-up task. A cold cache legitimately
# takes minutes (model load + download), but an unbounded await would pin
# ``app.state.warmup_result`` at "pending" — and ``GET /ready`` at 503 — forever.
_WARMUP_TASK_TIMEOUT_SECONDS: float = 600.0

# Background recovery after a failed startup warm-up (H2). The first retry waits
# ``_RECOVERY_INITIAL_DELAY_SECONDS``; each later wait multiplies by
# ``_RECOVERY_BACKOFF_FACTOR`` up to ``_RECOVERY_MAX_DELAY_SECONDS``, then stays there until
# the default models are warm: a transient Hugging Face / network failure heals without
# traffic, and a permanent one costs one cheap probe every five minutes.
_RECOVERY_INITIAL_DELAY_SECONDS: float = 5.0
_RECOVERY_BACKOFF_FACTOR: float = 2.0
_RECOVERY_MAX_DELAY_SECONDS: float = 300.0


def default_models_warm(pipeline: SearchPipeline, config: SearchConfig) -> bool:
    """True when the default embedder and, if one is configured, the reranker are warm.

    The readiness gate: a replica without them cannot embed or rank. An empty
    ``reranker_model`` disables the reranker on purpose, so it is never required.
    """
    if not pipeline.embedder_is_warm:
        return False
    return not config.reranker_model or pipeline.reranker_is_warm


def _failed_models(
    pipeline: SearchPipeline, config: SearchConfig, embedder_failures: list[str]
) -> list[str]:
    """Return every model the warm-up failed to warm, each named once.

    Order is deterministic: the global embedder, then *embedder_failures* in the order
    given, then the reranker. The global embedder and its preload entry share
    ``config.embedding_model``, so a failed default embedder is named only once.
    """
    failed: list[str] = []
    if not pipeline.embedder_is_warm:
        failed.append(config.embedding_model)
    failed.extend(embedder_failures)
    if config.reranker_model and not pipeline.reranker_is_warm:
        failed.append(config.reranker_model)
    return list(dict.fromkeys(failed))


def _still_cold_models(
    *,
    pipeline: SearchPipeline,
    embedder_cache: EmbedderCache,
    config: SearchConfig,
    model_names: list[str],
) -> list[str]:
    """Return every model the warm-up covers that is cold right now, each named once.

    A state probe for a warm-up cut short by its timeout, where ``preload`` never
    returned its outcome. It reads the cache, so it also names a model the LRU evicted
    after warming. Same order as the failure list: global embedder, *model_names*, reranker.
    """
    return _failed_models(pipeline, config, embedder_cache.cold_models(model_names))


def _embedding_models_label(count: int) -> str:
    return f"{count} embedding model{'' if count == 1 else 's'}"


async def run_model_warmup(
    *,
    set_result: Callable[[WarmupResult], None],
    pipeline: SearchPipeline,
    embedder_cache: EmbedderCache,
    config: SearchConfig,
    model_names: list[str],
) -> None:
    """Warm the models the first search needs and report the outcome through *set_result*.

    Never raises except ``CancelledError`` (shutdown) and the interpreter's own ``KeyboardInterrupt`` /
    ``SystemExit``. Every loader it drives logs and
    swallows a model that cannot load, so announcing "complete" unconditionally hid an
    invalid ``embedding_model`` / ``reranker_model`` from the log (S632). The outcome
    comes from the pipeline's warm state and ``preload``'s returned failures; only a
    timed-out run, where ``preload`` never returned, falls back to probing cache state.
    """
    try:
        try:
            # A wedged model load must not leave warmup_result "pending"
            # forever — /ready would answer 503 for the process lifetime
            # with no diagnostic and no recovery.
            async with asyncio.timeout(_WARMUP_TASK_TIMEOUT_SECONDS):
                # warmup_models() builds the global embedder, then the cross-encoder —
                # both before preload() (S279): the cross-encoder is a single small model
                # that every search pays for on the request path, whereas preload() may
                # build several large embedders. Warming it after them let a slow
                # embedder fetch (e.g. the "max" profile's ~1.3 GB BAAI/bge-large-en-v1.5)
                # consume the whole budget, so the reranker stayed cold and the first
                # search paid its 91 s model build inline.
                await pipeline.warmup_models()
                # Only the per-collection embedders are still cold; they are loaded lazily, on first use.
                embedder_failures = await embedder_cache.preload(model_names)
        except TimeoutError:
            cold = _still_cold_models(
                pipeline=pipeline, embedder_cache=embedder_cache, config=config, model_names=model_names
            )
            logger.warning(
                "model warm-up failed: timed out after %ss%s",
                _WARMUP_TASK_TIMEOUT_SECONDS,
                f"; still cold: {', '.join(cold)}" if cold else "",
            )
            set_result(WarmupResult.FAILED)
            return
        failed = _failed_models(pipeline, config, embedder_failures)
        if failed:
            logger.warning("model warm-up failed: %s", ", ".join(failed))
            set_result(WarmupResult.FAILED)
        else:
            # Startup returning no longer implies warm-up finished, so this
            # is the only signal an operator has that it completed at all.
            logger.info(
                "model warm-up complete (%s%s)",
                _embedding_models_label(len(model_names)),
                " + reranker" if config.reranker_model else "",
            )
            set_result(WarmupResult.DONE)
    except asyncio.CancelledError:
        logger.info("model warm-up cancelled during shutdown")
        set_result(WarmupResult.FAILED)
        raise
    except Exception as exc:  # never let the task escape; KeyboardInterrupt/SystemExit still propagate
        logger.warning("model warm-up failed: %s", exc, exc_info=True)
        set_result(WarmupResult.FAILED)


def _backoff_delays() -> Iterator[float]:
    """Yield the recovery waits: initial, then growing by the factor up to the cap, forever."""
    delay = _RECOVERY_INITIAL_DELAY_SECONDS
    while True:
        yield delay
        delay = min(delay * _RECOVERY_BACKOFF_FACTOR, _RECOVERY_MAX_DELAY_SECONDS)


async def _retry_default_models(pipeline: SearchPipeline) -> None:
    """One bounded retry of the default embedder and reranker; a failure is logged, never raised."""
    try:
        async with asyncio.timeout(_WARMUP_TASK_TIMEOUT_SECONDS):
            await pipeline.warmup_models()
    except Exception:  # the next backoff round retries
        logger.warning("model warm-up retry failed", exc_info=True)


async def run_warmup_recovery(
    *,
    set_result: Callable[[WarmupResult], None],
    pipeline: SearchPipeline,
    embedder_cache: EmbedderCache,
    config: SearchConfig,
    model_names: list[str],
    on_recovered: Callable[[], Awaitable[None]],
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> None:
    """Retry a failed startup warm-up in the background until the default models are warm (H2).

    Run it after :func:`run_model_warmup` finished ``FAILED``. Returns at once when the
    default embedder (and a configured reranker) are already warm, so a failure confined to
    a per-collection model is left to the search that needs it. Otherwise it waits with
    bounded exponential backoff and retries ``pipeline.warmup_models()``; a search or ingest
    that warms the models meanwhile also ends the loop. Once they are warm it reloads the
    per-collection models, reports ``DONE`` if every one warmed (otherwise the result stays
    ``FAILED``) and awaits *on_recovered* so the model check is re-run on the healed models.

    The default embedder is not preloaded through *embedder_cache*: it is the pipeline's own
    instance, and a second cached copy would hold its weights twice. Runs until healed or
    cancelled (shutdown); cancellation propagates.
    """
    if default_models_warm(pipeline, config):
        return
    for delay in _backoff_delays():
        await sleep(delay)
        if not default_models_warm(pipeline, config):
            await _retry_default_models(pipeline)
        if default_models_warm(pipeline, config):
            break
    logger.info("model warm-up recovered: the default models are warm")
    collection_models = [model for model in model_names if model != config.embedding_model]
    failed: list[str] = []
    if collection_models:
        try:
            async with asyncio.timeout(_WARMUP_TASK_TIMEOUT_SECONDS):
                failed = await embedder_cache.preload(collection_models)
        except TimeoutError:
            failed = embedder_cache.cold_models(collection_models)
    if failed:
        logger.warning("model warm-up still failing after recovery: %s", ", ".join(failed))
    else:
        set_result(WarmupResult.DONE)
    try:
        await on_recovered()
    except Exception:  # the healed models serve regardless
        logger.warning("model re-validation after warm-up recovery failed", exc_info=True)
