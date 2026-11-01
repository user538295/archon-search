"""T-12 (collection-descriptions plan) — startup notices, the ``/status`` block, every provider down, a missing package
(S36, S37, S38, S39, S51, S52, S66, S75; also S34's ``/status`` clause that T-11 left to this task).

Every test runs the real app (lifespan, real LanceDB, real ingest) with a loopback ``FakeModelServer`` or a port nothing
answers on as the model host, and reads what an operator reads: the log (``caplog``), ``GET /status`` and ``GET /ready``
over HTTP, and ``archon-search status`` / ``collection info`` through ``tests/_cli_testclient_bridge.py``. The BE-36 and
BE-37 integration files pin each finding on its own; these tests add the whole-app dimension: several features at once,
the ordering of the probe and an attempt as the operator sees it, and the CLI rendering of the same facts.

* S36, S37, S38, S66 (feature off => block null): fifteen boots over one store that holds a described and an undescribed
  collection, each with its own key and ``[description]`` setting; the off-notice, the hybrid WARNING, the privacy INFO and
  the unusable-provider WARNING appear exactly where the scenarios say and nowhere else, and ``/status`` and the CLI agree
  with the log. A last boot over a fully described store gets no hybrid WARNING.
* S39, S52, S66: ``/status`` while the startup probe is held (reachability ``null``, "not probed yet" in the CLI), after it,
  and after an attempt that works and one that fails; ``/ready`` never changes and never carries the block.
* S34 (``/status`` clause): the provider down with the backfill on: boot, one WARNING, ``/status`` and the CLI say not
  reachable (with the assumed window), and the model is never asked.
* S51: HyDE, RAG Fusion, the graph summary client and descriptions all on dead providers: the server boots, answers
  searches and logs one WARNING per feature; and on a server that accepts and never answers, the boot completes while
  the four features' requests are held.
* S75: a masked ``ollama``, ``anthropic`` or ``openai`` package: one WARNING with the install command (log only),
  ``/status`` not reachable, backfill skipped, every attempt (ingest, describe job) fails visibly.
"""

from __future__ import annotations

import contextlib
import logging
import os
import sys
import threading
import time
from collections.abc import Callable, Iterator
from datetime import datetime
from pathlib import Path
from typing import Any, NamedTuple
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from archon_search.cli import status as status_module
from archon_search.cli.collection import collection as collection_command
from archon_search.cli.status import status as status_command
from archon_search.collection_meta import (
    DESCRIPTION_STATUS_FAILED,
    DESCRIPTION_STATUS_OK,
)
from archon_search.description_lifecycle import GenerationResult
from archon_search.llm_client import llama_cpp_client, once_per_process
from archon_search.platform.service import ServiceStatus
from archon_search.server import description_backfill, description_startup
from archon_search.server.schemas import StatusResponse, WarmupResult
from tests import _fake_claude
from tests._cli_testclient_bridge import CliBridge, cli_against_app
from tests._fake_model_server import (
    FakeModelServer,
    RecordedRequest,
    keep_loopback_out_of_proxies,
    unreachable_port,
)
from tests._graph_engine_stub import install_graph_engine_stub
from tests._install_hint_leak import (
    INSTALL_HINT_PATH,
    INSTALL_HINT_SECRET,
    INSTALL_HINT_SENTINEL,
    patch_install_hint,
)
from tests.integration.conftest import ingest_doc, wait_for_job_done
from tests.integration.conftest import make_real_app as _make_real_app
from tests.integration.test_be37_status_description_block import _PRE_EXISTING_FIELDS

pytestmark = pytest.mark.integration

