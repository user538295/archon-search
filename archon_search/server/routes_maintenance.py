"""POST /maintenance/trigger — manually trigger a maintenance pass (D5 BE-4).

Sets ``_trigger_event`` on ``MaintenanceLoop``; the loop runs the pass
asynchronously. Returns 202 Accepted with ``{"status": "triggered"}`` or
``{"status": "already_triggered"}`` when a trigger is already pending or a
pass is already running.
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Request

from archon_search.server.schemas import ErrorDetail, MaintenanceTriggerResponse

logger = logging.getLogger(__name__)

router = APIRouter(tags=["maintenance"])


@router.post(
    "/maintenance/trigger",
    status_code=202,
    response_model=MaintenanceTriggerResponse,
    responses={401: {"model": ErrorDetail}},
)
async def trigger_maintenance(request: Request) -> MaintenanceTriggerResponse:
    """Set the maintenance trigger event; the loop fires a pass asynchronously.

    If the trigger event is already set (a trigger is pending or a pass is
    running), the request is acknowledged but the pass is not duplicated.
    ``"already_triggered"`` is used (not ``"already_running"``) because
    ``_trigger_event.is_set()`` means "pending or running", not "definitely
    running".
    """
    maintenance_loop = getattr(request.app.state, "maintenance_loop", None)
    if maintenance_loop is None:
        # Defensive: should never happen in production — MaintenanceLoop is
        # always wired in lifespan. Treat as already_triggered.
        logger.warning("trigger_maintenance: no maintenance_loop on app.state")
        return MaintenanceTriggerResponse(status="already_triggered")

    if maintenance_loop._trigger_event.is_set():
        return MaintenanceTriggerResponse(status="already_triggered")

    maintenance_loop._trigger_event.set()
    return MaintenanceTriggerResponse(status="triggered")
