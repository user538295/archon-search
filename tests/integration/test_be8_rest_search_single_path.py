"""BE-8 on the real app: the REST single-collection branch runs on ``SearchService`` over the real store.

Run with:
    uv run pytest tests/integration/test_be8_rest_search_single_path.py --no-cov
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from archon_search.pipeline import (
    METADATA_STORE_UNAVAILABLE_CODE,
    METADATA_STORE_UNAVAILABLE_DETAIL,
)
from archon_search.services.service_error import COLLECTION_NOT_FOUND_DETAIL
from archon_search.telemetry.writer import TelemetryWriter
from tests.integration.conftest import make_real_app

pytestmark = pytest.mark.integration


def _recording_writer() -> MagicMock:
    """A writer stand-in that records ``enqueue`` and survives the lifespan's ``await drain_and_stop()``."""
    writer = MagicMock(spec=TelemetryWriter)
    writer.drain_and_stop = AsyncMock()
    return writer


def test_search_unknown_and_invalid_collection_name_is_404_against_the_real_store(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """S11: ``bad name!`` is a 404 (it was a 503), like an unknown name, and records nothing."""
    with make_real_app(tmp_path, monkeypatch) as (client, _cfg, api_key):
        writer = _recording_writer()
        client.app.state.telemetry_writer = writer
        headers = {"Authorization": f"Bearer {api_key}"}

        for name in ("no-such-collection", "bad name!"):
            response = client.post("/search", json={"collection": name, "query": "q"}, headers=headers)
            assert (response.status_code, response.json()) == (404, {"detail": COLLECTION_NOT_FOUND_DETAIL}), name

        writer.enqueue.assert_not_called()


def test_search_metadata_failure_is_one_recorded_internal_error_entry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A store failure is a 503 ``metadata_store_error`` plus exactly one ``internal_error``/``other`` entry."""
    with make_real_app(tmp_path, monkeypatch) as (client, _cfg, api_key):
        writer = _recording_writer()
        client.app.state.telemetry_writer = writer
        client.app.state.pipeline.get_collection_meta = AsyncMock(side_effect=RuntimeError("lancedb is down"))

        response = client.post(
            "/search",
            json={"collection": "col", "query": "q"},
            headers={"Authorization": f"Bearer {api_key}"},
        )

        assert response.status_code == 503
        assert response.json() == {
            "detail": METADATA_STORE_UNAVAILABLE_DETAIL,
            "code": METADATA_STORE_UNAVAILABLE_CODE,
        }
        assert "lancedb is down" not in response.text
        writer.enqueue.assert_called_once()
        entry = writer.enqueue.call_args.args[0]
        assert (entry.endpoint, entry.status, entry.error_kind) == ("search", "internal_error", "other")
