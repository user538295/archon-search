"""FE-4 (interface-parity 01-SEARCH, S4, S51, S55, S74): ``archon-search search`` against the real app.

The real app (real store and pipeline) is reached through ``cli_against_app`` (``tests/_cli_testclient_bridge.py``), so the
printed text and ``--json`` output are what the REST adapter and the service answer, with no port bound.
"""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from archon_search.cli.search import search
from archon_search.services.request_hyde import HYDE_EXPANSION_FAILED_WARNING
from tests._cli_testclient_bridge import cli_against_app
from tests._hyde_stubs import rest_hyde_generating
from tests.integration.conftest import ingest_doc, make_real_app

pytestmark = pytest.mark.integration

_QUERY = "widget"
_ALPHA = "fe4-alpha"
_BETA = "fe4-beta"
_ALPHA_PATH = "/corpus/fe4/alpha-widgets.md"
_BETA_PATH = "/corpus/fe4/beta-widgets.md"
_DOC_TEXT = "# Widgets\n\nThe widget factory builds a widget every minute.\nEach widget is tested twice.\n"
_MAX_FANOUT = 2
_OTHER_MODEL = "some-other-embedding-model"
_COLLECTION_COLUMN = 2  # "rank  score  collection  path  excerpt"
_WRONG_KEY = "ab" * 32  # a well-formed key the server does not know
_BARE_401_TEXT = "the server rejected the API key. Check --api-key, ARCHON_SEARCH_API_KEY or the key file."


