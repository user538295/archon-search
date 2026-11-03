"""FE-3 (interface-parity 01-SEARCH, S42, S47-S49, S52, S53): the ``archon-search search`` command.

The unit tests patch ``httpx.post`` of ``archon_search.cli.search`` and read the request the command builds.
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from archon_search.cli import search as search_mod
from archon_search.cli.main import main
from archon_search.cli.search import search
from archon_search.constants import CLI_SEARCH_REQUEST_TIMEOUT_SECONDS

_POST = "archon_search.cli.search.httpx.post"


def _result(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "doc_id": "d1",
        "chunk_id": "c1",
        "text": "alpha beta",
        "score": 0.8125,
        "source_path": "/docs/a.md",
        "collection": "docs",
    }
    return {**base, **overrides}


def _response(results: list[dict[str, Any]] | None = None, **extra: Any) -> MagicMock:
    payload = {"results": [_result()] if results is None else results, "acl_filtered": False, **extra}
    resp = MagicMock()
    resp.status_code = 200
    resp.json.return_value = payload
    return resp


def _run(args: list[str], resp: MagicMock | None = None):
    with patch(_POST, return_value=resp or _response()) as post:
        result = CliRunner().invoke(search, [*args, "--api-key", "k"])
    return result, post


def test_search_is_registered_on_the_main_group() -> None:
    assert main.commands["search"] is search


def test_search_prints_one_line_per_result_in_response_order() -> None:
    results = [
        _result(source_path="/p/a.md", text="first hit", score=0.9, collection="docs"),
        _result(source_path="/p/b.md", text="second hit", score=0.5, collection="wiki"),
    ]

    result, _ = _run(["q", "--collection", "docs"], _response(results))

    assert result.exit_code == 0, result.output
    lines = result.stdout.splitlines()
    assert len(lines) == 2
    assert lines[0].split("  ") == ["1.", "0.9000", "docs", "/p/a.md", "first hit"]
    assert lines[1].split("  ") == ["2.", "0.5000", "wiki", "/p/b.md", "second hit"]


def test_search_prints_enough_score_digits_to_tell_close_rrf_scores_apart() -> None:
    results = [_result(score=1 / 61), _result(score=1 / 62)]

    result, _ = _run(["q", "--collection", "docs"], _response(results))

    first, second = (line.split("  ")[1] for line in result.stdout.splitlines())
    assert first != second


def test_search_collapses_whitespace_and_cuts_the_excerpt_at_the_constant() -> None:
    limit = search_mod._EXCERPT_MAX_CHARS
    text = "word\n\n  spaced\t" + "x" * (limit * 2)
    collapsed = "word spaced " + "x" * (limit * 2)

    result, _ = _run(["q", "--collection", "docs"], _response([_result(text=text)]))

    (line,) = result.stdout.splitlines()
    assert collapsed[:limit] in line
    assert collapsed[: limit + 1] not in line


def test_search_one_collection_sends_collection_and_several_send_collections() -> None:
    _, single = _run(["q", "--collection", "docs"])
    _, multi = _run(["q", "--collection", "docs", "--collection", "wiki"])
    _, repeated = _run(["q", "--collection", "docs", "--collection", "docs"])

    assert single.call_args.kwargs["json"] == {"query": "q", "collection": "docs"}
    assert multi.call_args.kwargs["json"] == {"query": "q", "collections": ["docs", "wiki"]}
    assert repeated.call_args.kwargs["json"] == {"query": "q", "collections": ["docs", "docs"]}


def test_search_posts_to_the_search_route_with_the_bearer_key() -> None:
    with patch(_POST, return_value=_response()) as post:
        CliRunner().invoke(search, ["q", "--collection", "docs", "--api-url", "http://h:1/", "--api-key", "secret"])

    assert post.call_args.args == ("http://h:1/search",)
    assert post.call_args.kwargs["headers"] == {"Authorization": "Bearer secret"}


def test_search_json_reserialises_the_response_with_sorted_keys_and_full_text() -> None:
    long_text = "word " * 200
    resp = _response([_result(text=long_text + "é")], embedding_model="m")

    result, _ = _run(["q", "--collection", "docs", "--json"], resp)

    assert result.exit_code == 0, result.output
    assert result.stdout == json.dumps(resp.json.return_value, indent=2, sort_keys=True) + "\n"
    assert json.loads(result.stdout)["results"][0]["text"] == long_text + "é"


def test_search_empty_result_prints_a_message_and_exits_0() -> None:
    result, _ = _run(["q", "--collection", "docs"], _response([]))

    assert result.exit_code == 0
    assert result.stdout == "No results found.\n"


def test_search_without_collection_is_a_usage_error_exit_2() -> None:
    with patch(_POST) as post:
        result = CliRunner().invoke(search, ["q", "--api-key", "k"])

    assert result.exit_code == 2
    post.assert_not_called()


def test_search_200_with_a_non_json_body_exits_1_like_collection_list() -> None:
    resp = MagicMock()
    resp.status_code = 200
    resp.json.side_effect = ValueError("Expecting value")

    result, _ = _run(["q", "--collection", "docs"], resp)

    assert result.exit_code == 1
    assert "Error: server returned unexpected response: Expecting value" in result.stderr
    assert result.stdout == ""


@pytest.mark.parametrize(
    ("payload", "problem"),
    [
        ({"results": "boom"}, "results is not a list of objects"),
        ({"results": {"a": 1}}, "results is not a list of objects"),
        ({"results": ["boom"]}, "results is not a list of objects"),
        ({"results": 0}, "results is not a list of objects"),
        ({"results": ""}, "results is not a list of objects"),
        ({"results": False}, "results is not a list of objects"),
        ({"results": [_result(score="high")]}, "a result score is not a number"),
        ({"results": [_result(score=None)]}, "a result score is not a number"),
        ({"results": [_result(score=True)]}, "a result score is not a number"),
        ({"results": [_result(score=10**400)]}, "a result score is not a number"),
        ({"results": [], "excluded_collections": "boom"}, "excluded_collections is not a list of objects"),
        ({"results": [], "excluded_collections": ["boom"]}, "excluded_collections is not a list of objects"),
        ({"results": [], "excluded_collections": 0}, "excluded_collections is not a list of objects"),
    ],
)
def test_search_200_with_a_wrongly_typed_field_exits_1_without_a_traceback(payload: dict[str, Any], problem: str) -> None:
    resp = MagicMock()
    resp.status_code = 200
    resp.json.return_value = payload

    result, _ = _run(["q", "--collection", "docs"], resp)

    assert result.exit_code == 1
    assert isinstance(result.exception, SystemExit)
    assert f"Error: server returned unexpected response: {problem}" in result.stderr
    assert result.stdout == ""


def test_search_200_with_null_results_and_excluded_collections_prints_no_results() -> None:
    resp = MagicMock()
    resp.status_code = 200
    resp.json.return_value = {"results": None, "excluded_collections": None}

    result, _ = _run(["q", "--collection", "docs"], resp)

    assert result.exit_code == 0
    assert result.stdout == "No results found.\n"


def test_search_json_prints_a_wrongly_typed_body_unchanged() -> None:
    resp = MagicMock()
    resp.status_code = 200
    resp.json.return_value = {"results": "boom"}

    result, _ = _run(["q", "--collection", "docs", "--json"], resp)

    assert result.exit_code == 0
    assert json.loads(result.stdout) == {"results": "boom"}


def test_search_200_with_a_json_body_that_is_not_an_object_exits_1() -> None:
    resp = MagicMock()
    resp.status_code = 200
    resp.json.return_value = ["not", "an", "object"]

    result, _ = _run(["q", "--collection", "docs"], resp)

    assert result.exit_code == 1
    assert "Error: server returned unexpected response" in result.stderr


def test_search_json_prints_no_notes_on_stderr() -> None:
    resp = _response(expansion_warning="HyDE did not run", excluded_collections=[{"name": "w", "reason": "r"}])

    result, _ = _run(["q", "--collection", "docs", "--json"], resp)

    assert result.stderr == ""


def test_search_prints_notes_after_the_results_on_stderr_only() -> None:
    resp = _response(
        expansion_warning="HyDE did\x1b[31m not run",
        excluded_collections=[{"name": "wiki\x07", "reason": "model\nmismatch"}, {"name": "old", "reason": "stale"}],
    )

    result, _ = _run(["q", "--collection", "docs", "--collection", "wiki", "--collection", "old"], resp)

    assert result.exit_code == 0, result.output
    assert len(result.stdout.splitlines()) == 1
    notes = result.stderr.splitlines()
    assert len(notes) == 3
    assert "HyDE did [31m not run" in notes[0]
    assert "wiki " in notes[1] and "model mismatch" in notes[1]
    assert "old" in notes[2] and "stale" in notes[2]
    assert "\x1b" not in result.stderr and "\x07" not in result.stderr


def test_search_all_skipped_prints_no_results_on_stdout_and_the_reasons_on_stderr() -> None:
    resp = _response([], excluded_collections=[{"name": "wiki", "reason": "model mismatch"}])

    result, _ = _run(["q", "--collection", "wiki"], resp)

    assert result.exit_code == 0
    assert result.stdout == "No results found.\n"
    assert "wiki" in result.stderr and "model mismatch" in result.stderr


def test_search_prints_no_note_when_neither_field_is_present() -> None:
    result, _ = _run(["q", "--collection", "docs"], _response(expansion_warning=None, excluded_collections=[]))

    assert result.stderr == ""


def test_search_strips_terminal_controls_from_every_result_field() -> None:
    result, _ = _run(
        ["q", "--collection", "docs"],
        _response([_result(text="a\x1b[2Jb\nforged", source_path="/p\nq\x07", collection="c\r\x1bd")]),
    )

    assert "\x1b" not in result.stdout and "\x07" not in result.stdout
    assert len(result.stdout.splitlines()) == 1


def test_search_sends_the_request_with_the_cli_timeout_constant() -> None:
    _, post = _run(["q", "--collection", "docs"])

    assert post.call_args.kwargs["timeout"] == CLI_SEARCH_REQUEST_TIMEOUT_SECONDS