_MODEL = "description-model"
_CONFIGURED_WINDOW = 8192
_KEY_VARIABLE = "ANTHROPIC_API_KEY"
_OPENAI_KEY_VARIABLE = "OPENAI_API_KEY"
_FAKE_KEY = "sk-test-key"
_ASSUMED_WINDOW = 4096  # S67: what a feature assumes when it cannot learn the window
_FAILED_INGESTS = 2
_NON_LOOPBACK_HOST = "192.0.2.1:11434"  # TEST-NET-1: never answers; the session host guard refuses the connection anyway
_NON_LOOPBACK_LLAMA_HOST = "192.0.2.1:8080"
_WAIT_SECONDS = 60.0
_GATE_HOLD_SECONDS = 60.0  # how long a fake provider holds a request; the test opens the gate long before
_POLL_SECONDS = 0.02
_PROVIDER_TIMEOUT_SECONDS = 30
_DOWN_PROVIDER_TIMEOUT_SECONDS = 2
_CHAT_PATH = "/api/chat"
_TAGS_PATH = "/api/tags"
_SHOW_PATH = "/api/show"
_HTTP_OK = 200
_HTTP_ACCEPTED = 202
_HTTP_UNAVAILABLE = 503
_HTTP_SERVER_ERROR = 500
_LEAVE_THE_MACHINE = "leave the machine"
#: A down llama-server is reported by the one per-server WARNING of ``llama_cpp_client`` (shared with the description probe).
_LOGGERS = {description_startup.__name__, description_backfill.__name__, llama_cpp_client.__name__}
_STARTUP_TASKS = (
    "_startup_sync_task",
    "_llm_window_task",
    "_description_probe_task",
    "_description_backfill_task",
    "_description_hybrid_task",
)
_DESCRIBED = "described-notes"
_PLAIN = "plain-notes"
_SEEDED = (_DESCRIBED, _PLAIN)
_HYBRID_COUNT_TEXT = f"1 of {len(_SEEDED)}"
_NOTE_TEXT = "Field notes about how the survey was carried out.\n" * 5
_MANUAL_TEXT = "Hand written note."
_REPLY = "Notes written for the status block."
_SEARCH_QUERY = "survey notes"
_SEARCH_TEXT = "Survey notes: the migration runbook describes the rollout."
_SEARCH_PATH = "/docs/survey.md"
_SEARCHES_PER_EXPANSION = 2
_EXPANSIONS = ("hyde", "rag_fusion")
_NOT_PROBED_LINE = "reachable (startup probe): not probed yet"
_REACHABLE_YES_LINE = "reachable (startup probe): yes"
_REACHABLE_NO_LINE = "reachable (startup probe): no"
_NO_ATTEMPT_LINE = "last attempt: none since start"
_HYBRID_ROUTING = '[routing]\nrouting_strategy = "hybrid"\n'
_DISABLED_SECTION = "[description]\nenabled = false\n"
_HYDE_LOGGER = "archon_search.hyde"
_RAG_LOGGER = "archon_search.rag_fusion"
_GRAPH_PROBE_LOGGER = "archon_search.model_validation"


@pytest.fixture(autouse=True)
def _no_proxy_for_loopback(monkeypatch: pytest.MonkeyPatch) -> None:
    keep_loopback_out_of_proxies(monkeypatch)


@pytest.fixture(autouse=True)
def _service_manager_reports_running(monkeypatch: pytest.MonkeyPatch) -> None:
    """``archon-search status`` first asks the OS service manager, which does not exist under a test."""
    service = MagicMock()
    service.status.return_value = ServiceStatus(running=True, pid=None, uptime_seconds=None)
    monkeypatch.setattr(status_module, "_get_service", lambda: service)


def _wait_for(condition: Callable[[], bool], what: str, *, deadline: float | None = None) -> None:
    """Poll *condition* until *deadline* (a ``time.monotonic()`` value; default ``_WAIT_SECONDS`` from now)."""
    limit = time.monotonic() + _WAIT_SECONDS if deadline is None else deadline
    while not condition():
        assert time.monotonic() < limit, f"{what} did not happen within {_WAIT_SECONDS}s"
        time.sleep(_POLL_SECONDS)


@contextlib.contextmanager
def _booted(
    *args: Any, release: Callable[[], None] | None = None, **kwargs: Any
) -> Iterator[tuple[TestClient, str]]:
    """``make_real_app`` that lets every provider-calling startup task end before the app shuts down: a task cancelled
    mid-request leaves a pooled connection that is closed on a dead loop at garbage collection and fails a later test.
    *release* opens whatever gate holds a provider request; it runs first, so a failing test does not wait for a held call."""
    with _make_real_app(*args, **kwargs) as (client, _config, admin_key):
        try:
            yield client, admin_key
        finally:
            if release is not None:
                release()
            deadline = time.monotonic() + _WAIT_SECONDS  # one budget for the whole teardown, however many tasks lag
            state = client.app.state
            for name in _STARTUP_TASKS:
                task = getattr(state, name, None)
                if task is not None:
                    _wait_for(task.done, f"the {name} to end", deadline=deadline)
            # The model validation has no task handle: readiness is its end.
            _wait_for(lambda: client.get("/ready").status_code == _HTTP_OK, "readiness", deadline=deadline)


def _headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _status(client: TestClient, token: str) -> dict[str, Any]:
    response = client.get("/status", headers=_headers(token))
    assert response.status_code == _HTTP_OK, response.text
    return response.json()


def _block(client: TestClient, token: str) -> dict[str, Any]:
    block = _status(client, token)["description"]
    assert isinstance(block, dict)
    return block


def _ready_when_validated(client: TestClient) -> None:
    """``/ready`` answers 200 once the startup model validation ended; it is never held back by a description provider."""
    _wait_for(lambda: client.get("/ready").status_code == _HTTP_OK, "readiness")


def _assert_ready_ignores_description(client: TestClient, token: str) -> None:
    """S39: ``/ready`` is 200, carries no description field, and the model validation holds no description warning."""
    _ready_when_validated(client)
    assert "description" not in client.get("/ready").json()
    warnings = _status(client, token)["model_validation"]["provider_warnings"]
    assert not [w for w in warnings if "description" in w.lower()], warnings


def _task(client: TestClient, name: str) -> Any:
    task = getattr(client.app.state, name)
    assert task is not None, f"{name} was not started"
    return task


