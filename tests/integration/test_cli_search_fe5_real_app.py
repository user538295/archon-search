"""FE-5 (interface-parity 01-SEARCH, S50): the filter and feature options reach the service through the real app."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from archon_search.cli.search import search
from tests._cli_testclient_bridge import cli_against_app
from tests.integration.conftest import ingest_doc, make_real_app

pytestmark = pytest.mark.integration

_COLLECTION = "fe5-docs"
_PATH = "/corpus/fe5/widgets.md"
_DOC_TEXT = "# Widgets\n\nThe widget factory builds a widget every minute.\n"


def test_cli_search_filters_and_feature_flags_reach_the_service(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with make_real_app(tmp_path, monkeypatch) as (client, _cfg, api_key):
        ingest_doc(client, _COLLECTION, _DOC_TEXT, _PATH, api_key=api_key)
        with cli_against_app(client, api_key, monkeypatch) as cli:
            plain = cli.invoke(search, ["widget", "--collection", _COLLECTION, "--json"])
            filtered = cli.invoke(
                search,
                [
                    "widget", "--collection", _COLLECTION, "--json",
                    "--file-type", "md", "--source-path-prefix", "/corpus/fe5/", "--acl-context",
                ],
            )
            unmatched = cli.invoke(
                search, ["widget", "--collection", _COLLECTION, "--json", "--file-type", "pdf"]
            )

    assert plain.exit_code == 0, plain.output
    plain_payload = json.loads(plain.stdout)
    assert plain_payload["results"]
    assert plain_payload["applied_filters"] is None
    assert all(hit["acl_gate"] is None for hit in plain_payload["results"])

    assert filtered.exit_code == 0, filtered.output
    payload = json.loads(filtered.stdout)
    assert payload["results"], "the seeded document must match the filters"
    assert payload["applied_filters"]["file_type"] == "md"
    assert payload["applied_filters"]["source_path_prefix"] == "/corpus/fe5/"
    assert all(hit["acl_gate"] is not None for hit in payload["results"])
    assert unmatched.exit_code == 0, unmatched.output
    assert json.loads(unmatched.stdout)["results"] == []
