"""GET /indexing-state endpoint — raw indexing state for machine consumers."""
from __future__ import annotations

from fastapi import APIRouter, Request

from archon_search.progress import to_dict

router = APIRouter()

_COLLECTION_API_FIELDS = {"status", "processed_files", "total_files", "error", "error_count", "started_at", "completed_at"}


@router.get("/indexing-state")
def indexing_state(request: Request) -> dict:
    """Return raw indexing state. Returns {} when no state file exists."""
    state_store = request.app.state.state_store
    state = state_store.read()
    if state is None:
        return {}
    raw = to_dict(state)
    raw["collections"] = {
        name: {k: v for k, v in col.items() if k in _COLLECTION_API_FIELDS}
        for name, col in raw["collections"].items()
    }
    return raw
