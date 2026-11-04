"""FE-4 (interface-parity 01-SEARCH, S4, S51, S54, S55, S74): ``archon-search search`` renders every REST error shape.

The unit tests patch ``httpx.post`` of ``archon_search.cli.search`` and answer with a real ``httpx.Response`` of the
shape the REST route sends (``routes_search._error_response`` and the app-level handlers).
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch

import httpx
import pytest
from click.testing import CliRunner

from archon_search.cli.collection import _EMPTY_SERVER_MESSAGE as _DESCRIPTION_TEXT
from archon_search.cli.search import search
from archon_search.embedder_cache import EMBEDDER_NOT_READY_DETAIL
from archon_search.services.service_error import RETRY_AFTER_SECONDS

_POST = "archon_search.cli.search.httpx.post"
_PROBE = "archon_search.cli._helpers.httpx.get"
_LOAD_KEY = "archon_search.cli._helpers.load_key"
_PLAIN_500 = "Internal Server Error"


def _answer(status: int, *, json: Any = None, text: str | None = None, headers: dict[str, str] | None = None):
    if text is not None:
        return httpx.Response(status, text=text, headers={"content-type": "text/plain", **(headers or {})})
    if json is not None:
        return httpx.Response(status, json=json, headers=headers)
    return httpx.Response(status, headers=headers)


def _run(resp: Any = None, *, args: list[str] | None = None, side_effect: Exception | None = None):
    argv = ["q", "--collection", "docs", "--api-key", "k"] if args is None else args
    with patch(_POST, return_value=resp, side_effect=side_effect):
        return CliRunner().invoke(search, argv)


@pytest.mark.parametrize(
    ("resp", "shown"),
    [
        pytest.param(
            _answer(400, json={"detail": {"code": "invalid_scope_filter", "message": "scope filter is not valid"}}),
            "scope filter is not valid",
            id="400-coded",
        ),
        pytest.param(_answer(404, json={"detail": "collection not found"}), "collection not found", id="404-string"),
        pytest.param(
            _answer(422, json={"detail": "collections exceeds the fan-out limit of 2"}),
            "collections exceeds the fan-out limit of 2",
            id="422-string",
        ),
        pytest.param(
            _answer(
                422,
                json={"detail": [{"loc": ["body", "query"], "msg": "Value error, query must not be empty", "type": "x"}]},
            ),
            "query must not be empty",
            id="422-list",
        ),
        pytest.param(
            _answer(
                422,
                json={"detail": {"code": "graph_communities_not_built", "message": "Run community detection first."}},
            ),
            "Run community detection first.",
            id="422-coded",
        ),
        pytest.param(_answer(504, json={"detail": "Search timed out"}), "Search timed out", id="504"),
        pytest.param(_answer(500, text=_PLAIN_500), _PLAIN_500, id="500-plain-text"),
        pytest.param(
            _answer(401),
            "the server rejected the API key. Check --api-key, ARCHON_SEARCH_API_KEY or the key file.",
            id="401-bare",
        ),
        pytest.param(_answer(502), "the server answered 502 without an error message.", id="502-empty-body"),
        pytest.param(_answer(500, text=" \n"), "the server answered 500 without an error message.", id="500-blank-body"),
        pytest.param(_answer(500, text=_PLAIN_500 + "\n"), _PLAIN_500, id="500-trailing-newline"),
    ],
)
def test_search_renders_every_rest_error_shape_on_stderr_and_exits_1(resp: httpx.Response, shown: str) -> None:
    result = _run(resp)

    assert result.exit_code == 1, result.output
    assert result.stdout == ""
    assert result.stderr == f"Error: {shown}\n"
    assert _DESCRIPTION_TEXT not in result.stderr


def test_search_422_list_drops_the_validator_prefix() -> None:
    detail = [{"msg": "Value error, query must not be empty"}, {"msg": "Value error, bad filter"}]

    result = _run(_answer(422, json={"detail": detail}))

    assert "Value error" not in result.stderr
    assert "query must not be empty; bad filter" in result.stderr


_DEEP_JSON = "[" * 100_000  # json.loads raises RecursionError, not ValueError


def test_search_a_200_body_of_deeply_nested_json_is_an_unexpected_response_not_a_traceback() -> None:
    result = _run(_answer(200, text=_DEEP_JSON))

    assert result.exit_code == 1
    assert isinstance(result.exception, SystemExit)
    assert result.stdout == ""
    assert result.stderr.startswith("Error: server returned unexpected response: ")
    assert "Traceback" not in result.stderr


def test_search_an_error_answer_of_deeply_nested_json_prints_its_body_text_and_exits_1() -> None:
    result = _run(_answer(500, text=_DEEP_JSON))

    assert result.exit_code == 1
    assert isinstance(result.exception, SystemExit)
    assert result.stderr == f"Error: {_DEEP_JSON}\n"


def test_search_503_shows_the_retry_after_in_seconds() -> None:
    resp = _answer(
        503,
        json={"detail": EMBEDDER_NOT_READY_DETAIL, "code": "embedder_not_ready"},
        headers={"Retry-After": str(RETRY_AFTER_SECONDS)},
    )

    result = _run(resp)

    assert result.exit_code == 1
    assert result.stderr == f"Error: {EMBEDDER_NOT_READY_DETAIL} (retry after {RETRY_AFTER_SECONDS}s)\n"


def test_search_503_without_retry_after_shows_the_detail_alone() -> None:
    result = _run(_answer(503, json={"detail": "metadata store error", "code": "metadata_store_error"}))

    assert result.stderr == "Error: metadata store error\n"


def test_search_503_does_not_echo_a_retry_after_that_is_not_a_number_of_seconds() -> None:
    resp = _answer(503, json={"detail": "busy"}, headers={"Retry-After": "Wed, 21 Oct 2026 07:28:00 \x1b[31mGMT"})

    result = _run(resp)

    assert result.stderr == "Error: busy\n"


def test_search_connection_refused_and_missing_key_print_the_shared_messages_and_exit_1() -> None:
    with patch(_PROBE, side_effect=httpx.ConnectError("refused")):
        refused = _run(
            side_effect=httpx.ConnectError("refused"),
            args=["q", "--collection", "d", "--api-key", "k", "--api-url", "http://h:1"],
        )
    with patch(_LOAD_KEY, return_value=None):
        no_key = _run(args=["q", "--collection", "d"])

    assert refused.exit_code == 1
    assert refused.stderr == "archon-search serve is not running. Start it first with: archon-search serve\n"
    assert no_key.exit_code == 1
    assert "No API key found. Pass --api-key" in no_key.stderr


def test_search_other_transport_errors_exit_1_with_the_httpx_text() -> None:
    result = _run(side_effect=httpx.ReadTimeout("took too long"))

    assert result.exit_code == 1
    assert "Error contacting server: took too long" in result.stderr


def test_search_strips_terminal_controls_from_stdout_and_stderr() -> None:
    ok = MagicMock(status_code=200)
    ok.json.return_value = {
        "results": [{"score": 1.0, "collection": "c\x1b[2J", "source_path": "/p\x07q", "text": "a\x1b[31mb\nc"}],
        "expansion_warning": "warn\x1b[0m",
        "excluded_collections": [{"name": "n\x07", "reason": "r\x1b]0;x"}],
    }
    shown = _run(ok)
    not_found = _run(_answer(404, json={"detail": "no \x1b[31mcollection\r\nforged: line"}))
    coded = _run(_answer(400, json={"detail": {"code": "c", "message": "bad\x1b[2J"}}))
    plain = _run(_answer(500, text="oops\x1b[0m\nline two"))

    for result in (shown, not_found, coded, plain):
        assert "\x1b" not in result.stdout + result.stderr
        assert "\x07" not in result.stdout + result.stderr
    assert len(shown.stdout.splitlines()) == 1
    assert not_found.stderr.count("\n") == 1
    assert plain.stderr.count("\n") == 1
    assert plain.stderr.startswith("Error: oops") and "line two" in plain.stderr
