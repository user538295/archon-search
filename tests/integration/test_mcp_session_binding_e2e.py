"""An MCP session belongs to the credential that created it.

The session id travels in a header and is not a credential: the MCP library refuses a request for a session whose principal
differs from the one that created it, but only when the request's ASGI scope carries an authenticated user. The API key
middleware stamps one, so a different key (another namespace's, or any other valid key) that learns a session id gets the
same answer as for a session that does not exist, and can neither use it nor cancel the calls running in it.
"""

from __future__ import annotations

import secrets
from pathlib import Path

import pytest

from tests._fake_model_server import keep_loopback_out_of_proxies
from tests.integration.conftest import make_real_app
from tests.integration.test_mcp_roundtrip_t2 import _mcp_headers, _mcp_initialize

pytestmark = pytest.mark.integration

_SESSION_NOT_FOUND = 404
_OK = 200
_ACCEPTED = 202
_LIST_TOOLS = {"jsonrpc": "2.0", "id": 2, "method": "tools/list"}
_CANCEL = {"jsonrpc": "2.0", "method": "notifications/cancelled", "params": {"requestId": 7, "reason": "binding test"}}


@pytest.fixture(autouse=True)
def _no_proxy_for_loopback(monkeypatch: pytest.MonkeyPatch) -> None:
    keep_loopback_out_of_proxies(monkeypatch)


def _post(client, token: str, session: str, body: dict) -> int:
    return client.post("/mcp", json=body, headers=_mcp_headers(token, session)).status_code


def test_a_session_answers_only_the_credential_that_created_it(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    owner, other = secrets.token_hex(32), secrets.token_hex(32)
    namespaces = {owner: "owner-namespace", other: "other-namespace"}
    with make_real_app(tmp_path, monkeypatch, namespaces=namespaces, mcp_enabled=True) as (client, _config, admin):
        session = _mcp_initialize(client, owner)

        assert _post(client, owner, session, _LIST_TOOLS) == _OK, "the creating credential keeps its session"
        assert _post(client, other, session, _LIST_TOOLS) == _SESSION_NOT_FOUND, "another namespace's key cannot use it"
        assert _post(client, other, session, _CANCEL) == _SESSION_NOT_FOUND, "nor cancel a call running in it"
        assert _post(client, admin, session, _LIST_TOOLS) == _SESSION_NOT_FOUND, "nor can the admin key of the server"
        assert _post(client, owner, session, _CANCEL) == _ACCEPTED, "the creating credential may cancel its own call"
        assert _post(client, owner, session, _LIST_TOOLS) == _OK, "and the refusals did not end the session"
