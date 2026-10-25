"""Pre-DONE model warm-up shared by the job tasks whose DONE clients act on by searching.

Factored out so the ingest tasks (``routes_jobs``) and the import task
(``routes_export``) share one bounded, best-effort warm-up without one route module
importing from the other.
"""
from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from archon_search.embedder import Embedder
    from archon_search.pipeline import SearchPipeline

logger = logging.getLogger(__name__)

#: Ceiling on the pre-DONE model warm-up wait in an ingest (S281) or import (S288)
#: task. Generous enough for a cold cross-encoder model build (the "max" profile
#: measured 91 s), bounded so a wedged model download cannot pin a job forever.
JOB_WARMUP_WAIT_SECONDS = 180.0


async def await_models_warm(
    pipeline: SearchPipeline | None, job_id: str, embedder: Embedder | None = None
) -> None:
    """Block until the lazy PyTorch models are built, before an ingest or import reports DONE.

    A job's terminal ``DONE`` is the completion signal clients act on —
    ``archon-search collection add --wait`` polls exactly this, then searches.
    Ingest only ever touches the *embedder*, so ``DONE`` used to be emitted while
    the cross-encoder was still cold; the next ``POST /search`` then awaited
    ``Reranker._warmup_lock`` outside ``_search_budget.SEARCH_TIMEOUT_SECONDS`` and
    produced no response inside any normal client read budget (S281). Absorbing that
    wait here — off the request path, where ``/ready`` already puts it for
    probe-aware clients (S279) — makes a completed job mean what it says.

    An import (``routes_export._import_task``) takes its vectors from the archive,
    so nothing on its path builds the cached embedder serving the imported
    collection; it passes that *embedder* to be warmed in place of the pipeline's
    global one (S288).

    Never fails the job: ``warmup_models`` already swallows model errors, and on a
    timeout the build keeps running in its worker thread while searches that arrive
    before it finishes degrade through ``_search_budget.warmup_for_search`` —
    FTS-only while the embedder is cold, unreranked while the cross-encoder is.
    The content is stored either way.
    """
    if pipeline is None:
        return
    try:
        await asyncio.wait_for(pipeline.warmup_models(embedder), timeout=JOB_WARMUP_WAIT_SECONDS)
    except TimeoutError:
        logger.warning(
            "job %s: model warm-up did not finish within %.0fs; searches on this "
            "collection run degraded (FTS-only or unreranked) until the build completes",
            job_id,
            JOB_WARMUP_WAIT_SECONDS,
        )
    except Exception:
        logger.warning(
            "job %s: model warm-up failed; searches on this collection run degraded "
            "(FTS-only or unreranked) while the model stays unbuilt",
            job_id,
            exc_info=True,
        )
