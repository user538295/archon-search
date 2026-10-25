"""GET /ready — unauthenticated readiness probe."""
from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from archon_search.server.schemas import (
    CheckStatus,
    ReadinessChecks,
    ReadinessResponse,
    StartupSyncResult,
    WarmupResult,
)
from archon_search.server.warmup import default_models_warm

router = APIRouter()


def _warmup_pending(request: Request) -> bool:
    """True while model warm-up is still running.

    Warm-up builds the PyTorch models (minutes); until it finishes the models are
    not usable yet and search runs degraded (``_search_budget.warmup_for_search``),
    so ``_model_check_status`` reports ``PENDING`` and readiness is gated through it.

    This must NOT be conditioned on ``config.eager_load_embedders`` (S279): that
    flag only widens warm-up's scope from the default embedder to every
    per-collection model, and gating on it left ``/ready`` blind for every
    deployment that did not opt in — reporting ``ready: true`` while the first
    search still paid the full cold cross-encoder build. ``warmup_result`` is
    ``None`` unless the lifespan actually started a warm-up task; the ``getattr``
    guard keeps the endpoint from raising for app factories that never set it —
    such an app falls through to ``model_validation``, which reads ``PENDING`` (so
    ``/ready`` answers ``503``) until a result is set (S631).
    """
    return getattr(request.app.state, "warmup_result", None) == WarmupResult.PENDING


def _startup_sync_pending(request: Request) -> bool:
    """True while the lifespan's startup sync task is still running.

    During the startup sync the index is still being (re)built, so search
    results are incomplete — a load balancer must not route real traffic here
    yet. The ``getattr`` guard keeps the endpoint resilient to app factories
    that never set the attribute.
    """
    task = getattr(request.app.state, "_startup_sync_task", None)
    return task is not None and not task.done()


def _sync_check_status(request: Request, sync_pending: bool) -> CheckStatus:
    """Map startup-sync progress to a ``CheckStatus`` for ``checks.sync``.

    ``PENDING`` while the task is still running. ``FAIL`` once it has finished
    but failed (``app.state._startup_sync_failed``, set by the lifespan either
    when the sync raises — the swallow-all ``except BaseException`` branch —
    or when it returns normally with a non-empty ``SyncResult.errors``, the
    common failure mode) — without this, a failed sync is indistinguishable
    from one that completed cleanly. ``WARN`` when the lifespan's crash-loop
    guard suppressed the sync entirely (``sync_result == SUPPRESSED``): no task
    ever ran, so neither ``PENDING`` nor ``_startup_sync_failed`` fires, and a
    bare ``OK`` would report an all-clear over a potentially stale index.
    ``OK`` otherwise. Priority mirrors ``_model_check_status``: FAIL > WARN > OK.
    The ``getattr`` guards keep the endpoint resilient to app factories that
    never set ``_startup_sync_failed`` / ``sync_result``.

    ``ready_flag`` must NOT gate on a ``FAIL`` here: the startup sync's failure
    is deliberately swallowed so a corrupted collection cannot wedge the pod's
    readiness forever — the check is informational only.
    """
    if sync_pending:
        return CheckStatus.PENDING
    if getattr(request.app.state, "_startup_sync_failed", False):
        return CheckStatus.FAIL
    if getattr(request.app.state, "sync_result", None) == StartupSyncResult.SUPPRESSED:
        return CheckStatus.WARN
    return CheckStatus.OK


def _model_check_status(request: Request, warmup_pending: bool) -> CheckStatus:
    """Map ``app.state.model_validation`` to a ``CheckStatus`` for ``checks.models``.

    Priority is strict (D6 BE-6): FAIL (either model could not load) > WARN (both
    loaded but a warning was emitted — e.g. the configured device is unavailable so they
    loaded on CPU, S635, or the graph NER model is absent) > OK (both
    loaded, no warnings). The check is ``PENDING`` when the background task has not
    produced a result yet — either ``app.state.model_validation`` is ``None``, or a result
    exists but a probe flag is still unset (``None``). Each branch asserts its
    condition positively so a partially-populated result can never read as OK/WARN.
    The ``getattr`` guard keeps the endpoint from raising for app factories that never
    set ``app.state.model_validation`` — such an app reads ``PENDING``, so ``/ready``
    answers ``503`` (S631).

    Warm-up outranks everything: while it is pending the models are not
    usable yet no matter what ``model_validation`` already concluded.
    ``PENDING`` — from either source — gates ``ready`` (S631); ``WARN``/``FAIL`` gate it only
    through ``_default_models_unavailable``.
    ``warmup_pending`` is a required argument — the caller evaluates
    ``_warmup_pending`` exactly once per request and passes the result in, so one
    read of ``app.state.warmup_result`` decides ``PENDING``: two reads are not atomic,
    and a single deciding read keeps ``checks.models`` and ``ready`` (derived from it)
    consistent within one response. The later ``FAILED`` read below is safe because
    terminal warm-up states never return to pending.

    A *failed* warm-up also outranks ``model_validation``: mirrors
    ``_sync_check_status``, which gained an equivalent ``FAIL`` state for a
    failed startup sync. The warm-up task (``server/warmup.py``) sets
    ``WarmupResult.FAILED`` when it raised or timed out, *or* when it finished with
    any model it covered failing to load or warm (S632 — the WARNING log names them).
    Without this branch such a failure is indistinguishable from a healthy warm-up once
    ``model_validation`` reports OK, because validation builds and runs its own
    throwaway probe instances — it never proves the instances search actually uses
    were built. On its own this does not gate ``ready`` — ``_default_models_unavailable``
    does, for the default embedder and a configured reranker only.
    """
    if warmup_pending:
        return CheckStatus.PENDING
    if getattr(request.app.state, "warmup_result", None) == WarmupResult.FAILED:
        return CheckStatus.FAIL

    result = getattr(request.app.state, "model_validation", None)
    if result is None:
        return CheckStatus.PENDING
    if result.embedder_ok is False or result.reranker_ok is False:
        return CheckStatus.FAIL
    if result.embedder_ok is True and result.reranker_ok is True:
        return CheckStatus.WARN if result.provider_warnings else CheckStatus.OK
    # A non-None result with an unset (None) probe flag — validation incomplete.
    return CheckStatus.PENDING


