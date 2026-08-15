"""GET /ready — unauthenticated readiness probe."""
from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from archon_search.server.schemas import CheckStatus, ReadinessChecks, ReadinessResponse, WarmupResult

router = APIRouter()


def _warmup_pending(request: Request) -> bool:
    """True while eager model warm-up is still running.

    ``model_validation`` only probes that the models are *resolvable* (seconds);
    eager warm-up builds the ONNX weights (minutes). Only the latter means the
    first real search would block, so it is the one signal that gates readiness.
    The ``getattr`` guards keep the endpoint resilient to app factories that set
    neither attribute.
    """
    config = getattr(request.app.state, "config", None)
    return (
        config is not None
        and getattr(config, "eager_load_embedders", False)
        and getattr(request.app.state, "warmup_result", None) == WarmupResult.PENDING
    )


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
    from one that completed cleanly. ``OK`` otherwise. The ``getattr`` guard
    keeps the endpoint resilient to app factories that never set
    ``_startup_sync_failed``.

    ``ready_flag`` must NOT gate on a ``FAIL`` here: the startup sync's failure
    is deliberately swallowed so a corrupted collection cannot wedge the pod's
    readiness forever — the check is informational only.
    """
    if sync_pending:
        return CheckStatus.PENDING
    if getattr(request.app.state, "_startup_sync_failed", False):
        return CheckStatus.FAIL
    return CheckStatus.OK


def _model_check_status(request: Request, warmup_pending: bool) -> CheckStatus:
    """Map ``app.state.model_validation`` to a ``CheckStatus`` for ``checks.models``.

    Priority is strict (D6 BE-6): FAIL (either model could not load) > WARN (both
    loaded but a provider fallback warning was emitted) > OK (both loaded, no
    warnings). The check is ``PENDING`` when the background task has not produced a
    result yet — either ``app.state.model_validation`` is ``None``, or a result
    exists but a probe flag is still unset (``None``). Each branch asserts its
    condition positively so a partially-populated result can never read as OK/WARN.
    The ``getattr`` guard keeps the endpoint resilient to app factories that never
    set ``app.state.model_validation``.

    Eager warm-up outranks everything: while it is pending the models are not
    usable yet no matter what ``model_validation`` already concluded.
    ``warmup_pending`` is a required argument — the caller evaluates
    ``_warmup_pending`` exactly once per request and passes the result in, so
    ``app.state.warmup_result`` is never read twice: two reads are not atomic and
    a warm-up finishing between them would yield ``models: pending`` with
    ``ready: true``.

    A *failed* eager warm-up also outranks ``model_validation``: mirrors
    ``_sync_check_status``, which gained an equivalent ``FAIL`` state for a
    failed startup sync — without it, a failed warm-up is indistinguishable
    from a healthy one once ``model_validation``'s cheap resolvability probe
    (seconds) reports OK, because that probe never proves the ONNX weights
    were actually built (minutes). This does NOT gate ``ready`` — see the
    docstring on ``ready()`` — only the diagnostic.
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


@router.get(
    "/ready",
    responses={
        200: {"model": ReadinessResponse},
        503: {"model": ReadinessResponse},
    },
)
async def ready(request: Request) -> JSONResponse:
    store = request.app.state.search_store
    storage_ok = await store.ping()
    warmup_pending = _warmup_pending(request)
    models_status = _model_check_status(request, warmup_pending)
    sync_pending = _startup_sync_pending(request)
    # Readiness gates on storage, eager warm-up and the startup sync. A failed/warned
    # model_validation stays informational (the lazy-load contract means a search can
    # still succeed), but a pending eager warm-up means the first search would block
    # on ONNX construction, and a running startup sync means the index is still being
    # (re)built — a load balancer must not route real traffic here in either case.
    ready_flag = storage_ok and not warmup_pending and not sync_pending
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
