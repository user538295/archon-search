"""FE-6 (interface-parity 01-SEARCH, S19, S20, S50): the ``--top-k`` option of ``archon-search search``.

The unit tests patch ``httpx.post`` of ``archon_search.cli.search``; the 422 answers have the shapes REST sends.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch

import httpx
from click.testing import CliRunner

from archon_search.cli.search import search
from archon_search.services.service_error import top_k_exceeded_detail

_POST = "archon_search.cli.search.httpx.post"
_ARGS = ["q", "--collection", "docs", "--api-key", "k"]


def _ok() -> MagicMock:
    resp = MagicMock()
    resp.status_code = 200
    resp.json.return_value = {"results": []}
    return resp


def _body(extra: list[str]) -> dict[str, Any]:
    with patch(_POST, return_value=_ok()) as post:
        result = CliRunner().invoke(search, [*_ARGS, *extra])
    assert result.exit_code == 0, result.output
    return post.call_args.kwargs["json"]


def test_search_top_k_is_sent_in_the_body_only_when_given() -> None:
    assert "top_k" not in _body([])
    assert _body(["--top-k", "7"])["top_k"] == 7


def test_search_top_k_above_the_maximum_prints_the_422_string_and_exits_1() -> None:
    resp = httpx.Response(422, json={"detail": top_k_exceeded_detail(101, 100)})

    with patch(_POST, return_value=resp):
        result = CliRunner().invoke(search, [*_ARGS, "--top-k", "101"])

    assert result.exit_code == 1
    assert result.stdout == ""
    assert result.stderr == f"Error: {top_k_exceeded_detail(101, 100)}\n"


def test_search_top_k_zero_prints_the_servers_422_text_and_exits_1() -> None:
    detail = [{"loc": ["body", "top_k"], "msg": "Input should be greater than or equal to 1", "type": "x"}]
    resp = httpx.Response(422, json={"detail": detail})

    with patch(_POST, return_value=resp) as post:
        result = CliRunner().invoke(search, [*_ARGS, "--top-k", "0"])

    assert post.call_args.kwargs["json"]["top_k"] == 0
    assert result.exit_code == 1
    assert result.stdout == ""
    assert result.stderr == "Error: Input should be greater than or equal to 1\n"