def _probed(client: TestClient) -> Any:
    _wait_for(lambda: client.app.state.description_reachability is not None, "the startup probe")
    return client.app.state.description_reachability


def _cli_status(cli: CliBridge) -> str:
    result = cli.invoke(status_command, [])
    assert result.exit_code == 0, result.output
    return result.output


def _cli_info(cli: CliBridge, name: str) -> str:
    result = cli.invoke(collection_command, ["info", name])
    assert result.exit_code == 0, result.output
    return result.output


def _description_toml(
    base_url: str,
    *,
    extra: str = "",
    timeout_seconds: float = _PROVIDER_TIMEOUT_SECONDS,
    window: int | None = _CONFIGURED_WINDOW,
) -> str:
    context = "" if window is None else f"ollama_context_tokens = {window}\n"
    return (
        "[description]\n"
        "enabled = true\n"
        'provider = "ollama"\n'
        f'model = "{_MODEL}"\n'
        f'ollama_base_url = "{base_url}"\n'
        f"{context}"
        f"timeout_seconds = {timeout_seconds}\n"
        f"{extra}"
    )


def _messages(caplog: pytest.LogCaptureFixture, level: int) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.name in _LOGGERS and r.levelno == level]


def _unreachable_warnings(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [m for m in _messages(caplog, logging.WARNING) if "not reachable" in m]


def _seed_two_collections(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """One boot with descriptions off: ``described-notes`` gets a manual description, ``plain-notes`` stays empty."""
    with _booted(tmp_path, monkeypatch, toml_content=_DISABLED_SECTION) as (client, admin_key):
        for name in _SEEDED:
            ingest_doc(client, name, _NOTE_TEXT, f"/docs/{name}.md", api_key=admin_key)
        _set_manual_description(client, admin_key, _DESCRIBED)


def _set_manual_description(client: TestClient, token: str, name: str) -> None:
    response = client.put(
        f"/collections/{name}/description", json={"description": _MANUAL_TEXT}, headers=_headers(token)
    )
    assert response.status_code == _HTTP_OK, response.text


class _Findings:
    """What the log of one boot says about the description feature."""

    def __init__(self, caplog: pytest.LogCaptureFixture) -> None:
        infos = _messages(caplog, logging.INFO)
        warnings = _messages(caplog, logging.WARNING)
        self.off_notices = [m for m in infos if "Collection descriptions are off" in m]
        self.privacy = [m for m in infos if _LEAVE_THE_MACHINE in m]
        self.hybrid = [m for m in warnings if "hybrid" in m]
        self.unreachable = [m for m in warnings if "not reachable" in m]
        self.other_warnings = [m for m in warnings if m not in self.hybrid and m not in self.unreachable]


class _Boot(NamedTuple):
    """One boot of the notices matrix and what each surface must say about it."""

    name: str
    toml: str
    key_set: bool = True
    openai_key_set: bool = True
    off_notice: bool = False
    hybrid: bool = True
    #: Where the privacy INFO says the excerpts go; ``None`` = no such INFO.
    privacy: str | None = None
    #: Text the one unreachable WARNING must carry; ``None`` = no such WARNING.
    unreachable: str | None = None
    #: Fields ``/status`` ``description`` must show (the feature is on); ``None`` = the block is null and the CLI prints none.
    block: dict[str, Any] | None = None
    #: Text the CLI status must print besides the block (the key warning goes to stderr).
    cli_text: str | None = None


def _notices_matrix(server_url: str) -> list[_Boot]:
    cloud = '[description]\nenabled = true\nprovider = "{}"\n'
    llama = '[description]\nenabled = true\nprovider = "llama_cpp"\nllama_cpp_base_url = "{}"\n'
    return [
        _Boot("section absent, key set", _HYBRID_ROUTING, off_notice=True),
        _Boot("hybrid off, section absent, key set", "[search]\n", off_notice=True, hybrid=False),
        _Boot("section absent, no key", _HYBRID_ROUTING, key_set=False),
        _Boot("enabled = false", _HYBRID_ROUTING + _DISABLED_SECTION),
        _Boot(
            "enabled = false on a provider that is unusable",
            _HYBRID_ROUTING + _DISABLED_SECTION + 'provider = "anthropic"\n',
            key_set=False,
        ),
        _Boot(
            "anthropic with key",
            _HYBRID_ROUTING + cloud.format("anthropic"),
            privacy="provider anthropic",
            block={"provider": "anthropic", "key_available": True, "reachable": True, "effective_context_tokens": None},
        ),
        _Boot(
            "anthropic without key",
            _HYBRID_ROUTING + cloud.format("anthropic"),
            key_set=False,
            privacy="provider anthropic",
            unreachable=_KEY_VARIABLE,
            block={"provider": "anthropic", "key_available": False, "reachable": False},
            cli_text=f"collection descriptions enabled but {_KEY_VARIABLE} is not set",
        ),
        _Boot(
            "openai with both keys",
            _HYBRID_ROUTING + cloud.format("openai"),
            privacy="provider openai",
            block={"provider": "openai", "key_available": True, "reachable": True},
        ),
        _Boot(
            "openai with only its own key",
            _HYBRID_ROUTING + cloud.format("openai"),
            key_set=False,
            privacy="provider openai",
            block={"provider": "openai", "key_available": True, "reachable": True},
        ),
        _Boot(
            "openai without its key",
            _HYBRID_ROUTING + cloud.format("openai"),
            openai_key_set=False,
            privacy="provider openai",
            unreachable=_OPENAI_KEY_VARIABLE,
            block={"provider": "openai", "key_available": False, "reachable": False},
            cli_text=f"collection descriptions enabled but {_OPENAI_KEY_VARIABLE} is not set",
        ),
        _Boot(
            "loopback ollama",
            _HYBRID_ROUTING + _description_toml(server_url),
            block={
                "provider": "ollama",
                "key_available": True,
                "reachable": True,
                "effective_context_tokens": _CONFIGURED_WINDOW,
                "context_window_source": "configured",
            },
        ),
        _Boot(
            "remote ollama",
            _HYBRID_ROUTING + _description_toml(f"http://{_NON_LOOPBACK_HOST}"),
            privacy=f"the ollama server at {_NON_LOOPBACK_HOST}",
            unreachable=_NON_LOOPBACK_HOST,
            block={"provider": "ollama", "reachable": False},
        ),
        _Boot(
            "loopback llama_cpp",
            _HYBRID_ROUTING + llama.format(server_url),
            block={"provider": "llama_cpp", "key_available": True, "reachable": True, "context_window_source": "probed"},
        ),
        _Boot(
            "remote llama_cpp",
            _HYBRID_ROUTING + llama.format(f"http://{_NON_LOOPBACK_LLAMA_HOST}"),
            privacy=f"the llama_cpp server at {_NON_LOOPBACK_LLAMA_HOST}",
            unreachable=_NON_LOOPBACK_LLAMA_HOST,
            block={
                "provider": "llama_cpp",
                "reachable": False,
                "effective_context_tokens": _ASSUMED_WINDOW,
                "context_window_source": "assumed",
            },
        ),
        _Boot(
            "claude_cli",
            _HYBRID_ROUTING + '[description]\nenabled = true\nprovider = "claude_cli"\nmodel = "claude-fake"\n',
            privacy="provider claude_cli",
            block={"provider": "claude_cli", "key_available": True, "reachable": True},
        ),
    ]


def _assert_boot_findings(boot: _Boot, found: _Findings) -> None:
    name = boot.name
    assert len(found.off_notices) == (1 if boot.off_notice else 0), (name, found.off_notices)
    if boot.off_notice:
        assert "enabled = true" in found.off_notices[0], found.off_notices  # S36: says how to turn descriptions on
    if boot.hybrid:
        assert len(found.hybrid) == 1, (name, found.hybrid)
        assert "centroid" in found.hybrid[0] and _HYBRID_COUNT_TEXT in found.hybrid[0], (name, found.hybrid)
    else:
        assert found.hybrid == [], (name, found.hybrid)
    if boot.privacy is None:
        assert found.privacy == [], (name, found.privacy)
    else:
        assert len(found.privacy) == 1 and boot.privacy in found.privacy[0], (name, found.privacy)
        assert "retry" in found.privacy[0], found.privacy  # S38: a retry sends the excerpts again
    if boot.unreachable is None:
        assert found.unreachable == [], (name, found.unreachable)
    else:
        assert len(found.unreachable) == 1 and boot.unreachable in found.unreachable[0], (name, found.unreachable)
    assert found.other_warnings == [], (name, found.other_warnings)


def _assert_boot_surfaces(boot: _Boot, client: TestClient, key: str, cli: CliBridge) -> None:
    """``/status`` and the CLI agree with what the boot logged (S66: the block is null when the feature is off)."""
    if boot.block is None:
        assert _status(client, key)["description"] is None, boot.name
        assert "Collection descriptions:" not in _cli_status(cli), boot.name
        return
    _probed(client)
    _wait_for(_task(client, "_llm_window_task").done, "the context-window resolution")
    block = _block(client, key)
    assert {field: block[field] for field in boot.block} == boot.block, (boot.name, block)
    # Nothing was attempted and the backfill is off on every row: no stray last attempt, no stray flag.
    assert (block["last_attempt_ok"], block["last_attempt_at"], block["backfill_on_startup"]) == (None, None, False)
    cli_output = _cli_status(cli)
    assert f"provider: {boot.block['provider']}" in cli_output, (boot.name, cli_output)
    if boot.cli_text is not None:
        assert boot.cli_text in cli_output, (boot.name, cli_output)
    else:
        assert "is not set" not in cli_output, (boot.name, cli_output)


def test_e2e_notices_and_warnings_for_enabled_unusable_and_hybrid_setups(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """S36, S37, S38: over one store with a described and an undescribed collection and fifteen settings of the key,
    the routing and ``[description]``, each finding shows exactly where the scenarios say. The hybrid WARNING is the same
    on every hybrid boot (it counts collections without a description whatever the feature does); the off-notice needs the
    key and no section; the privacy INFO names a cloud provider or a non-loopback host and never a loopback one; an
    unusable provider is one WARNING that names the cause; ``/status`` and the CLI show the block only when the feature is
    on. Once every collection is described the hybrid WARNING is gone."""
    _seed_two_collections(tmp_path, monkeypatch)
    fake_claude = _fake_claude.install(tmp_path / "bin")
    monkeypatch.setenv("PATH", f"{fake_claude.path.parent}{os.pathsep}{os.environ['PATH']}")

    with FakeModelServer(reply_text=_REPLY) as server:
        for boot in _notices_matrix(server.base_url):
            caplog.clear()
            for variable, is_set in ((_KEY_VARIABLE, boot.key_set), (_OPENAI_KEY_VARIABLE, boot.openai_key_set)):
                if is_set:
                    monkeypatch.setenv(variable, _FAKE_KEY)
                else:
                    monkeypatch.delenv(variable, raising=False)
            once_per_process._warned_keys.clear()  # each boot is a fresh process as far as warn-once goes
            with (
                caplog.at_level(logging.INFO),
                _booted(tmp_path, monkeypatch, toml_content=boot.toml) as (client, key),
                cli_against_app(client, key, monkeypatch) as cli,
            ):
                assert client.get("/health").status_code == _HTTP_OK, boot.name
                _assert_boot_surfaces(boot, client, key, cli)
            _assert_boot_findings(boot, _Findings(caplog))
        assert server.requests_to(_CHAT_PATH) == [], "no boot may ask the model for a description"
        assert len(server.requests_to(_TAGS_PATH)) == 1, "only the loopback ollama boot probes the Ollama model list"

        # Every collection described: nothing is left to fall back, so hybrid routing has nothing to warn about.
        caplog.clear()
        with _booted(tmp_path, monkeypatch, toml_content=_DISABLED_SECTION) as (client, key):
            _set_manual_description(client, key, _PLAIN)
        with caplog.at_level(logging.INFO), _booted(tmp_path, monkeypatch, toml_content=_HYBRID_ROUTING):
            pass
        assert _Findings(caplog).hybrid == []


def _make_probe_patient(monkeypatch: pytest.MonkeyPatch) -> None:
    """Let the startup probe wait for as long as a test holds its request (the production bound is 5 s, which a loaded
    machine can exceed between boot and the assertions that need the probe still in flight)."""
    real_probe = description_backfill.probe_description_reachability

    async def patient_probe(description: Any) -> Any:
        return await real_probe(description, timeout_seconds=_GATE_HOLD_SECONDS)

    monkeypatch.setattr(description_backfill, "probe_description_reachability", patient_probe)


class _ProbeGatedServer(FakeModelServer):
    """A ``FakeModelServer`` that answers the reachability probe (``GET /api/tags``) only when the test lets it, and can
    be told to fail every chat call."""

    def __init__(self, **options: Any) -> None:
        super().__init__(**options)
        self._probe_gate = threading.Event()
        self.fail_chat = False

    def respond(self, request: RecordedRequest) -> tuple[int, dict[str, Any]]:
        if request.path == _TAGS_PATH:
            self._probe_gate.wait(timeout=_GATE_HOLD_SECONDS)
        elif request.path == _CHAT_PATH and self.fail_chat:
            return self.error(request.path, _HTTP_SERVER_ERROR, "the model crashed")
        return super().respond(request)

    def release_probe(self) -> None:
        self._probe_gate.set()

    def __exit__(self, *exc_info: object) -> None:
        self.release_probe()
        super().__exit__(*exc_info)


def test_e2e_status_description_block_before_and_after_probe_and_attempt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """S39, S52, S66: while the startup probe is held ``/status`` and the CLI say reachability is not known yet and no
    attempt happened; after the probe it is known; an attempt that works and then one that fails move only the last attempt
    (the probe's verdict is never refreshed); ``/ready`` answers 200 throughout and never carries the block."""
    _make_probe_patient(monkeypatch)
    with (
        _ProbeGatedServer(reply_text=_REPLY) as server,
        _booted(
            tmp_path, monkeypatch, toml_content=_description_toml(server.base_url), release=server.release_probe
        ) as (client, key),
        cli_against_app(client, key, monkeypatch) as cli,
    ):
        _wait_for(lambda: len(server.requests_to(_TAGS_PATH)) >= 1, "the probe request")
        assert client.app.state.description_reachability is None

        # Before the probe answers.
        _ready_when_validated(client)  # readiness does not wait for the probe
        held = _block(client, key)
        assert held == {
            "provider": "ollama",
            "key_available": True,
            "reachable": None,
            "effective_context_tokens": _CONFIGURED_WINDOW,
            "context_window_source": "configured",
            "backfill_on_startup": False,
            "last_attempt_ok": None,
            "last_attempt_at": None,
        }
        held_cli = _cli_status(cli)
        assert "Collection descriptions:" in held_cli
        assert "provider: ollama" in held_cli and "key: available" in held_cli
        assert f"context window: {_CONFIGURED_WINDOW} tokens (configured)" in held_cli
        assert _NOT_PROBED_LINE in held_cli and _NO_ATTEMPT_LINE in held_cli

        # After the probe.
        server.release_probe()
        assert _probed(client).reachable is True
        probed = _block(client, key)
        assert probed["reachable"] is True and probed["last_attempt_ok"] is None
        probed_cli = _cli_status(cli)
        assert _REACHABLE_YES_LINE in probed_cli and _NO_ATTEMPT_LINE in probed_cli

        # After an attempt that works (a first ingest asks for the description).
        ingest_doc(client, _PLAIN, _NOTE_TEXT, f"/docs/{_PLAIN}.md", api_key=key)
        _wait_for(lambda: _block(client, key)["last_attempt_ok"] is True, "the first attempt to be recorded")
        worked = _block(client, key)
        collection = client.get(f"/collections/{_PLAIN}", headers=_headers(key)).json()
        assert (collection["description"], collection["description_status"]) == (_REPLY, DESCRIPTION_STATUS_OK)
        worked_at = datetime.fromisoformat(worked["last_attempt_at"])
        assert worked["reachable"] is True
        assert f"last attempt: ok at {worked['last_attempt_at']}" in _cli_status(cli)

        # After an attempt that fails: only the last attempt changes.
        chats_before = len(server.requests_to(_CHAT_PATH))
        server.fail_chat = True
        response = client.post(f"/collections/{_PLAIN}/describe", headers=_headers(key))
        assert response.status_code == _HTTP_ACCEPTED, response.text
        job = wait_for_job_done(client, response.json()["job_id"], what="describe")
        assert job.result["outcome"] == GenerationResult.FAILED.value, job.result
        assert len(server.requests_to(_CHAT_PATH)) == chats_before + 1  # the model was asked and failed
        failed = _block(client, key)
        assert failed["last_attempt_ok"] is False
        assert datetime.fromisoformat(failed["last_attempt_at"]) > worked_at
        assert failed["reachable"] is True  # the startup verdict is not refreshed
        assert {k: v for k, v in failed.items() if not k.startswith("last_attempt")} == {
            k: v for k, v in worked.items() if not k.startswith("last_attempt")
        }
        assert f"last attempt: failed at {failed['last_attempt_at']}" in _cli_status(cli)
        assert len(server.requests_to(_TAGS_PATH)) == 1  # the probe ran once at startup and never again

        # Never in /ready, and every field a consumer that predates the block reads is still there.
        _assert_ready_ignores_description(client, key)
        body = _status(client, key)
        assert _PRE_EXISTING_FIELDS <= body.keys()
        legacy = {k: v for k, v in body.items() if k != "description"}
        assert StatusResponse.model_validate(legacy).description is None


def test_e2e_backfill_provider_down_status_and_cli_report_not_ok(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """S34 (``/status`` clause), S39, S66: backfill on, the provider answers HTTP 503 to the probe and the window is not
    configured: the server boots, logs one WARNING, ``/status`` and ``archon-search status`` report the provider not
    reachable with the assumed window, ``/ready`` stays 200 with no description warning, and the backfill, which waits for
    the probe, never asks the model although an empty collection waits."""
    _seed_two_collections(tmp_path, monkeypatch)
    with FakeModelServer(reply_text=_REPLY, status_overrides={_TAGS_PATH: _HTTP_UNAVAILABLE}) as server:
        toml = _description_toml(server.base_url, extra="backfill_on_startup = true\n", window=None)
        with (
            caplog.at_level(logging.INFO),
            _booted(tmp_path, monkeypatch, toml_content=toml) as (client, key),
            cli_against_app(client, key, monkeypatch) as cli,
        ):
            assert _probed(client).reachable is False
            _wait_for(_task(client, "_description_backfill_task").done, "the backfill task")
            _wait_for(_task(client, "_llm_window_task").done, "the context-window resolution")
            _assert_ready_ignores_description(client, key)
            block = _block(client, key)
            cli_output = _cli_status(cli)
            plain = client.get(f"/collections/{_PLAIN}", headers=_headers(key)).json()

    assert block["reachable"] is False and block["backfill_on_startup"] is True
    assert block["last_attempt_ok"] is None  # nothing was attempted
    assert (block["effective_context_tokens"], block["context_window_source"]) == (_ASSUMED_WINDOW, "assumed")
    assert _REACHABLE_NO_LINE in cli_output and "backfill on startup: yes" in cli_output
    assert f"context window: {_ASSUMED_WINDOW} tokens (assumed)" in cli_output
    assert len(server.requests_to(_TAGS_PATH)) == 1  # the one probe, which the 503 failed
    assert server.requests_to(_CHAT_PATH) == []
    assert (plain["description"], plain["description_status"]) == ("", None)
    unreachable = _unreachable_warnings(caplog)
    assert len(unreachable) == 1 and "HTTP 503" in unreachable[0], unreachable
    skipped = [m for m in _messages(caplog, logging.INFO) if "backfill skipped" in m]
    assert len(skipped) == 1 and "not reachable" in skipped[0], skipped


def _down_toml(url: str, section: str, extra: str = "", *, model_key: str = "model") -> str:
    return (
        f"[{section}]\n"
        "enabled = true\n"
        'provider = "ollama"\n'
        f'{model_key} = "{_MODEL}"\n'
        f'ollama_base_url = "{url}"\n'
        f"ollama_context_tokens = {_CONFIGURED_WINDOW}\n"
        f"{extra}"
    )


def _four_features_toml(url: str) -> str:
    """HyDE, RAG Fusion, the graph summary client and descriptions, all on one Ollama server, with short call timeouts."""
    timeout = f"timeout_seconds = {_DOWN_PROVIDER_TIMEOUT_SECONDS}\n"
    return (
        _down_toml(url, "hyde", timeout)
        + _down_toml(url, "rag_fusion", timeout)
        + _down_toml(
            url, "graph", f"extraction_timeout_seconds = {_DOWN_PROVIDER_TIMEOUT_SECONDS}\n", model_key="extraction_model"
        )
        + _description_toml(url, extra="backfill_on_startup = true\n", timeout_seconds=_DOWN_PROVIDER_TIMEOUT_SECONDS)
    )


class _HangingServer(FakeModelServer):
    """A ``FakeModelServer`` that accepts every request and answers none until the test opens the gate."""

    def __init__(self, **options: Any) -> None:
        super().__init__(**options)
        self._gate = threading.Event()

    def respond(self, request: RecordedRequest) -> tuple[int, dict[str, Any]]:
        self._gate.wait(timeout=_GATE_HOLD_SECONDS)
        return super().respond(request)

    def open_gate(self) -> None:
        self._gate.set()

    def __exit__(self, *exc_info: object) -> None:
        self.open_gate()
        super().__exit__(*exc_info)


def test_e2e_every_llm_provider_down_boots_normally(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """S51: HyDE, RAG Fusion, the graph summary client and descriptions all point at a dead server. The server boots,
    ``/health`` and ``/ready`` answer, searches with each expansion still return the hit (the plain query), and each
    feature logs exactly one WARNING however many searches fail. On a server that accepts and never answers, the boot
    completes while the requests of the four features are held (it never waits for a provider)."""
    import ollama  # noqa: F401  # the first call of a feature would count this import against its timeout

    install_graph_engine_stub(monkeypatch, entities=lambda text: [])
    with (
        unreachable_port() as port,
        caplog.at_level(logging.INFO),
        _booted(tmp_path, monkeypatch, toml_content=_four_features_toml(f"http://127.0.0.1:{port}")) as (client, key),
    ):
        assert client.get("/health").status_code == _HTTP_OK
        assert _probed(client).reachable is False
        _ready_when_validated(client)
        ingest_doc(client, "survey", _SEARCH_TEXT, _SEARCH_PATH, api_key=key)
        _wait_for(lambda: client.app.state.warmup_result == WarmupResult.DONE, "the lifespan warm-up")
        # HyDE is suppressed by a request that asks for RAG Fusion, so each expansion gets its own searches.
        answers = [
            (
                expansion,
                client.post(
                    "/search",
                    json={"query": _SEARCH_QUERY, "collection": "survey", expansion: True},
                    headers=_headers(key),
                ),
            )
            for expansion in _EXPANSIONS
            for _ in range(_SEARCHES_PER_EXPANSION)
        ]
        _wait_for(lambda: not client.app.state.description_lifecycle.is_pending("survey"), "the failed first description")
        block = _block(client, key)
        _assert_ready_ignores_description(client, key)

    for expansion, answer in answers:
        assert answer.status_code == _HTTP_OK, answer.text
        data = answer.json()
        assert data[f"{expansion}_applied"] is False
        assert [hit["source_path"] for hit in data["results"]] == [_SEARCH_PATH]
    assert block["reachable"] is False and block["last_attempt_ok"] is False
    warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]

    def logged_by(logger_name: str, *words: str) -> list[str]:
        return [r.getMessage() for r in warnings if r.name == logger_name and all(w in r.getMessage() for w in words)]

    assert len(logged_by(_HYDE_LOGGER, "HyDE generation failed")) == 1
    assert len(logged_by(_RAG_LOGGER, "RAG Fusion generation failed")) == 1
    assert len(logged_by(_GRAPH_PROBE_LOGGER, "extraction model probe")) == 1
    assert len(_unreachable_warnings(caplog)) == 1, _unreachable_warnings(caplog)

    held_dir = tmp_path / "held"
    held_dir.mkdir()
    _make_probe_patient(monkeypatch)
    with (
        _HangingServer() as server,
        _booted(held_dir, monkeypatch, toml_content=_four_features_toml(server.base_url), release=server.open_gate) as (
            client,
            _key,
        ),
    ):
        # Reaching this point is the boot completing; the requests of the startup tasks are still held on the server.
        _wait_for(
            lambda: bool(server.requests_to(_TAGS_PATH) and server.requests_to(_SHOW_PATH)), "the held startup requests"
        )
        assert client.get("/health").status_code == _HTTP_OK
        assert client.app.state.description_reachability is None  # the probe is waiting for its answer
        assert not _task(client, "_llm_window_task").done()  # and so is the context-window resolution


@pytest.mark.parametrize("provider", ["ollama", "anthropic", "openai"])
def test_e2e_missing_description_package_fails_visibly(
    provider: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """S75: ``[description]`` on a provider whose package is masked. The server boots with one WARNING that names the
    package and carries the install command (the log only), ``/status`` and the CLI say not reachable, the backfill is
    skipped although a collection waits, and every attempt (ingest, describe job) fails visibly: status ``failed`` on REST
    and in ``collection info``, ``last_attempt_ok`` false, one generation WARNING per process, nothing sent to the model
    server."""
    monkeypatch.setenv(_KEY_VARIABLE, _FAKE_KEY)
    monkeypatch.setenv(_OPENAI_KEY_VARIABLE, _FAKE_KEY)
    _seed_two_collections(tmp_path, monkeypatch)
    patch_install_hint(monkeypatch, "archon_search.server.provider_dependency")
    monkeypatch.setitem(sys.modules, provider, None)
    with FakeModelServer(reply_text=_REPLY) as server, caplog.at_level(logging.INFO):
        if provider == "ollama":
            toml = _description_toml(server.base_url, extra="backfill_on_startup = true\n")
        else:
            toml = f'[description]\nenabled = true\nprovider = "{provider}"\nbackfill_on_startup = true\n'
        with (
            _booted(tmp_path, monkeypatch, toml_content=toml) as (client, key),
            cli_against_app(client, key, monkeypatch) as cli,
        ):
            assert client.get("/health").status_code == _HTTP_OK
            verdict = _probed(client)
            assert verdict.reachable is False and provider in verdict.reason
            _wait_for(_task(client, "_description_backfill_task").done, "the backfill task")
            _assert_ready_ignores_description(client, key)
            before = _block(client, key)
            before_cli = _cli_status(cli)
            untouched = client.get(f"/collections/{_PLAIN}", headers=_headers(key)).json()  # the backfill skipped it

            for attempt in range(_FAILED_INGESTS):
                name = f"fresh-notes-{attempt}"
                ingest_doc(client, name, _NOTE_TEXT, f"/docs/{name}.md", api_key=key)
                _wait_for(
                    lambda name=name: client.get(f"/collections/{name}", headers=_headers(key)).json()[
                        "description_status"
                    ]
                    == DESCRIPTION_STATUS_FAILED,
                    f"status failed of {name}",
                )
            response = client.post(f"/collections/{_PLAIN}/describe", headers=_headers(key))
            assert response.status_code == _HTTP_ACCEPTED, response.text
            describe = wait_for_job_done(client, response.json()["job_id"], what="describe")
            after = _block(client, key)
            after_cli = _cli_status(cli)
            info = _cli_info(cli, "fresh-notes-0")
            plain = client.get(f"/collections/{_PLAIN}", headers=_headers(key)).json()
            status_text = client.get("/status", headers=_headers(key)).text

    assert describe.result["outcome"] == GenerationResult.FAILED.value, describe.result
    assert before["reachable"] is False and before["last_attempt_ok"] is None
    assert after["reachable"] is False and after["last_attempt_ok"] is False
    assert _REACHABLE_NO_LINE in before_cli and _NO_ATTEMPT_LINE in before_cli
    assert _REACHABLE_NO_LINE in after_cli and "last attempt: failed at" in after_cli
    assert f"description_status: {DESCRIPTION_STATUS_FAILED}" in info
    assert (untouched["description"], untouched["description_status"]) == ("", None)
    assert plain["description_status"] == DESCRIPTION_STATUS_FAILED  # the describe job failed visibly
    if provider == "ollama":  # the cloud rows have no model server to receive a request in the first place
        assert server.requests == []
    for surface in (status_text, before_cli, after_cli, info):
        assert INSTALL_HINT_SECRET not in surface and INSTALL_HINT_PATH not in surface, "the install command leaked"
    package_warnings = [m for m in _messages(caplog, logging.WARNING) if "package is not installed" in m]
    assert len(package_warnings) == 1 and f"'{provider}'" in package_warnings[0], package_warnings
    assert INSTALL_HINT_SENTINEL in package_warnings[0]
    skipped = [m for m in _messages(caplog, logging.INFO) if "backfill skipped" in m]
    assert len(skipped) == 1 and "not reachable" in skipped[0], skipped
    failures = [
        r.getMessage()
        for r in caplog.records
        if r.levelno == logging.WARNING and "Description generation failed" in r.getMessage()
    ]
    assert len(failures) == 1, failures
