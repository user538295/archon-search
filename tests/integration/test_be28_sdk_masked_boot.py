"""BE-28 (S49): with the ``claude_agent_sdk`` module masked, the server boots, generates a description and ``archon-search config show`` works.

The server runs in this process with ``[description]`` enabled and an ingest, so any import of the SDK that the description
path executes would raise; the CLI runs in a fresh interpreter whose mask is installed before ``archon_search`` is imported,
so a module-scope import of the SDK would fail it too.
"""
from __future__ import annotations

import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path

import pytest

from tests._fake_model_server import FakeModelServer, keep_loopback_out_of_proxies
from tests.integration.conftest import ingest_file_via_path, make_real_app

pytestmark = pytest.mark.integration

_SDK_MODULE = "claude_agent_sdk"
_HTTP_OK = 200
_SUBPROCESS_TIMEOUT_SECONDS = 60
_WAIT_SECONDS = 30.0
_POLL_SECONDS = 0.01
_COLLECTION = "masked-sdk-collection"
_DESCRIPTION_REPLY = "A short collection written with the SDK module masked."
_DOCUMENT_REPEATS = 20
_MASK_NOT_ACTIVE_EXIT_CODE = 97
_CONFIG_SHOW_MARKER = "[server]"

_MASKED_CONFIG_SHOW_SCRIPT = f"""\
import sys

sys.modules[{_SDK_MODULE!r}] = None  # import now raises ImportError, as without the package
try:
    import {_SDK_MODULE}
except ImportError:
    pass
else:
    sys.exit({_MASK_NOT_ACTIVE_EXIT_CODE})

from click.testing import CliRunner

from archon_search.cli.main import main

result = CliRunner().invoke(main, ["config", "show"])
print(result.output)
sys.exit(result.exit_code)
"""


def _description_toml(base_url: str) -> str:
    return (
        "[description]\n"
        "enabled = true\n"
        'provider = "ollama"\n'
        'model = "description-model"\n'
        f'ollama_base_url = "{base_url}"\n'
        "ollama_context_tokens = 8192\n"
    )


def _wait_for(condition: Callable[[], bool], what: str) -> None:
    deadline = time.monotonic() + _WAIT_SECONDS
    while not condition():
        if time.monotonic() > deadline:
            raise TimeoutError(f"{what} did not happen within {_WAIT_SECONDS}s")
        time.sleep(_POLL_SECONDS)


def test_server_and_cli_boot_with_sdk_module_masked(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    keep_loopback_out_of_proxies(monkeypatch)
    monkeypatch.setitem(sys.modules, _SDK_MODULE, None)
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    (corpus / "notes.txt").write_text("Archon search keeps local documents searchable.\n" * _DOCUMENT_REPEATS, encoding="utf-8")

    with FakeModelServer(reply_text=_DESCRIPTION_REPLY) as server:
        toml = _description_toml(server.base_url)
        with make_real_app(tmp_path, monkeypatch, toml_content=toml) as (client, _cfg, api_key):
            assert client.get("/health").status_code == _HTTP_OK
            ingest_file_via_path(client, _COLLECTION, str(corpus), api_key=api_key)

            def description_written() -> bool:
                meta = client.portal.call(client.app.state.search_store.get_collection_meta, _COLLECTION)  # type: ignore[union-attr]
                return meta is not None and meta.description == _DESCRIPTION_REPLY

            _wait_for(description_written, "the generated description")

    monkeypatch.setenv("ARCHON_SEARCH_DATA_DIR", str(tmp_path / "cli-data"))
    monkeypatch.setenv("ARCHON_SEARCH_CONFIG", str(tmp_path / "archon-search.toml"))
    result = subprocess.run(
        [sys.executable, "-c", _MASKED_CONFIG_SHOW_SCRIPT],
        capture_output=True,
        text=True,
        timeout=_SUBPROCESS_TIMEOUT_SECONDS,
        check=False,
    )
    assert result.returncode == 0, f"stdout: {result.stdout!r}\nstderr: {result.stderr!r}"
    assert _CONFIG_SHOW_MARKER in result.stdout