def _headers(api_key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {api_key}"}


def _rest_search(client: TestClient, api_key: str, body: dict[str, Any]):
    return client.post("/search", json=body, headers=_headers(api_key))


def _seed(client: TestClient, api_key: str, *names: str) -> None:
    paths = {_ALPHA: _ALPHA_PATH, _BETA: _BETA_PATH}
    for name in names:
        ingest_doc(client, name, _DOC_TEXT, paths[name], api_key=api_key)


def test_cli_search_text_and_json_come_from_the_real_adapter_and_service(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with make_real_app(tmp_path, monkeypatch) as (client, _cfg, api_key):
        _seed(client, api_key, _ALPHA)
        rest = _rest_search(client, api_key, {"collection": _ALPHA, "query": _QUERY})
        assert rest.status_code == 200, rest.text
        expected = rest.json()
        assert expected["results"], "the seeded document must match the query"

        with cli_against_app(client, api_key, monkeypatch) as cli:
            text = cli.invoke(search, [_QUERY, "--collection", _ALPHA])
            as_json = cli.invoke(search, [_QUERY, "--collection", _ALPHA, "--json"])

    assert text.exit_code == 0, text.output
    lines = text.stdout.splitlines()
    assert len(lines) == len(expected["results"])
    for rank, (line, hit) in enumerate(zip(lines, expected["results"], strict=True), start=1):
        assert line.startswith(f"{rank}.  ")
        assert f"{hit['score']:.4f}" in line
        assert _ALPHA in line and _ALPHA_PATH in line
    assert as_json.exit_code == 0, as_json.output
    assert as_json.stdout == json.dumps(expected, indent=2, sort_keys=True) + "\n"


def test_cli_search_unknown_collection_and_fanout_exceeded_print_the_server_text(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with make_real_app(tmp_path, monkeypatch, max_fanout=_MAX_FANOUT) as (client, _cfg, api_key):
        unknown = _rest_search(client, api_key, {"collection": "fe4-missing", "query": _QUERY})
        fanout_names = [_ALPHA, _BETA, "fe4-third"]
        fanout = _rest_search(client, api_key, {"collections": fanout_names, "query": _QUERY})
        assert (unknown.status_code, fanout.status_code) == (404, 422)

        with cli_against_app(client, api_key, monkeypatch) as cli:
            unknown_run = cli.invoke(search, [_QUERY, "--collection", "fe4-missing"])
            fanout_run = cli.invoke(
                search, [_QUERY, *(arg for name in fanout_names for arg in ("--collection", name))]
            )

    assert unknown_run.exit_code == 1 and unknown_run.stdout == ""
    assert f"Error: {unknown.json()['detail']}\n" in unknown_run.stderr
    assert fanout_run.exit_code == 1 and fanout_run.stdout == ""
    assert f"Error: {fanout.json()['detail']}\n" in fanout_run.stderr


def test_cli_two_collections_use_the_multi_path_through_the_real_app(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with make_real_app(tmp_path, monkeypatch) as (client, _cfg, api_key):
        _seed(client, api_key, _ALPHA, _BETA)
        rest = _rest_search(client, api_key, {"collections": [_ALPHA, _BETA], "query": _QUERY})
        assert rest.status_code == 200, rest.text
        expected = rest.json()

        with cli_against_app(client, api_key, monkeypatch) as cli:
            text = cli.invoke(search, [_QUERY, "--collection", _ALPHA, "--collection", _BETA])
            as_json = cli.invoke(search, [_QUERY, "--collection", _ALPHA, "--collection", _BETA, "--json"])

    assert text.exit_code == 0, text.output
    assert len(text.stdout.splitlines()) == len(expected["results"])
    assert {hit["collection"] for hit in expected["results"]} == {_ALPHA, _BETA}
    assert json.loads(as_json.stdout) == expected


def test_cli_search_prints_the_not_run_step_and_skipped_collection_notes_on_stderr(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """TQ1, option D, through the real app.

    ``--hyde`` has no usable generator here, so the server answers with the standard warning (the app, the service
    and the notes are all real). A model-mismatched collection is skipped.
    """
    with make_real_app(tmp_path, monkeypatch) as (client, _cfg, api_key):
        _seed(client, api_key, _ALPHA, _BETA)
        store = client.app.state.search_store
        beta = client.portal.call(store.get_collection_meta, _BETA)
        client.portal.call(store.update_collection_meta, replace(beta, active_embedding_model=_OTHER_MODEL))

        with rest_hyde_generating(client.app, return_value=None), cli_against_app(client, api_key, monkeypatch) as cli:
            noted = cli.invoke(search, [_QUERY, "--collection", _ALPHA, "--collection", _BETA, "--hyde"])
            as_json = cli.invoke(search, [_QUERY, "--collection", _ALPHA, "--collection", _BETA, "--hyde", "--json"])

    assert noted.exit_code == 0, noted.output
    result_lines = noted.stdout.splitlines()
    assert result_lines and all(line.split("  ")[_COLLECTION_COLUMN] == _ALPHA for line in result_lines)
    notes = noted.stderr.splitlines()
    assert len(notes) == 2, noted.stderr
    assert HYDE_EXPANSION_FAILED_WARNING in notes[0]
    assert _BETA in notes[1] and "embedding_model_mismatch" in notes[1]
    assert as_json.exit_code == 0 and as_json.stderr == ""
    payload = json.loads(as_json.stdout)
    assert len(result_lines) == len(payload["results"])
    assert payload["expansion_warning"] == HYDE_EXPANSION_FAILED_WARNING
    assert payload["excluded_collections"] == [{"name": _BETA, "reason": "embedding_model_mismatch"}]


def test_cli_search_all_skipped_prints_no_results_on_stdout_and_the_reason_on_stderr(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with make_real_app(tmp_path, monkeypatch) as (client, _cfg, api_key):
        _seed(client, api_key, _ALPHA, _BETA)
        store = client.app.state.search_store
        for name in (_ALPHA, _BETA):
            row = client.portal.call(store.get_collection_meta, name)
            client.portal.call(store.update_collection_meta, replace(row, active_embedding_model=_OTHER_MODEL))

        with cli_against_app(client, api_key, monkeypatch) as cli:
            result = cli.invoke(search, [_QUERY, "--collection", _ALPHA, "--collection", _BETA])

    assert result.exit_code == 0, result.output
    assert result.stdout == "No results found.\n"
    assert _ALPHA in result.stderr and _BETA in result.stderr


def test_cli_search_wrong_key_and_empty_query_print_the_real_bare_401_and_422_list_text(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with make_real_app(tmp_path, monkeypatch) as (client, _cfg, api_key):
        with cli_against_app(client, api_key, monkeypatch) as cli:
            wrong_key = cli.invoke(search, [_QUERY, "--collection", _ALPHA], token=_WRONG_KEY)
            empty_query = cli.invoke(search, ["", "--collection", _ALPHA])
        empty_rest = _rest_search(client, api_key, {"collection": _ALPHA, "query": ""})
        assert empty_rest.status_code == 422, empty_rest.text

    assert wrong_key.exit_code == 1 and wrong_key.stdout == ""
    assert f"Error: {_BARE_401_TEXT}\n" in wrong_key.stderr
    assert empty_query.exit_code == 1 and empty_query.stdout == ""
    assert "Error: " in empty_query.stderr and "Value error" not in empty_query.stderr
