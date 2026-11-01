"""FE-10 (collection-descriptions plan, S26, S28, S60, S62): ``collection set-description`` / ``clear-description``.

The unit tests patch ``httpx.put`` / ``httpx.delete`` of ``archon_search.cli.collection``; one test runs the verbs through
the CLI-to-TestClient bridge against the real BE-31 routes.
"""

from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import MagicMock, patch

import httpx
import pytest
from _cli_server_stub import server_reported_stopped
from click.testing import CliRunner
from fastapi.testclient import TestClient

from archon_search.cli._helpers import (
    _DESCRIPTION_SET_REQUEST_TIMEOUT_S,
    _LOCK_WAITING_REQUEST_TIMEOUT_S,
)
from archon_search.cli.collection import collection
from archon_search.collection_meta import CollectionMeta
from archon_search.constants import (
    DEFAULT_NAMESPACE,
    EMBED_WAIT_SECONDS,
    INGEST_LOCK_TIMEOUT_S,
    MAX_DESCRIPTION_CHARS,
)
from archon_search.description_write import DescriptionWriteOutcome
from archon_search.description_writer import ClearChange, ManualSetChange
from tests._cli_testclient_bridge import cli_against_app
from tests._description_text_cases import REJECTED_TEXTS
from tests.test_be31_manual_description_routes import (
    _COLLECTION as _ROUTE_COLLECTION,
)
from tests.test_be31_manual_description_routes import (
    _MANUAL_TEXT as _ROUTE_TEXT,
)
from tests.test_be31_manual_description_routes import (
    _app,
    _meta,
    _reads,
    _RecordingWriter,
)

_NAME = "docs"
_TEXT = "Hand written note about the planning documents."
_KEY = "test-key"
_PUT = "archon_search.cli.collection.httpx.put"
_DELETE = "archon_search.cli.collection.httpx.delete"
_BEL = "\x07"
_ESC = "\x1b"
_BLANK_CASES = frozenset({"empty", "spaces-only", "invisible-only"})
_TOKEN = os.environ["ARCHON_SEARCH_API_KEY"]  # the key the app under test accepts (set by tests/conftest.py)


def _response(status_code: int, body: dict | None = None, text: str = "") -> MagicMock:
    resp = MagicMock()
    resp.status_code = status_code
    resp.json.return_value = body if body is not None else {}
    resp.text = text or str(body or "")
    resp.headers = {}
    return resp


def _set(*args: str, response: MagicMock | None = None) -> tuple[object, MagicMock]:
    with patch(_PUT, return_value=response or _response(200, {})) as call:
        result = CliRunner().invoke(collection, ["set-description", *args, "--api-key", _KEY])
    return result, call


def _clear(*args: str, response: MagicMock | None = None) -> tuple[object, MagicMock]:
    with patch(_DELETE, return_value=response or _response(200, {})) as call:
        result = CliRunner().invoke(collection, ["clear-description", *args, "--api-key", _KEY])
    return result, call


def _set_with(response: MagicMock) -> object:
    return _set(_NAME, _TEXT, response=response)[0]


def _clear_with(response: MagicMock) -> object:
    return _clear(_NAME, response=response)[0]


# ---------------------------------------------------------------------------
# set-description
# ---------------------------------------------------------------------------


def test_set_description_puts_body_and_prints_manual_source() -> None:
    """S26: the PUT carries exactly ``{"description": text}`` to the description route; the result names the source."""
    detail = {"name": _NAME, "description": _TEXT, "description_source": "manual", "description_status": "ok"}

    result, call = _set(_NAME, _TEXT, response=_response(200, detail))

    assert result.exit_code == 0, result.output
    call.assert_called_once()
    assert call.call_args.args == ("http://localhost:8765/collections/docs/description",)
    assert call.call_args.kwargs["json"] == {"description": _TEXT}
    assert call.call_args.kwargs["headers"] == {"Authorization": f"Bearer {_KEY}"}
    assert _NAME in result.output
    assert "manual" in result.output
    assert "ok" in result.output


def test_set_description_strips_terminal_controls_from_what_it_prints() -> None:
    """The source and status come from the server: nothing in them may drive the operator's terminal."""
    detail = {"description_source": f"manual{_ESC}[31m", "description_status": f"ok{_BEL}"}

    result, _ = _set(_NAME, _TEXT, response=_response(200, detail))

    assert result.exit_code == 0, result.output
    assert "description_source: manual [31m," in result.output
    assert "description_status: ok )" in result.output
    assert _ESC not in result.output
    assert _BEL not in result.output


