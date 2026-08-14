"""GET /ready — unauthenticated readiness probe."""
from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from archon_search.server.schemas import CheckStatus, ReadinessChecks, ReadinessResponse

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
        and getattr(request.app.state, "warmup_result", None) == "pending"
    )


def _model_check_status(request: Request) -> CheckStatus:
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
    """
    if _warmup_pending(request):
        return CheckStatus.PENDING

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
    models_status = _model_check_status(request)
    # Readiness gates on storage plus eager warm-up only. A failed/warned
    # model_validation stays informational (the lazy-load contract means a search can
    # still succeed), but a pending eager warm-up means the first search would block
    # on ONNX construction — a load balancer must not route real traffic here yet.
    ready_flag = storage_ok and not _warmup_pending(request)
    body = ReadinessResponse(
        ready=ready_flag,
        checks=ReadinessChecks(
            storage=CheckStatus.OK if storage_ok else CheckStatus.FAIL,
            models=models_status,
        ),
    )
    status_code = 200 if ready_flag else 503
    return JSONResponse(body.model_dump(mode="json"), status_code=status_code)
