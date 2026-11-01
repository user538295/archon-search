"""BE-35 (collection-descriptions plan, C5, S33, S34, S86): the description reachability probe and the startup backfill loop.

The loop and the probe are plain coroutines over ``app.state``-like state, so these tests drive them with a stand-in store
and lifecycle; the real app and a fake model server are exercised in ``tests/integration/test_be35_description_backfill.py``.
"""

from __future__ import annotations

import asyncio
import logging
import re
from types import SimpleNamespace
from typing import Any, Self

import httpx
import pytest

from archon_search.collection_meta import (
    DESCRIPTION_SOURCE_GENERATED,
    DESCRIPTION_SOURCE_MANUAL,
    DESCRIPTION_STATUS_FAILED,
    DESCRIPTION_STATUS_OK,
    CollectionMeta,
)
from archon_search.config import DescriptionConfig
from archon_search.constants import ASSUMED_CONTEXT_TOKENS, DEFAULT_NAMESPACE
from archon_search.description_lifecycle import (
    GenerationKind,
    GenerationResult,
    GenerationSettlement,
    GenerationTicket,
    RequestOutcome,
)
from archon_search.llm_client import once_per_process
from archon_search.llm_client.llama_cpp_client import LlamaCppClient
from archon_search.llm_client.server_address import UNKNOWN_HOST
from archon_search.server import description_backfill
from archon_search.server.description_backfill import (
    DESCRIPTION_UNREACHABLE_WARN_KEY,
    DescriptionReachability,
    _probe_url,
    backfill_candidates,
    probe_description_reachability,
    probed_server_label,
    run_description_backfill,
    run_description_probe,
)
from archon_search.server.schemas import StartupSyncResult
from tests._fake_model_server import (
    FakeModelServer,
    keep_loopback_out_of_proxies,
    unreachable_port,
)

_LET_TASKS_RUN_SECONDS = 0.05
_ONE_TICK_SECONDS = 0.001
_GENERATION_SECONDS = 0.01
_PROBE_TIMEOUT_SECONDS = 2.0
_SHORT_PROBE_TIMEOUT_SECONDS = 0.5
_HANG_GUARD_SECONDS = 5.0  # far past any bound under test: a call that has lost its own deadline fails here instead of hanging the run


@pytest.fixture(autouse=True)
def _no_proxy_for_loopback(monkeypatch: pytest.MonkeyPatch) -> None:
    keep_loopback_out_of_proxies(monkeypatch)


def _meta(
    name: str,
    *,
    namespace: str = DEFAULT_NAMESPACE,
    description: str | None = None,
    source: str | None = None,
    status: str | None = None,
    doc_count: int = 2,
) -> CollectionMeta:
    return CollectionMeta(
        name=name,
        namespace=namespace,
        description=description,
        description_source=source,  # type: ignore[arg-type]
        description_status=status,  # type: ignore[arg-type]
        doc_count=doc_count,
    )


def test_backfill_selects_only_empty_unfailed_non_manual_collections() -> None:
    metas = [
        _meta("described", description="Notes.", source=DESCRIPTION_SOURCE_GENERATED, status=DESCRIPTION_STATUS_OK),
        _meta("manual", description="Mine.", source=DESCRIPTION_SOURCE_MANUAL, status=DESCRIPTION_STATUS_OK),
        _meta("failed", status=DESCRIPTION_STATUS_FAILED),
        _meta("failed-with-old-text", description="Old.", source=DESCRIPTION_SOURCE_GENERATED, status=DESCRIPTION_STATUS_FAILED),
        _meta("no-documents", doc_count=0),
        _meta("b-empty", namespace="team-b"),
        _meta("a-empty", namespace="team-a"),
        _meta("z-empty"),
        _meta("m-empty"),
    ]

    picked = backfill_candidates(metas)

    assert [(meta.namespace, meta.name) for meta in picked] == [
        (DEFAULT_NAMESPACE, "m-empty"),
        (DEFAULT_NAMESPACE, "z-empty"),
        ("team-a", "a-empty"),
        ("team-b", "b-empty"),
    ]


def test_blank_description_counts_as_empty() -> None:
    assert [meta.name for meta in backfill_candidates([_meta("blank", description="")])] == ["blank"]


# ------------------------------------------------------------------------------ the probe


def _description(**fields: Any) -> DescriptionConfig:
    return DescriptionConfig(enabled=True, **fields)