def test_set_description_prints_the_name_with_terminal_controls_stripped() -> None:
    result, _ = _set(f"docs{_ESC}[31m", _TEXT)

    assert _ESC not in result.output
    assert "docs [31m" in result.output


def _expected_refusal(case: str) -> str:
    """The part of the shared rule's message each rejected case must show."""
    if "limit" in case:
        return f"at most {MAX_DESCRIPTION_CHARS}"
    return "blank" if case in _BLANK_CASES else "one line of plain text"


@pytest.mark.parametrize("case", list(REJECTED_TEXTS))
def test_set_description_rejects_multiline_before_calling_server(case: str) -> None:
    """S61: the shared text rule runs on the client; an invalid text makes no request, exits 1, names the rule, and
    never echoes the text."""
    text = REJECTED_TEXTS[case]

    result, call = _set(_NAME, text)

    assert result.exit_code == 1
    call.assert_not_called()
    assert _expected_refusal(case) in result.stderr
    assert text.strip() not in result.output or not text.strip()


def test_set_description_never_echoes_the_rejected_text() -> None:
    result, _ = _set(_NAME, f"first line\n{_ESC}[31msecond line")

    assert result.exit_code == 1
    assert "second line" not in result.output
    assert "second line" not in result.stderr


def test_set_description_accepts_exactly_the_limit() -> None:
    result, call = _set(_NAME, "x" * MAX_DESCRIPTION_CHARS, response=_response(200, {}))

    assert result.exit_code == 0, result.output
    call.assert_called_once()


def test_set_description_takes_text_that_starts_with_a_dash() -> None:
    """A bullet-like text is text, not an unknown option."""
    result, call = _set(_NAME, "- manuals and notes")

    assert result.exit_code == 0, result.output
    assert call.call_args.kwargs["json"] == {"description": "- manuals and notes"}


def test_set_description_not_found_exits_1() -> None:
    result, _ = _set(_NAME, _TEXT, response=_response(404, {"detail": "Collection 'docs' not found"}))

    assert result.exit_code == 1
    assert "'docs' not found" in result.stderr


def test_set_description_422_prints_the_servers_message_without_its_validator_prefix() -> None:
    detail = [{"type": "value_error", "loc": ["body", "description"], "msg": "Value error, description is odd"}]

    result, _ = _set(_NAME, _TEXT, response=_response(422, {"detail": detail}))

    assert result.exit_code == 1
    assert "Error: description is odd" in result.stderr
    assert "Value error" not in result.stderr


def test_set_description_422_joins_several_messages() -> None:
    detail = [{"msg": "Value error, first"}, {"msg": "Value error, second"}]

    result, _ = _set(_NAME, _TEXT, response=_response(422, {"detail": detail}))

    assert "first; second" in result.stderr


def test_set_description_422_with_a_plain_detail_string() -> None:
    result, _ = _set(_NAME, _TEXT, response=_response(422, {"detail": "text refused"}))

    assert result.exit_code == 1
    assert "text refused" in result.stderr


def test_set_description_422_without_a_usable_detail_still_exits_1_with_a_message() -> None:
    result, _ = _set(_NAME, _TEXT, response=_response(422, {}))

    assert result.exit_code == 1
    assert "refused the description" in result.stderr


def test_set_description_422_that_is_not_json_exits_1_instead_of_a_traceback() -> None:
    resp = _response(422, text="<html>proxy says no</html>")
    resp.json.side_effect = ValueError("not json")

    result, _ = _set(_NAME, _TEXT, response=resp)

    assert result.exit_code == 1
    assert "proxy says no" in result.stderr


def test_set_description_503_store_busy_says_to_retry() -> None:
    body = {"detail": "collection is busy with another write; retry after Retry-After seconds", "code": "store_busy"}

    result, _ = _set(_NAME, _TEXT, response=_response(503, body))

    assert result.exit_code == 1
    assert "write in progress" in result.stderr
    assert "Retry" in result.stderr


def test_set_description_other_status_prints_status_and_body_without_terminal_controls() -> None:
    result, _ = _set(_NAME, _TEXT, response=_response(500, text=f"boom{_ESC}[31m"))

    assert result.exit_code == 1
    assert "500" in result.stderr
    assert "boom [31m" in result.stderr
    assert _ESC not in result.stderr