def _default_models_unavailable(request: Request) -> bool:
    """True once warm-up failed and the default embedder or a configured reranker is not loaded.

    A replica whose default embedder cannot load cannot embed: every ingest fails and every
    search runs FTS-only, so it must not receive traffic. The same holds for a configured
    reranker (``reranker_model`` non-empty), whose absence silently degrades ranking.

    Deliberately narrow:

    * Only a *failed* warm-up can leave them unloaded. ``DONE`` means both are warm, and a
      pending warm-up already gates through ``checks.models: pending``.
    * The deciding signal is the live warm state of the instances search uses
      (``embedder_is_warm`` / ``reranker_is_warm``, both side-effect-free), not
      ``model_validation``: its probes load throwaway instances, and its timeout and
      crash paths report both flags false on a merely slow host that warmed fine.
    * ``WarmupResult.FAILED`` also covers a per-collection model that failed to preload
      and a warm-up that timed out while the defaults were already warm. Neither makes
      the replica unfit, so neither gates; only the defaults do.
    * An empty ``reranker_model`` disables the reranker on purpose: it is never required,
      so it cannot wedge readiness.
    * Because the state is read live, a default model that finishes building after the
      warm-up gave up clears the gate without a restart.
    * ``run_warmup_recovery`` retries the default models in the background after a failed
      warm-up, so the gate clears on its own once a transient failure is over (H2).
    """
    if getattr(request.app.state, "warmup_result", None) != WarmupResult.FAILED:
        return False
    return not default_models_warm(request.app.state.pipeline, request.app.state.config)


@router.get(
    "/ready",
    responses={
        200: {"model": ReadinessResponse},
        503: {"model": ReadinessResponse},
    },
)
async def ready(request: Request) -> JSONResponse:
    """Readiness probe: ``200`` once the storage ping succeeds, the models check is no
    longer ``pending`` and the default models serve, and the startup sync is not
    ``pending``; ``503`` otherwise.

    ``checks.models`` stays ``pending`` until model warm-up and the background model
    validation finish (a failed warm-up ends it as ``fail``). A finished warm-up that left
    the default embedder, or a configured reranker, unloaded also answers ``503``: that
    replica cannot embed, so a load balancer must not route to it. Other ``warn``/``fail``
    model results and the sync ``fail``/``warn`` results do not gate readiness — search
    still answers, degraded.
    """
    store = request.app.state.search_store
    storage_ok = await store.ping()
    warmup_pending = _warmup_pending(request)
    models_status = _model_check_status(request, warmup_pending)
    sync_pending = _startup_sync_pending(request)
    # Readiness gates on storage, a pending models check, the default models after a failed
    # warm-up, and the startup sync.
    # ``models: pending`` covers both a running warm-up (the models are not usable yet,
    # so search runs degraded) and a model validation that has not finished yet (S631) —
    # never report ready while our own check is pending. Validation never raises; its
    # bound is the pre-probes (llama-server reachability, graph NER importability
    # check, and the extraction-model probe only when configured) plus
    # ``validation_timeout_seconds`` for the embedder/reranker probe
    # (``model_validation.validate_models_async``).
    # A running startup sync means the index is still being (re)built. Any other WARN/FAIL
    # models stay informational: search still answers, degraded — FTS-only while a
    # per-collection query embedder is cold, unreranked while the cross-encoder is
    # (``_search_budget.warmup_for_search``).
    ready_flag = (
        storage_ok
        and models_status is not CheckStatus.PENDING
        and not _default_models_unavailable(request)
        and not sync_pending
    )
    body = ReadinessResponse(
        ready=ready_flag,
        checks=ReadinessChecks(
            storage=CheckStatus.OK if storage_ok else CheckStatus.FAIL,
            models=models_status,
            sync=_sync_check_status(request, sync_pending),
        ),
    )
    status_code = 200 if ready_flag else 503
    return JSONResponse(body.model_dump(mode="json"), status_code=status_code)
