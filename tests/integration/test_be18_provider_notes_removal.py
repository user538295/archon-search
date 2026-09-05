"""BE-18 integration: ``provider_notes`` is off the wire (S23, C6).

The field is removed from ``GET /status`` → ``model_validation`` with nothing
replacing it, and both committed OpenAPI snapshots — the server one and the
contract one, the latter stale before this change — must agree.
"""
from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from tests.integration.conftest import make_real_app

pytestmark = pytest.mark.integration

_PROBE_DEADLINE_SECONDS = 10.0
_SERVER_SNAPSHOT = Path(__file__).resolve().parents[1] / "server" / "openapi_snapshot.json"
_CONTRACT_SNAPSHOT = Path(__file__).resolve().parents[1] / "contract" / "openapi_snapshot.json"


def test_status_payload_omits_provider_notes(tmp_path, monkeypatch) -> None:
    """Read off the lifespan's own background probe result. Seeding
    ``app.state.model_validation`` instead would race that probe, which
    overwrites the slot the moment it finishes."""
    with make_real_app(tmp_path, monkeypatch) as (client, _config, api_key):
        deadline = time.monotonic() + _PROBE_DEADLINE_SECONDS
        while time.monotonic() < deadline and client.app.state.model_validation is None:
            time.sleep(0.05)
        assert client.app.state.model_validation is not None, "validation probe never completed"
        body = client.get("/status", headers={"Authorization": f"Bearer {api_key}"}).json()

    model_validation = body["model_validation"]
    assert "provider_warnings" in model_validation, model_validation
    assert "provider_notes" not in model_validation, model_validation


def test_both_openapi_snapshots_match() -> None:
    """The two committed snapshots are the same document — drift between them is
    how the contract copy went stale in the first place."""
    assert json.loads(_SERVER_SNAPSHOT.read_text()) == json.loads(_CONTRACT_SNAPSHOT.read_text())
