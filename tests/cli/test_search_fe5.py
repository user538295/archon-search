"""FE-5 (interface-parity 01-SEARCH, S50): the filter and feature options of ``archon-search search``.

The unit tests patch ``httpx.post`` of ``archon_search.cli.search`` and read the request body the command builds.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch

from click.testing import CliRunner

from archon_search.cli.search import search

_POST = "archon_search.cli.search.httpx.post"


def _ok() -> MagicMock:
    resp = MagicMock()
    resp.status_code = 200
    resp.json.return_value = {"results": []}
    return resp


def _body(args: list[str], collections: tuple[str, ...] = ("docs",)) -> dict[str, Any]:
    selection = [arg for name in collections for arg in ("--collection", name)]
    with patch(_POST, return_value=_ok()) as post:
        result = CliRunner().invoke(search, ["q", *selection, *args, "--api-key", "k"])
    assert result.exit_code == 0, result.output
    return post.call_args.kwargs["json"]


def test_search_nests_flat_filter_options_under_filters() -> None:
    body = _body(
        [
            "--file-type", "md",
            "--source-path-prefix", "/docs/",
            "--source-path-glob", "*.md",
            "--indexed-after", "2024-01-01",
            "--indexed-before", "2025-01-01",
            "--language", "en",
        ]
    )

    assert body["filters"] == {
        "file_type": "md",
        "source_path_prefix": "/docs/",
        "source_path_glob": "*.md",
        "indexed_after": "2024-01-01",
        "indexed_before": "2025-01-01",
        "language": "en",
    }
    assert set(body) == {"query", "collection", "filters"}


def test_search_omits_filters_when_no_filter_option_is_given() -> None:
    body = _body(["--hyde"])

    assert "filters" not in body


def test_search_include_metadata_sets_the_top_level_flag_only() -> None:
    body = _body(["--include-metadata", "--file-type", "md"])

    assert body["include_metadata"] is True
    assert body["filters"] == {"file_type": "md"}


def test_search_omits_unset_flags_and_sends_graph_mode_as_given() -> None:
    plain = _body([])
    assert plain == {"query": "q", "collection": "docs"}

    featured = _body(
        ["--hyde", "--rag-fusion", "--graph-mode", "not-a-mode", "--scope-filter", "a:b", "--acl-context"]
    )
    assert featured == {
        "query": "q",
        "collection": "docs",
        "hyde": True,
        "rag_fusion": True,
        "graph_mode": "not-a-mode",
        "scope_filter": "a:b",
        "acl_context": True,
    }


def test_search_sends_an_empty_string_value_so_the_server_rejects_it() -> None:
    body = _body(["--scope-filter", "", "--file-type", ""])

    assert body["scope_filter"] == ""
    assert body["filters"] == {"file_type": ""}


def test_search_sends_timestamps_as_given_and_filters_with_several_collections() -> None:
    body = _body(["--indexed-after", "2024-01-01T10:00:00+00:00", "--hyde", "--language", "en"], ("a", "b"))

    assert body == {
        "query": "q",
        "collections": ["a", "b"],
        "filters": {"indexed_after": "2024-01-01T10:00:00+00:00", "language": "en"},
        "hyde": True,
    }