def test_set_description_server_down_prints_the_connect_failure_message() -> None:
    with (
        patch(_PUT, side_effect=httpx.ConnectError("refused")),
        server_reported_stopped(),
    ):
        result = CliRunner().invoke(collection, ["set-description", _NAME, _TEXT, "--api-key", _KEY])

    assert result.exit_code == 1
    assert "not running" in result.stderr


def test_set_description_other_transport_error_exits_1() -> None:
    with patch(_PUT, side_effect=httpx.ReadTimeout("slow")):
        result = CliRunner().invoke(collection, ["set-description", _NAME, _TEXT, "--api-key", _KEY])

    assert result.exit_code == 1
    assert "Error contacting server" in result.stderr


# ---------------------------------------------------------------------------
# clear-description
# ---------------------------------------------------------------------------


def test_clear_description_deletes_and_confirms() -> None:
    result, call = _clear(_NAME)

    assert result.exit_code == 0, result.output
    call.assert_called_once()
    assert call.call_args.args == ("http://localhost:8765/collections/docs/description",)
    assert call.call_args.kwargs["headers"] == {"Authorization": f"Bearer {_KEY}"}
    assert "Cleared" in result.output
    assert _NAME in result.output


def test_clear_description_prints_describe_hint() -> None:
    """S62: nothing is generated by a clear; the output says how to get a new description."""
    result, _ = _clear(_NAME)

    assert result.exit_code == 0, result.output
    assert f"archon-search collection describe {_NAME}" in result.output


def test_clear_description_not_found_exits_1_without_the_hint() -> None:
    result, _ = _clear(_NAME, response=_response(404, {"detail": "Collection 'docs' not found"}))

    assert result.exit_code == 1
    assert "'docs' not found" in result.stderr
    assert "describe" not in result.output


def test_clear_description_503_store_busy_says_to_retry() -> None:
    result, _ = _clear(_NAME, response=_response(503, {"detail": "busy", "code": "store_busy"}))

    assert result.exit_code == 1
    assert "write in progress" in result.stderr


@pytest.mark.parametrize("run", [_set_with, _clear_with], ids=["set-description", "clear-description"])
def test_503_that_is_not_store_busy_is_not_reported_as_a_write_in_progress(run: object) -> None:
    """A server without its search store answers 503 ``service not ready``: that is not lock contention."""
    result = run(_response(503, {"detail": f"service not ready{_BEL}"}))  # type: ignore[operator]

    assert result.exit_code == 1
    assert "service not ready" in result.stderr
    assert "write in progress" not in result.stderr
    assert _BEL not in result.stderr


@pytest.mark.parametrize("name", ["docs#x", "docs?x", "a/b", "docs/..", "my docs", ".", "..", "./."])
def test_the_collection_name_is_one_path_segment_of_the_request_url(name: str) -> None:
    """A name with ``#``, ``?``, ``/`` or dots must not turn the destructive DELETE into a request for another route:
    the path httpx actually sends (after its own dot-segment removal) is still ``/collections/<one segment>/description``."""
    _, put_call = _set(name, _TEXT)
    _, delete_call = _clear(name)

    for call in (put_call, delete_call):
        path = httpx.Request("DELETE", call.call_args.args[0]).url.raw_path.decode()
        segments = path.split("/")
        assert segments[:2] == ["", "collections"], path
        assert segments[3:] == ["description"], path
        assert len(segments) == 4, path
        assert "#" not in path
        assert "?" not in path


def test_an_empty_collection_name_makes_no_request() -> None:
    with patch(_PUT) as put, patch(_DELETE) as delete:
        result = CliRunner().invoke(collection, ["clear-description", "", "--api-key", _KEY])
        set_result = CliRunner().invoke(collection, ["set-description", "", _TEXT, "--api-key", _KEY])

    assert result.exit_code == 1
    assert set_result.exit_code == 1
    put.assert_not_called()
    delete.assert_not_called()
    assert "name" in result.stderr


def test_set_description_refuses_text_that_cannot_be_encoded_as_utf8() -> None:
    """A stray non-UTF-8 byte in argv arrives as a lone surrogate: say so instead of a traceback from the JSON body."""
    result, call = _set(_NAME, "bad \udcff byte")

    assert result.exit_code == 1
    assert "UTF-8" in result.stderr
    call.assert_not_called()


