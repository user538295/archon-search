"""GET /indexing-state endpoint — raw indexing state for machine consumers."""
from __future__ import annotations

from fastapi import APIRouter, Request

from archon_search.progress import to_dict

router = APIRouter()

_COLLECTION_API_FIELDS = {"status", "processed_files", "total_files", "error", "error_count", "started_at", "completed_at"}


@router.get("/indexing-state")
async def indexing_state(request: Request) -> dict:
    """Return raw indexing state filtered to the caller's namespace. Returns {} when no state file exists."""
    ns: str = request.state.namespace
    state_store = request.app.state.state_store
    state = state_store.read()
    if state is None:
        return {}

    search_store = request.app.state.search_store
    all_meta = await search_store.get_all_collections_meta()
    ns_names: set[str] = {m.name for m in all_meta if m.namespace == ns}

    raw = to_dict(state)
    raw["collections"] = {
        name: {k: v for k, v in col.items() if k in _COLLECTION_API_FIELDS}
        for name, col in raw["collections"].items()
        if name in ns_names
    }
    return raw
