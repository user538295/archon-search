"""S308: a multi-collection ``POST /search`` fan-out carrying a filter must answer
``200`` and echo one shared, normalised ``applied_filters`` object.

Bug (``Documentation/Backlog/S308-fanout_with_filter_returns_200.md``). The
reporter ingested ``.md`` documents mentioning "widget" into ``s308_alpha`` and
``s308_beta`` and then issued
``POST /search {"collections": [...], "query": "widget", "filters": {"file_type": ".md"}}``,
recording status ``0`` — a connection-level failure, not an HTTP response.

Documented contract (``Documentation/Architecture/600_api_reference_or_public_interface.md``):
a fan-out returns ``200``; a collection that cannot be searched is reported in
``excluded_collections[]`` rather than failing the request; ``applied_filters``
echoes the parsed, normalised ``SearchFilters`` — one flat object shared by every
leg, never keyed per collection.

Run with:
    uv run pytest tests/integration/test_s308_fanout_filter_applied_filters.py -v --no-cov
"""
from __future__ import annotations

from pathlib import Path

import pytest

from tests.integration.conftest import ingest_doc, make_real_app

pytestmark = pytest.mark.integration

_ALPHA = "s308_alpha"
_BETA = "s308_beta"

_ALPHA_TEXT = (
    "The widget assembly line calibrates every widget before packing. A widget "
    "that fails calibration is returned to the widget bench for rework."
)
_BETA_TEXT = (
    "Widget inventory is counted nightly. Each widget carries a serial number so "
    "a missing widget can be traced back to its widget batch."
)

#: The submitted value; the documented echo is the normalised form below.
_SUBMITTED_FILE_TYPE = ".md"
_NORMALISED_FILE_TYPE = "md"


def test_s308_fanout_with_filter_returns_shared_normalised_applied_filters(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Fan-out over two collections with ``filters.file_type='.md'``."""
    with make_real_app(tmp_path, monkeypatch) as (client, _cfg, api_key):
        headers = {"Authorization": f"Bearer {api_key}"}
        ingest_doc(client, _ALPHA, _ALPHA_TEXT, "/corpus/s308_alpha/widgets.md", api_key=api_key)
        ingest_doc(client, _BETA, _BETA_TEXT, "/corpus/s308_beta/widgets.md", api_key=api_key)

        resp = client.post(
            "/search",
            json={
                "collections": [_ALPHA, _BETA],
                "query": "widget",
                "filters": {"file_type": _SUBMITTED_FILE_TYPE},
            },
            headers=headers,
        )

        # 1. the fan-out answers at all
        assert resp.status_code == 200, f"fan-out search failed: {resp.status_code} {resp.text}"
        body = resp.json()

        # 2. both legs stayed live
        assert body["excluded_collections"] == [], body["excluded_collections"]

        # 3. the filter matches every seeded doc
        assert body["results"], body

        # 4. applied_filters is a JSON object
        applied = body["applied_filters"]
        assert isinstance(applied, dict), f"applied_filters is {type(applied).__name__}: {applied!r}"

        # 5. echoed normalised: leading dot stripped, lowercased
        assert applied["file_type"] == _NORMALISED_FILE_TYPE, applied

        # 6. one shared echo, not keyed per collection
        assert _ALPHA not in applied and _BETA not in applied, applied
