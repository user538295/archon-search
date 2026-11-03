"""Smoke tests for ``archon-search search`` run as a real subprocess against a real server (S67).

The shared session ``smoke_server`` fixture (``conftest.py``) provides the live
``archon-search serve`` subprocess with the pre-seeded ``smoke`` collection. Module-level
``pytestmark`` serialises this file on the one xdist worker that owns the server.
"""

from __future__ import annotations

import json
import os
import re
import subprocess

import pytest

from tests._fake_model_server import unreachable_port

pytestmark = pytest.mark.xdist_group("smoke_e2e")

# Above the CLI's own 65 s request timeout (``CLI_SEARCH_REQUEST_TIMEOUT_SECONDS``), so the CLI fails first.
_CLI_RUN_TIMEOUT_S = 120
_QUERY = "hybrid retrieval"
_COLLECTION = "smoke"
_ESCAPE_CHARACTER = "\x1b"
# One human result line: ``<rank>.  <score>  <collection>  <source path>  <excerpt>`` (cli/search.py ``_result_line``).
_RESULT_LINE = re.compile(
    r"^(?P<rank>\d+)\.  (?P<score>-?\d+\.\d{4})  (?P<collection>\S+)  (?P<path>.+?)  (?P<excerpt>\S.*)$"
)
# The CLI-local excerpt cut (``_EXCERPT_MAX_CHARS`` in cli/search.py).
_EXCERPT_MAX_CHARS = 200
_USAGE_EXIT_CODE = 2
_ERROR_EXIT_CODE = 1
_MISSING_KEY_TEXT = (
    "No API key found. Pass --api-key, set ARCHON_SEARCH_API_KEY, or run the server once to auto-generate a key file."
)


def _search(args: list[str], *, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["uv", "run", "archon-search", "search", *args],
        capture_output=True,
        text=True,
        timeout=_CLI_RUN_TIMEOUT_S,
        check=False,
        env=env,
    )


def _server_args(smoke_server) -> list[str]:
    return ["--api-url", smoke_server.base_url, "--api-key", smoke_server.api_key]


def test_cli_search_human_and_json_against_a_real_server(smoke_server) -> None:
    """Human mode prints one ranked line per result and exits 0; ``--json`` parses with empty stderr and no escapes."""
    human = _search([_QUERY, "--collection", _COLLECTION, *_server_args(smoke_server)])
    as_json = _search([_QUERY, "--collection", _COLLECTION, "--json", *_server_args(smoke_server)])

    assert human.returncode == 0, f"stdout={human.stdout!r} stderr={human.stderr!r}"
    assert as_json.returncode == 0, f"stdout={as_json.stdout!r} stderr={as_json.stderr!r}"

    payload = json.loads(as_json.stdout)
    results = payload["results"]
    assert results, "the seeded corpus must answer the query"

    lines = human.stdout.splitlines()
    assert len(lines) == len(results)
    for rank, (line, result) in enumerate(zip(lines, results, strict=True), start=1):
        match = _RESULT_LINE.match(line)
        assert match, f"not a ranked result line: {line!r}"
        assert int(match["rank"]) == rank
        assert match["score"] == f"{result['score']:.4f}"
        assert match["collection"] == _COLLECTION == result["collection"]
        assert match["path"] == result["source_path"]
        assert match["excerpt"] == " ".join(result["text"].split())[:_EXCERPT_MAX_CHARS]

    assert as_json.stderr == ""
    assert human.stderr == ""
    assert _ESCAPE_CHARACTER not in human.stdout
    assert _ESCAPE_CHARACTER not in as_json.stdout


def test_cli_search_missing_collection_stopped_server_and_missing_key(smoke_server, tmp_path) -> None:
    """Exit 2 without ``--collection``; exit 1 with the not-running text; exit 1 with the missing-key text."""
    no_collection = _search([_QUERY, *_server_args(smoke_server)])
    assert no_collection.returncode == _USAGE_EXIT_CODE, (
        f"stdout={no_collection.stdout!r} stderr={no_collection.stderr!r}"
    )
    assert "Missing option '--collection'" in no_collection.stderr

    with unreachable_port() as dead_port:
        stopped = _search(
            [_QUERY, "--collection", _COLLECTION, "--api-url", f"http://127.0.0.1:{dead_port}", "--api-key", smoke_server.api_key]
        )
    assert stopped.returncode == _ERROR_EXIT_CODE, f"stdout={stopped.stdout!r} stderr={stopped.stderr!r}"
    assert "archon-search serve is not running." in stopped.stderr
    assert stopped.stdout == ""

    # An empty data dir holds no key file, so only the (unset) environment variable could answer.
    keyless_env = {
        k: v for k, v in os.environ.items() if k not in ("ARCHON_SEARCH_API_KEY", "ARCHON_SEARCH_KEY_FILE")
    }
    keyless_env["ARCHON_SEARCH_DATA_DIR"] = str(tmp_path)
    no_key = _search(
        [_QUERY, "--collection", _COLLECTION, "--api-url", smoke_server.base_url], env=keyless_env
    )
    assert no_key.returncode == _ERROR_EXIT_CODE, f"stdout={no_key.stdout!r} stderr={no_key.stderr!r}"
    assert _MISSING_KEY_TEXT in no_key.stderr
    assert no_key.stdout == ""
