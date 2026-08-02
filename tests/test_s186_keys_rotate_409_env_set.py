"""S186 regression: the incident runbook's ``POST /keys/rotate`` 409 invocation
must show the required request body and Content-Type.

``POST /keys/rotate`` takes a required JSON body (``KeyRotateRequest``). FastAPI
validates the body BEFORE the ``ARCHON_SEARCH_API_KEY`` env-var check runs, so a
plain ``curl`` with no body returns 422 (``{"loc": ["body"], "msg": "Field
required"}``) and the documented 409 is never reached. The runbook must give a
copy-paste-ready invocation that actually produces the 409 — i.e. one that sends
``-H 'Content-Type: application/json'`` and a request body.
"""
from __future__ import annotations

import secrets
from pathlib import Path

from fastapi.testclient import TestClient

from tests.test_keystore_be8 import _make_app

_REPO_ROOT = Path(__file__).resolve().parents[1]
_RUNBOOK = _REPO_ROOT / "Documentation" / "OperatorGuide" / "90_incident_runbook.md"


def test_rotate_requires_body_before_env_check(tmp_path, monkeypatch):
    """Grounds the doc requirement: no body → 422; body + env set → 409.

    Body validation runs before the env-var check, so the documented 409 is only
    reachable when a request body is sent. This is why the runbook must show one.
    """
    api_key = secrets.token_hex(32)
    app, api_key = _make_app(tmp_path, monkeypatch, api_key=api_key)  # env var set

    with TestClient(app) as client:
        headers = {"Authorization": f"Bearer {api_key}"}
        # No body: body validation fires first → 422, NOT the documented 409.
        no_body = client.post("/keys/rotate", headers=headers)
        assert no_body.status_code == 422
        # Body present: env-var check reached → documented 409.
        with_body = client.post("/keys/rotate", json={}, headers=headers)
        assert with_body.status_code == 409


def test_runbook_409_invocation_shows_request_body():
    """The runbook's env-var-set 409 guidance must show a body + Content-Type.

    Without them an operator's copy-pasted curl returns 422, not the 409 the
    runbook promises.
    """
    text = _RUNBOOK.read_text()

    # Locate the env-var-set section that documents the 409.
    marker = "When `ARCHON_SEARCH_API_KEY` env var is set"
    assert marker in text, "runbook lost the env-var-set 409 section"
    section = text[text.index(marker):]
    # Bound the search to this section (up to the next H3 heading).
    next_heading = section.find("\n### ", 1)
    if next_heading != -1:
        section = section[:next_heading]

    assert "/keys/rotate" in section
    assert "Content-Type: application/json" in section, (
        "runbook 409 invocation omits the Content-Type header — a plain curl "
        "returns 422 (body required) before the env-var check"
    )
    assert "-d " in section, (
        "runbook 409 invocation omits the request body (-d) — a plain curl "
        "returns 422 (body required) before the env-var check"
    )