def test_the_describe_hint_quotes_a_name_for_the_shell() -> None:
    result, _ = _clear("my docs")

    assert "archon-search collection describe 'my docs'" in result.output


def test_clear_description_prints_the_name_with_terminal_controls_stripped() -> None:
    result, _ = _clear(f"docs{_ESC}[31m")

    assert _ESC not in result.output
    assert "docs [31m" in result.output


def test_clear_description_does_not_read_the_answer_body() -> None:
    """The 200 body of a clear is never used: a body that is not JSON must not turn a done clear into a traceback."""
    resp = _response(200, {})
    resp.json.side_effect = ValueError("not json")

    result, _ = _clear(_NAME, response=resp)

    assert result.exit_code == 0, result.output
    assert "Cleared" in result.output


# ---------------------------------------------------------------------------
# Request timeout: both routes may wait for the collection lock, the set route also embeds the text first
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("patched", "args", "expected_timeout"),
    [
        pytest.param(_PUT, ["set-description", _NAME, _TEXT], _DESCRIPTION_SET_REQUEST_TIMEOUT_S, id="set-description"),
        pytest.param(_DELETE, ["clear-description", _NAME], _LOCK_WAITING_REQUEST_TIMEOUT_S, id="clear-description"),
    ],
)
def test_description_verbs_use_lock_waiting_timeout(patched: str, args: list[str], expected_timeout: float) -> None:
    """Both routes may wait up to INGEST_LOCK_TIMEOUT_S for the collection lock, and the set route embeds the text
    (up to EMBED_WAIT_SECONDS) before it takes the lock: the CLI waits longer than the server can, not httpx's 5 s."""
    with patch(patched, return_value=_response(500, text="boom")) as call:
        CliRunner().invoke(collection, [*args, "--api-key", _KEY])

    call.assert_called_once()
    assert call.call_args.kwargs["timeout"] == expected_timeout
    assert expected_timeout > INGEST_LOCK_TIMEOUT_S


def test_set_description_timeout_outlasts_the_embedding_wait_and_the_lock_wait() -> None:
    assert _DESCRIPTION_SET_REQUEST_TIMEOUT_S > EMBED_WAIT_SECONDS + INGEST_LOCK_TIMEOUT_S


# ---------------------------------------------------------------------------
# Through the real routes (CLI-to-TestClient bridge)
# ---------------------------------------------------------------------------


def _bridged(tmp_path: Path, before: CollectionMeta, after: CollectionMeta) -> tuple[TestClient, _RecordingWriter]:
    client, store, _lifecycle = _app(tmp_path, {DEFAULT_NAMESPACE: before})
    _reads(store, before, after)
    writer = _RecordingWriter(store, DescriptionWriteOutcome.WRITTEN)
    client.app.state.description_writer = writer
    return client, writer


def test_set_and_clear_reach_the_real_routes_through_the_bridge(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    after_set = _meta(description=_ROUTE_TEXT, source="manual", status="ok")
    client, writer = _bridged(tmp_path, _meta(), after_set)

    with cli_against_app(client, _TOKEN, monkeypatch) as cli:
        set_result = cli.invoke(collection, ["set-description", _ROUTE_COLLECTION, _ROUTE_TEXT])

    assert set_result.exit_code == 0, set_result.output
    assert "manual" in set_result.output
    assert writer.calls[-1][2] == ManualSetChange(text=_ROUTE_TEXT)

    cleared = _meta(description="", source=None, status=None)
    client, writer = _bridged(tmp_path, after_set, cleared)

    with cli_against_app(client, _TOKEN, monkeypatch) as cli:
        clear_result = cli.invoke(collection, ["clear-description", _ROUTE_COLLECTION])

    assert clear_result.exit_code == 0, clear_result.output
    assert f"archon-search collection describe {_ROUTE_COLLECTION}" in clear_result.output
    assert writer.calls[-1][2] == ClearChange()


def test_the_bridge_maps_an_unknown_collection_to_the_not_found_message(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, _store, _lifecycle = _app(tmp_path, {DEFAULT_NAMESPACE: _meta()})

    with cli_against_app(client, _TOKEN, monkeypatch) as cli:
        result = cli.invoke(collection, ["set-description", "ghost", _ROUTE_TEXT])

    assert result.exit_code == 1
    assert "'ghost' not found" in result.stderr
