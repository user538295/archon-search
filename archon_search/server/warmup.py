"""Background startup model warm-up, run by the server lifespan (S184/S279/S632)."""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from typing import TYPE_CHECKING

from archon_search.server.schemas import WarmupResult

if TYPE_CHECKING:
    from archon_search.config import SearchConfig
    from archon_search.embedder_cache import EmbedderCache
    from archon_search.pipeline import SearchPipeline

logger = logging.getLogger(__name__)

# Terminal ceiling for the model warm-up task. A cold cache legitimately
# takes minutes (ONNX build + download), but an unbounded await would pin
# ``app.state.warmup_result`` at "pending" — and ``GET /ready`` at 503 — forever.
_WARMUP_TASK_TIMEOUT_SECONDS: float = 600.0


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

    Never raises except ``CancelledError`` (shutdown). Every loader it drives logs and
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
                # search paid its 91 s ONNX build inline.
                await pipeline.warmup_models()
                # Only the equally lazy embedders are still cold (S184).
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
    except BaseException as exc:  # noqa: BLE001 — never let the task escape
        logger.warning("model warm-up failed: %s", exc, exc_info=True)
        set_result(WarmupResult.FAILED)
