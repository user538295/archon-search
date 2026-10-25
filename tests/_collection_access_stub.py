"""Let a test drive a mutating route on an app whose lifespan never ran.

``POST /ingest`` and its siblings ask the store whether the caller's namespace may write the collection
(``SearchStore.assert_collection_access``) before they take its lock. That read needs a connected store, which a
``TestClient`` built without ``with`` (no lifespan) never has. A test that is about something else than ownership
(a size limit, a path policy, a body cap) answers the question "yes" here; the ownership rules have their own tests.
"""
from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI

from archon_search.store import SearchStore


def allow_collection_access(app: FastAPI) -> FastAPI:
    """Make *app*'s store grant every namespace access to every collection; returns *app*."""
    app.state.search_store.assert_collection_access = AsyncMock()
    return app


def allow_collection_access_everywhere(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make every ``SearchStore`` grant every namespace access to every collection, for the test's duration."""
    monkeypatch.setattr(SearchStore, "assert_collection_access", AsyncMock())
