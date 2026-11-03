"""BE-11 on the real app: the REST multi-collection branch runs on ``SearchService`` behind the real route and service wiring.

Run with:
    uv run pytest tests/integration/test_be11_rest_search_multi_path.py --no-cov
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from archon_search.pipeline import (
    METADATA_STORE_UNAVAILABLE_CODE,
    METADATA_STORE_UNAVAILABLE_DETAIL,
    CollectionNotFoundError,
    FanoutTimeoutError,
    MetadataLookupError,
    SearchPipelineResult,
)
from archon_search.services.service_error import (
    COLLECTION_NOT_FOUND_DETAIL,
    fanout_exceeded_detail,
)
from archon_search.telemetry.writer import TelemetryWriter
from tests.integration.conftest import make_real_app

pytestmark = pytest.mark.integration

_MULTI_BODY = {"collections": ["a", "b"], "query": "q"}
_TIMEOUT_DETAIL = "Search timed out"
_MAX_FANOUT = 2
_FANOUT_EXCEEDED_DETAIL = "collections length exceeds maximum of 2"


def _recording_writer() -> MagicMock:
    """A writer stand-in that records ``enqueue`` and survives the lifespan's ``await drain_and_stop()``."""
    writer = MagicMock(spec=TelemetryWriter)
    writer.drain_and_stop = AsyncMock()
    return writer


def _fail_fan_out_with(client, exc: Exception) -> MagicMock:  # type: ignore[no-untyped-def]
    """Replace the pipeline's models warm-up and ``search_many`` so the fan-out raises *exc*; return the writer."""
    writer = _recording_writer()
    client.app.state.telemetry_writer = writer
    client.app.state.pipeline.warmup_models = AsyncMock(return_value=True)
    client.app.state.pipeline.search_many = AsyncMock(side_effect=exc)
    return writer


def test_rest_multi_not_found_metadata_error_timeout_bodies_are_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """404 plain detail, 503 coded, 504 timed out: the bodies of the old multi branch, now rendered from the service."""
    with make_real_app(tmp_path, monkeypatch) as (client, _cfg, api_key):
        headers = {"Authorization": f"Bearer {api_key}"}

        writer = _fail_fan_out_with(client, CollectionNotFoundError(["b"]))
        response = client.post("/search", json=_MULTI_BODY, headers=headers)
        assert (response.status_code, response.json()) == (404, {"detail": COLLECTION_NOT_FOUND_DETAIL})
        writer.enqueue.assert_not_called()

        writer = _fail_fan_out_with(client, MetadataLookupError(RuntimeError("lancedb is down")))
        response = client.post("/search", json=_MULTI_BODY, headers=headers)
        assert response.status_code == 503
        assert response.json() == {"detail": METADATA_STORE_UNAVAILABLE_DETAIL, "code": METADATA_STORE_UNAVAILABLE_CODE}
        assert "lancedb is down" not in response.text
        writer.enqueue.assert_called_once()
        entry = writer.enqueue.call_args.args[0]
        assert (entry.endpoint, entry.status, entry.error_kind) == ("search", "internal_error", "other")

        writer = _fail_fan_out_with(client, FanoutTimeoutError())
        response = client.post("/search", json=_MULTI_BODY, headers=headers)
        assert (response.status_code, response.json()) == (504, {"detail": _TIMEOUT_DETAIL})
        writer.enqueue.assert_called_once()
        entry = writer.enqueue.call_args.args[0]
        assert (entry.endpoint, entry.status, entry.error_kind) == ("search", "timeout", "timeout")


def test_rest_fanout_above_max_is_the_same_422_text_on_both_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The fan-out limit is one message from the service's normaliser; a list within the limit is not refused."""
    with make_real_app(tmp_path, monkeypatch, max_fanout=_MAX_FANOUT) as (client, _cfg, api_key):
        headers = {"Authorization": f"Bearer {api_key}"}
        pipeline = client.app.state.pipeline
        pipeline.warmup_models = AsyncMock(return_value=True)
        pipeline.search_many = AsyncMock(return_value=SearchPipelineResult(results=[], acl_filtered=False))

        response = client.post(
            "/search", json={"collections": ["a", "b", "c"], "query": "q"}, headers=headers
        )
        assert (response.status_code, response.json()) == (422, {"detail": _FANOUT_EXCEEDED_DETAIL})
        assert _FANOUT_EXCEEDED_DETAIL == fanout_exceeded_detail(_MAX_FANOUT)
        pipeline.search_many.assert_not_called()

        response = client.post("/search", json=_MULTI_BODY, headers=headers)
        assert response.status_code == 200
        pipeline.search_many.assert_called_once()
