"""FE-6 (interface-parity 01-SEARCH, S19, S20): ``--top-k`` reaches the service through the real app."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from archon_search.cli.search import search
from tests._cli_testclient_bridge import cli_against_app
from tests.integration.conftest import ingest_doc, make_real_app

pytestmark = pytest.mark.integration

_COLLECTION = "fe6-docs"
_DOC_COUNT = 6
_TOP_K = 3


def test_cli_search_top_k_returns_exactly_n_results(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    with make_real_app(tmp_path, monkeypatch) as (client, _cfg, api_key):
        for index in range(_DOC_COUNT):
            text = f"# Widgets {index}\n\nThe widget factory number {index} builds a widget every minute.\n"
            ingest_doc(client, _COLLECTION, text, f"/corpus/fe6/widgets_{index}.md", api_key=api_key)
        with cli_against_app(client, api_key, monkeypatch) as cli:
            limited = cli.invoke(search, ["widget", "--collection", _COLLECTION, "--json", "--top-k", str(_TOP_K)])
            invalid = cli.invoke(search, ["widget", "--collection", _COLLECTION, "--top-k", "0"])

    assert limited.exit_code == 0, limited.output
    assert len(json.loads(limited.stdout)["results"]) == _TOP_K
    assert invalid.exit_code == 1
    assert invalid.stdout == ""
    assert invalid.stderr == "Error: Input should be greater than or equal to 1\n"