async def test_claude_cli_reachability_is_presence_check_without_model_call(monkeypatch: pytest.MonkeyPatch) -> None:
    lookups: list[str] = []

    def found() -> str:
        lookups.append("claude")
        return "/usr/local/bin/claude"

    monkeypatch.setattr(description_backfill, "resolve_claude_binary", found)

    def refuse_any_call(*args: object, **kwargs: object) -> None:
        raise AssertionError("the claude_cli probe must not run the program, open a connection or call a model")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", refuse_any_call)
    monkeypatch.setattr(description_backfill.httpx, "AsyncClient", refuse_any_call)

    present = await probe_description_reachability(_description(provider="claude_cli"))
    assert present == DescriptionReachability(reachable=True, reason=None)
    assert lookups == ["claude"]

    monkeypatch.setattr(description_backfill, "resolve_claude_binary", lambda: None)
    missing = await probe_description_reachability(_description(provider="claude_cli"))
    assert missing.reachable is False
    assert missing.reason is not None and "claude" in missing.reason


async def test_provider_other_than_claude_cli_never_looks_for_the_claude_program(monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse_lookup() -> str | None:
        raise AssertionError("only a description configured with claude_cli looks for the claude program")

    monkeypatch.setattr(description_backfill, "resolve_claude_binary", refuse_lookup)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")

    assert (await probe_description_reachability(_description(provider="anthropic"))).reachable is True
    assert (await probe_description_reachability(_description(provider="openai"))).reachable is True
    with FakeModelServer() as server:
        ollama = await probe_description_reachability(
            _description(provider="ollama", ollama_base_url=server.base_url), timeout_seconds=_PROBE_TIMEOUT_SECONDS
        )
    assert ollama.reachable is True


@pytest.mark.parametrize(("provider", "variable"), [("anthropic", "ANTHROPIC_API_KEY"), ("openai", "OPENAI_API_KEY")])
async def test_cloud_provider_is_reachable_when_its_key_is_set_and_calls_nothing(
    provider: str, variable: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(variable, raising=False)
    missing = await probe_description_reachability(_description(provider=provider))
    assert missing.reachable is False
    assert missing.reason is not None and variable in missing.reason

    monkeypatch.setenv(variable, "sk-test")
    assert await probe_description_reachability(_description(provider=provider)) == DescriptionReachability(True, None)


async def test_ollama_probe_reads_the_model_list_and_sends_no_prompt() -> None:
    with FakeModelServer() as server:
        result = await probe_description_reachability(
            _description(provider="ollama", ollama_base_url=server.base_url), timeout_seconds=_PROBE_TIMEOUT_SECONDS
        )
        assert result == DescriptionReachability(True, None)
        assert [(request.method, request.path) for request in server.requests] == [("GET", "/api/tags")]


async def test_llama_cpp_probe_reads_props() -> None:
    with FakeModelServer() as server:
        result = await probe_description_reachability(
            _description(provider="llama_cpp", llama_cpp_base_url=server.base_url), timeout_seconds=_PROBE_TIMEOUT_SECONDS
        )
        assert result.reachable is True
        assert [request.path for request in server.requests] == ["/props"]


async def test_error_status_and_dead_port_read_unreachable_without_raising() -> None:
    with FakeModelServer(status_overrides={"/api/tags": 503}) as server:
        failing = await probe_description_reachability(
            _description(provider="ollama", ollama_base_url=server.base_url), timeout_seconds=_PROBE_TIMEOUT_SECONDS
        )
    assert failing.reachable is False
    assert failing.reason is not None and "503" in failing.reason

    with unreachable_port() as port:
        dead = await probe_description_reachability(
            _description(provider="ollama", ollama_base_url=f"http://127.0.0.1:{port}"),
            timeout_seconds=_SHORT_PROBE_TIMEOUT_SECONDS,
        )
    assert dead.reachable is False
    assert dead.reason is not None and f"127.0.0.1:{port}" in dead.reason


async def test_llama_cpp_probe_reasons_do_not_repeat_the_host_the_warning_names() -> None:
    """The one llama_cpp WARNING already reads ``the server at <host:port> is not reachable (<reason>)``: its reason is host-free."""
    with FakeModelServer(status_overrides={"/props": 503}) as server:
        failing = await probe_description_reachability(
            _description(provider="llama_cpp", llama_cpp_base_url=server.base_url), timeout_seconds=_PROBE_TIMEOUT_SECONDS
        )
    assert failing == DescriptionReachability(False, "answered HTTP 503")

    with unreachable_port() as port:
        dead = await probe_description_reachability(
            _description(provider="llama_cpp", llama_cpp_base_url=f"http://127.0.0.1:{port}"),
            timeout_seconds=_SHORT_PROBE_TIMEOUT_SECONDS,
        )
    assert dead.reachable is False
    assert dead.reason is not None and re.fullmatch(r"no answer: \w+", dead.reason)  # host-free, no parenthesis inside the WARNING's


async def test_probe_reason_names_host_and_port_only_never_credentials_or_path() -> None:
    result = await probe_description_reachability(
        _description(provider="ollama", ollama_base_url="http://user:secret@127.0.0.1:1/private/path"),
        timeout_seconds=_SHORT_PROBE_TIMEOUT_SECONDS,
    )
    assert result.reachable is False
    assert result.reason is not None
    assert "secret" not in result.reason and "private" not in result.reason and "user" not in result.reason


async def test_scheme_less_host_and_port_is_probed_like_the_sdk_reads_it() -> None:
    with FakeModelServer() as server:
        scheme_less = server.base_url.removeprefix("http://")
        result = await probe_description_reachability(
            _description(provider="ollama", ollama_base_url=scheme_less), timeout_seconds=_PROBE_TIMEOUT_SECONDS
        )
        assert result.reachable is True
        assert [request.path for request in server.requests] == ["/api/tags"]


async def test_ipv6_host_keeps_its_brackets_in_the_reason() -> None:
    result = await probe_description_reachability(
        _description(provider="ollama", ollama_base_url="http://[::1]:1"), timeout_seconds=_SHORT_PROBE_TIMEOUT_SECONDS
    )
    assert result.reachable is False
    assert result.reason is not None and "[::1]:1" in result.reason


@pytest.mark.parametrize(
    ("configured", "label"),
    [
        ("myollama.internal", "myollama.internal:11434"),
        ("user:secret@[::1]", "[::1]:11434"),
        ("myollama.internal:9", "myollama.internal:9"),
        ("http://myollama.internal", "myollama.internal:80"),
    ],
)
async def test_an_ollama_reason_labels_the_url_actually_probed(
    configured: str, label: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    class _Refusing:
        def __init__(self, **kwargs: object) -> None:
            pass

        async def __aenter__(self) -> Self:
            return self

        async def __aexit__(self, *exc_info: object) -> None:
            return None

        async def get(self, url: str) -> None:
            raise httpx.ConnectError("refused")

    monkeypatch.setattr(description_backfill.httpx, "AsyncClient", _Refusing)

    result = await probe_description_reachability(
        _description(provider="ollama", ollama_base_url=configured), timeout_seconds=_SHORT_PROBE_TIMEOUT_SECONDS
    )

    assert result.reason == f"no answer from {label}: ConnectError"


async def test_a_request_that_never_completes_is_cut_by_the_overall_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    class _Hanging:
        def __init__(self, **kwargs: object) -> None:
            pass

        async def __aenter__(self) -> Self:
            return self

        async def __aexit__(self, *exc_info: object) -> None:
            return None

        async def get(self, url: str) -> None:
            await asyncio.Event().wait()  # a server that trickles bytes forever: no httpx phase timeout fires

    monkeypatch.setattr(description_backfill.httpx, "AsyncClient", _Hanging)

    # The outer bound turns a probe that lost its own deadline into a failure of this test instead of a hang of the run.
    result = await asyncio.wait_for(
        probe_description_reachability(
            _description(provider="ollama", ollama_base_url="http://127.0.0.1:1"), timeout_seconds=_SHORT_PROBE_TIMEOUT_SECONDS
        ),
        _HANG_GUARD_SECONDS,
    )

    assert result.reachable is False
    assert result.reason is not None and "TimeoutError" in result.reason


@pytest.mark.parametrize("provider", ["ollama", "llama_cpp"])
@pytest.mark.parametrize(
    "configured",
    [
        "alice:12345/rest@server",  # no scheme: the authority ends at the ``/``, so ``alice:12345`` may be user and password
        "alice:12345/rest@server.test:80",
        "alice:s3cr3t@host.test/x@y",
        "alice:12345?k=v@server.test",
        "alice:12345#f@server.test",
        "http://alice:12345/rest@server.test",
        "[::1]alice@server.test",
        "user:12345@/@x",
        "http://[::1",
        "http://host.test:notaport",
    ],
)
def test_a_label_comes_from_the_raw_value_so_a_value_that_does_not_parse_has_no_name(provider: str, configured: str) -> None:
    """The ollama probe rebuilds the URL it asks from the netloc, which reads ``alice:12345`` out of the first value: a label
    taken from that URL would name the user and a password prefix. The label is the raw value's, or ``UNKNOWN_HOST``."""
    assert probed_server_label(provider, configured) == UNKNOWN_HOST


@pytest.mark.parametrize(
    ("provider", "configured", "label"),
    [
        ("ollama", "myollama.internal", "myollama.internal:11434"),
        ("ollama", "user:secret@[::1]", "[::1]:11434"),
        ("ollama", "myollama.internal:9/v1//x", "myollama.internal:9"),
        ("ollama", "http://myollama.internal", "myollama.internal:80"),
        ("ollama", "  http://Host.Test:5/  ", "host.test:5"),
        ("llama_cpp", "http://user:secret@LLAMA.test:8080/v1?x=1", "llama.test:8080"),
        ("llama_cpp", "localhost:8080", "localhost:8080"),
        ("llama_cpp", "localhost", "localhost"),
    ],
)
def test_the_label_of_a_value_that_parses_is_its_host_and_port_as_the_probe_asks_it(provider: str, configured: str, label: str) -> None:
    assert probed_server_label(provider, configured) == label


async def test_the_shared_warning_has_one_parenthesis_for_a_reason_that_has_none_of_its_own(caplog: pytest.LogCaptureFixture) -> None:
    """``is not reachable (<reason>)`` with ``<reason>`` = ``no answer (ConnectError)`` read as a nested parenthesis."""
    with unreachable_port() as port:
        state = SimpleNamespace(description_reachability=None)
        with caplog.at_level(logging.WARNING):
            await run_description_probe(
                state, _description(provider="ollama", ollama_base_url=f"http://127.0.0.1:{port}")
            )
            await run_description_probe(
                state, _description(provider="llama_cpp", llama_cpp_base_url=f"http://127.0.0.1:{port}")
            )

    ollama, llama = [record.getMessage() for record in caplog.records if record.levelno == logging.WARNING]
    assert re.search(rf"provider ollama is not reachable \(no answer from 127\.0\.0\.1:{port}: \w+\); generations will fail", ollama)
    assert re.search(rf"the server at 127\.0\.0\.1:{port} is not reachable \(no answer: \w+\); requests to it fail", llama)


async def test_a_probe_of_a_value_that_does_not_parse_never_names_a_piece_of_it(caplog: pytest.LogCaptureFixture) -> None:
    """The real connect failure goes to ``127.0.0.1`` (what the netloc names), whatever the path spells out."""
    with unreachable_port() as port:
        configured = f"127.0.0.1:{port}/svc-u5er:s3cr3t@gpu.test"
        state = SimpleNamespace(description_reachability=None)
        with caplog.at_level(logging.DEBUG):
            await run_description_probe(state, _description(provider="ollama", ollama_base_url=configured))

    assert re.fullmatch(rf"no answer from {UNKNOWN_HOST}: \w+", state.description_reachability.reason)
    logged = caplog.text + "".join(logging.Formatter().formatException(r.exc_info) for r in caplog.records if r.exc_info)
    for secret in ("svc-u5er", "s3cr3t", "gpu.test"):
        assert secret not in logged


@pytest.mark.parametrize(
    ("provider", "configured", "asked"),
    [
        ("ollama", "http://h:1/", "http://h:1/api/tags"),
        ("ollama", "localhost:9", "http://localhost:9/api/tags"),
        ("ollama", "myollama.internal", "http://myollama.internal:11434/api/tags"),
        ("ollama", "user:secret@[::1]", "http://user:secret@[::1]:11434/api/tags"),
        ("ollama", "localhost:11434/a//b", "http://localhost:11434/api/tags"),  # a ``//`` in the path is not a URL prefix
        ("ollama", "  http://h:1/  ", "http://h:1/api/tags"),
        ("llama_cpp", "localhost:8080", "localhost:8080/props"),  # the llama.cpp client does not add a scheme
        ("llama_cpp", "http://h:2", "http://h:2/props"),
        # the probe sets the path: nothing of a query or fragment survives, and the endpoint is never appended after one
        ("ollama", "http://h:1/?k=v", "http://h:1/api/tags"),
        ("ollama", "http://h:1?k=v", "http://h:1/api/tags"),
        ("ollama", "http://h:1/#frag", "http://h:1/api/tags"),
        ("ollama", "http://h:1/a/b/?k=v#f", "http://h:1/a/b/api/tags"),
        ("ollama", "h:1/?k=v", "http://h:1/api/tags"),
        ("ollama", "h?k=v", "http://h:11434/api/tags"),
        ("llama_cpp", "http://h:2/?k=v", "http://h:2/props"),
        ("llama_cpp", "http://h:2?k=v#f", "http://h:2/props"),
        ("llama_cpp", "http://gw:2/llama-a/?k=v", "http://gw:2/llama-a/props"),
        ("llama_cpp", "h:2/?k=v", "h:2/props"),
    ],
)
def test_probe_asks_the_url_the_providers_own_client_would_use(provider: str, configured: str, asked: str) -> None:
    assert _probe_url(provider, configured) == asked


@pytest.mark.parametrize(
    ("provider", "field", "path"), [("ollama", "ollama_base_url", "/api/tags"), ("llama_cpp", "llama_cpp_base_url", "/props")]
)
async def test_a_base_url_with_a_query_is_still_asked_its_own_endpoint(
    monkeypatch: pytest.MonkeyPatch, provider: str, field: str, path: str
) -> None:
    """``http://h:1/?k=v`` used to be asked as ``/?k=v/api/tags``: the server saw ``/``, so any server that answers ``/`` read
    as reachable."""
    asked: list[httpx.URL] = []

    def answer(request: httpx.Request) -> httpx.Response:
        asked.append(request.url)
        return httpx.Response(200 if request.url.path == path else 404)

    real_client = httpx.AsyncClient
    monkeypatch.setattr(
        description_backfill.httpx, "AsyncClient", lambda **kwargs: real_client(transport=httpx.MockTransport(answer), **kwargs)
    )

    result = await probe_description_reachability(_description(provider=provider, **{field: "http://h.test:1/?k=v#f"}))

    assert result.reachable is True, result.reason
    assert [(url.path, url.query) for url in asked] == [(path, b"")]


async def test_scheme_less_llama_cpp_url_reads_unreachable_like_its_generations() -> None:
    result = await probe_description_reachability(
        _description(provider="llama_cpp", llama_cpp_base_url="localhost:8080"), timeout_seconds=_SHORT_PROBE_TIMEOUT_SECONDS
    )
    assert result.reachable is False


async def test_unparsable_url_reads_unreachable_without_raising() -> None:
    result = await probe_description_reachability(
        _description(provider="ollama", ollama_base_url="http://[::1"), timeout_seconds=_SHORT_PROBE_TIMEOUT_SECONDS
    )
    assert result.reachable is False
    assert result.reason is not None and "the configured host" in result.reason


async def test_probe_task_publishes_the_result_and_warns_once_when_unreachable(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    state = SimpleNamespace(description_reachability=None)
    config = _description(provider="anthropic")

    with caplog.at_level(logging.INFO, logger=description_backfill.__name__):
        await run_description_probe(state, config)
        await run_description_probe(state, config)

    assert state.description_reachability.reachable is False
    warnings = [record for record in caplog.records if record.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert "anthropic" in warnings[0].getMessage() and "ANTHROPIC_API_KEY" in warnings[0].getMessage()
    assert DESCRIPTION_UNREACHABLE_WARN_KEY in once_per_process._warned_keys


@pytest.mark.parametrize("window_lookup_first", [True, False], ids=["window-first", "probe-first"])
async def test_a_down_llama_server_found_by_the_probe_and_the_window_lookup_is_one_warning(
    window_lookup_first: bool, caplog: pytest.LogCaptureFixture
) -> None:
    """S8 + S34: both paths ask the same server the same question; whichever reports first, the operator reads one WARNING that
    says the server is not reachable and the window is assumed."""
    with unreachable_port() as port:
        server = f"127.0.0.1:{port}"
        url = f"http://{server}"
        client = LlamaCppClient(model="m", base_url=url)
        state = SimpleNamespace(description_reachability=None)
        config = _description(provider="llama_cpp", llama_cpp_base_url=url)

        with caplog.at_level(logging.WARNING):
            if window_lookup_first:
                await client.probe_context_window(_SHORT_PROBE_TIMEOUT_SECONDS)
                await run_description_probe(state, config)
            else:
                await run_description_probe(state, config)
                await client.probe_context_window(_SHORT_PROBE_TIMEOUT_SECONDS)

    assert state.description_reachability.reachable is False
    [record] = [record for record in caplog.records if record.levelno == logging.WARNING]
    warning = record.getMessage()
    assert warning.startswith(f"llama_cpp: the server at {server} is not reachable (")
    assert warning.endswith(f"requests to it fail until it answers, and where its context window could not be read, {ASSUMED_CONTEXT_TOKENS} tokens are assumed")
    assert warning.count(server) == 1  # the reason does not repeat the host
    assert record.name == "archon_search.llm_client.llama_cpp_client"  # the shared line, whichever path logged it
    assert DESCRIPTION_UNREACHABLE_WARN_KEY not in once_per_process._warned_keys


async def test_a_down_llama_server_spelled_differently_by_the_probe_is_still_one_warning(caplog: pytest.LogCaptureFixture) -> None:
    with unreachable_port() as port:
        client = LlamaCppClient(model="m", base_url=f"http://127.0.0.1:{port}")
        state = SimpleNamespace(description_reachability=None)
        config = _description(provider="llama_cpp", llama_cpp_base_url=f"HTTP://user:secret@127.0.0.1:{port}/v1/?x=1")

        with caplog.at_level(logging.WARNING):
            await client.probe_context_window(_SHORT_PROBE_TIMEOUT_SECONDS)
            await run_description_probe(state, config)

    assert len([record for record in caplog.records if record.levelno == logging.WARNING]) == 1


async def test_probe_of_a_down_llama_server_names_host_and_port_never_credentials(caplog: pytest.LogCaptureFixture) -> None:
    state = SimpleNamespace(description_reachability=None)
    config = _description(provider="llama_cpp", llama_cpp_base_url="http://user:secret@127.0.0.1:1/private/path?token=abc")

    with caplog.at_level(logging.WARNING):
        await run_description_probe(state, config)

    [warning] = [record.getMessage() for record in caplog.records if record.levelno == logging.WARNING]
    assert "127.0.0.1:1" in warning
    for hidden in ("secret", "private", "abc", "user"):
        assert hidden not in warning


async def test_probe_task_never_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    async def explode(*args: object, **kwargs: object) -> DescriptionReachability:
        raise RuntimeError("probe bug")

    monkeypatch.setattr(description_backfill, "probe_description_reachability", explode)
    state = SimpleNamespace(description_reachability=None)

    await run_description_probe(state, _description(provider="anthropic"))

    assert state.description_reachability is not None and state.description_reachability.reachable is False


@pytest.mark.parametrize("provider", ["llama_cpp", "ollama"])
async def test_an_unexpected_probe_failure_is_the_generic_warning_never_a_per_server_one(
    provider: str, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    async def explode(*args: object, **kwargs: object) -> DescriptionReachability:
        raise RuntimeError("probe bug")

    monkeypatch.setattr(description_backfill, "probe_description_reachability", explode)
    state = SimpleNamespace(description_reachability=None)

    with caplog.at_level(logging.WARNING):
        await run_description_probe(state, _description(provider=provider))

    [record] = [record for record in caplog.records if record.levelno == logging.WARNING]
    assert record.name == description_backfill.__name__
    assert "the probe failed unexpectedly (RuntimeError)" in record.getMessage()
    assert DESCRIPTION_UNREACHABLE_WARN_KEY in once_per_process._warned_keys
    assert not [key for key in once_per_process._warned_keys if key.startswith("llama_cpp_unreachable:")]


# ------------------------------------------------------------------------------ the loop


class _Store:
    def __init__(self, metas: list[CollectionMeta], failure: Exception | None = None) -> None:
        self._metas = metas
        self._failure = failure
        self.listed = 0

    async def get_all_collections_meta(self) -> list[CollectionMeta]:
        self.listed += 1
        if self._failure is not None:
            raise self._failure
        return list(self._metas)


class _Lifecycle:
    """Records the requests; ``outcomes`` maps a collection name to the outcome it answers (default: accepted)."""

    def __init__(self, outcomes: dict[str, RequestOutcome] | None = None) -> None:
        self.requests: list[tuple[str, str, GenerationKind]] = []
        self.in_flight = 0
        self.max_in_flight = 0
        self._outcomes = outcomes or {}

    async def request_generation(self, collection: str, namespace: str, kind: GenerationKind) -> GenerationTicket:
        self.requests.append((collection, namespace, kind))
        loop = asyncio.get_running_loop()
        settled: asyncio.Future[GenerationSettlement] = loop.create_future()
        outcome = self._outcomes.get(collection, RequestOutcome.ACCEPTED)
        if outcome is RequestOutcome.ACCEPTED:
            self.in_flight += 1
            self.max_in_flight = max(self.max_in_flight, self.in_flight)
            loop.call_later(_GENERATION_SECONDS, self._settle, settled)
        else:
            settled.set_result(GenerationSettlement(GenerationResult.NOT_REQUIRED))
        return GenerationTicket(outcome, settled)

    def _settle(self, settled: asyncio.Future[GenerationSettlement]) -> None:
        self.in_flight -= 1
        if not settled.done():  # the loop's task may have been cancelled, which cancels the ticket's future
            settled.set_result(GenerationSettlement(GenerationResult.GENERATED))


def _state(*, sync_task: asyncio.Task[None] | None = None, sync_result: StartupSyncResult | None = None) -> SimpleNamespace:
    return SimpleNamespace(
        _startup_sync_task=sync_task,
        sync_result=sync_result,
        _description_probe_task=None,
        _llm_window_task=None,
        description_reachability=DescriptionReachability(True, None),
    )


async def test_loop_requests_candidates_one_at_a_time_in_order_with_the_backfill_kind() -> None:
    store = _Store([_meta("b"), _meta("a", namespace="team-a"), _meta("done", description="x", source="generated", status="ok")])
    lifecycle = _Lifecycle()

    await run_description_backfill(_state(), store, lifecycle)

    assert lifecycle.requests == [
        ("b", DEFAULT_NAMESPACE, GenerationKind.BACKFILL),
        ("a", "team-a", GenerationKind.BACKFILL),
    ]
    assert lifecycle.max_in_flight == 1


async def test_loop_waits_for_the_startup_sync_in_any_end_state() -> None:
    for end in (StartupSyncResult.DONE, StartupSyncResult.FAILED):
        release = asyncio.Event()
        state = _state(sync_result=StartupSyncResult.PENDING)

        async def sync(end: StartupSyncResult = end, release: asyncio.Event = release, state: SimpleNamespace = state) -> None:
            await release.wait()
            state.sync_result = end

        state._startup_sync_task = asyncio.create_task(sync())
        store = _Store([_meta("a")])
        lifecycle = _Lifecycle()

        backfill = asyncio.create_task(run_description_backfill(state, store, lifecycle))
        await asyncio.sleep(_LET_TASKS_RUN_SECONDS)
        assert store.listed == 0 and lifecycle.requests == []
        release.set()
        await backfill

        assert [request[0] for request in lifecycle.requests] == ["a"]


async def test_cancelling_the_loop_while_it_waits_for_the_sync_ends_it_before_any_request() -> None:
    state = _state(sync_result=StartupSyncResult.PENDING)
    state._startup_sync_task = asyncio.create_task(asyncio.Event().wait())
    store = _Store([_meta("a")])
    lifecycle = _Lifecycle()
    backfill = asyncio.create_task(run_description_backfill(state, store, lifecycle))
    await asyncio.sleep(_LET_TASKS_RUN_SECONDS)

    backfill.cancel()
    with pytest.raises(asyncio.CancelledError):
        await backfill
    state._startup_sync_task.cancel()

    assert store.listed == 0 and lifecycle.requests == []


async def test_loop_waits_for_a_crashed_sync_too() -> None:
    async def crash() -> None:
        raise RuntimeError("sync died")

    sync_task = asyncio.create_task(crash())
    state = _state(sync_task=sync_task, sync_result=StartupSyncResult.FAILED)
    lifecycle = _Lifecycle()

    await run_description_backfill(state, _Store([_meta("a")]), lifecycle)

    assert [request[0] for request in lifecycle.requests] == ["a"]
    with pytest.raises(RuntimeError):
        await sync_task  # the loop retrieved nothing from the task: the sync's own failure stays its own


async def test_loop_waits_for_the_probe_and_is_skipped_with_one_info_when_it_says_unreachable(
    caplog: pytest.LogCaptureFixture,
) -> None:
    release = asyncio.Event()
    state = _state()
    state.description_reachability = None

    async def probe() -> None:
        await release.wait()
        state.description_reachability = DescriptionReachability(False, "ANTHROPIC_API_KEY is not set")

    state._description_probe_task = asyncio.create_task(probe())
    store = _Store([_meta("a")])
    lifecycle = _Lifecycle()

    with caplog.at_level(logging.INFO, logger=description_backfill.__name__):
        backfill = asyncio.create_task(run_description_backfill(state, store, lifecycle))
        await asyncio.sleep(_LET_TASKS_RUN_SECONDS)
        assert not backfill.done()
        release.set()
        await backfill

    assert store.listed == 0 and lifecycle.requests == []
    infos = [record for record in caplog.records if record.levelno == logging.INFO]
    assert len(infos) == 1
    assert "ANTHROPIC_API_KEY is not set" in infos[0].getMessage()
    assert not [record for record in caplog.records if record.levelno >= logging.WARNING]


async def test_loop_waits_for_the_context_window_resolution_before_listing() -> None:
    release = asyncio.Event()
    state = _state()
    state._llm_window_task = asyncio.create_task(release.wait())
    store = _Store([_meta("a")])
    lifecycle = _Lifecycle()

    backfill = asyncio.create_task(run_description_backfill(state, store, lifecycle))
    await asyncio.sleep(_LET_TASKS_RUN_SECONDS)
    assert store.listed == 0 and lifecycle.requests == []
    release.set()
    await backfill

    assert [request[0] for request in lifecycle.requests] == ["a"]


async def test_loop_never_raises_when_the_lifecycle_does() -> None:
    class _Broken(_Lifecycle):
        async def request_generation(self, collection: str, namespace: str, kind: GenerationKind) -> GenerationTicket:
            raise RuntimeError("lifecycle bug")

    await run_description_backfill(_state(), _Store([_meta("a")]), _Broken())



async def test_loop_is_skipped_with_one_info_when_the_startup_sync_is_suppressed(caplog: pytest.LogCaptureFixture) -> None:
    state = _state(sync_result=StartupSyncResult.SUPPRESSED)
    store = _Store([_meta("a")])
    lifecycle = _Lifecycle()

    with caplog.at_level(logging.INFO, logger=description_backfill.__name__):
        await run_description_backfill(state, store, lifecycle)

    assert store.listed == 0 and lifecycle.requests == []
    infos = [record for record in caplog.records if record.levelno == logging.INFO]
    assert len(infos) == 1
    assert "suppressed" in infos[0].getMessage().lower()
    assert "POST /sync" in infos[0].getMessage()


async def test_loop_moves_past_a_collection_already_running_and_stops_when_closed() -> None:
    store = _Store([_meta("a"), _meta("b"), _meta("c"), _meta("d")])
    lifecycle = _Lifecycle({"a": RequestOutcome.ALREADY_RUNNING, "b": RequestOutcome.NOT_REQUIRED, "c": RequestOutcome.CLOSED})

    await run_description_backfill(_state(), store, lifecycle)

    assert [request[0] for request in lifecycle.requests] == ["a", "b", "c"]  # "d" is never asked once the service is closed


async def test_loop_survives_an_unreadable_store_with_a_warning(caplog: pytest.LogCaptureFixture) -> None:
    store = _Store([], failure=RuntimeError("table unreadable"))
    lifecycle = _Lifecycle()

    with caplog.at_level(logging.INFO, logger=description_backfill.__name__):
        await run_description_backfill(_state(), store, lifecycle)

    assert lifecycle.requests == []
    assert [record for record in caplog.records if record.levelno == logging.WARNING]


async def test_loop_stops_at_the_collection_whose_generation_ends_in_shutdown() -> None:
    class _ShuttingDown(_Lifecycle):
        def _settle(self, settled: asyncio.Future[GenerationSettlement]) -> None:
            self.in_flight -= 1
            settled.set_result(GenerationSettlement(GenerationResult.SHUTTING_DOWN))

    lifecycle = _ShuttingDown()

    await run_description_backfill(_state(), _Store([_meta("a"), _meta("b")]), lifecycle)

    assert [request[0] for request in lifecycle.requests] == ["a"]


async def test_cancelling_the_loop_mid_generation_ends_it_cleanly() -> None:
    lifecycle = _Lifecycle()
    task = asyncio.create_task(run_description_backfill(_state(), _Store([_meta("a"), _meta("b")]), lifecycle))
    await asyncio.sleep(_ONE_TICK_SECONDS)

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert [request[0] for request in lifecycle.requests] == ["a"]
