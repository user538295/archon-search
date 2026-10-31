"""T-4 (collection-descriptions plan) — graph community build on ``claude_cli`` and with the provider down (S4, S64).

Both tests run the real app (lifespan, real LanceDB, real ingest, real graph store, real Leiden) and trigger the build
the way an operator does, through ``POST /graph/{collection}/rebuild-communities``. Only the graph extraction engine
(gliner) is stubbed, deterministically, so every document yields one entity of its own and the three documents become
three communities. ``claude`` is the real-subprocess fake of ``tests/_fake_claude.py``.

* S64: a community whose passages are far larger than any argv holds still gets its summary, because the prompt goes to
  the ``claude`` program on standard input; ``extraction_rate_limit_rpm`` throttles the build.
* S4: with the provider down the build completes, the communities keep no summary text, and the failure is one WARNING.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from archon_search.graph_store import GraphStore
from archon_search.graph_types import Community
from archon_search.llm_client.rate_limit import FixedWindowRateLimiter
from archon_search.server import llm_composition
from tests import _fake_claude
from tests._fake_model_server import keep_loopback_out_of_proxies, unreachable_port
from tests._graph_engine_stub import install_graph_engine_stub
from tests.integration.conftest import ingest_doc, make_real_app

pytestmark = pytest.mark.integration

_COLLECTION = "graph-e2e"
_NAMESPACE = "default"
_GRAPH_MODEL = "graph-model"
_SUMMARY = "the community summary"
#: One entity per document; every paragraph of a document names its entity, so every chunk of it extracts that entity.
_TOPICS = ("Aardvark", "Barnacle", "Cormorant")
#: Text that must never reach a log line: it sits in every chunk of every document.
_PASSAGE_MARKER = "secretpassage-8841"
_PARAGRAPH_CHARS = 1000
#: Per document; a community holds one document's chunks, and the prompt carries all of them.
_DOC_CHARS = 330_000
#: Far above the 128 KiB a Linux argument may hold and a fraction of the macOS total.
_LARGE_PROMPT_BYTES = 300_000
#: Bounds the cost of the build: the MMR pass over a community's chunks is cubic in their number, and ~4 K-token chunks keep it small
#: while the prompt stays above `_LARGE_PROMPT_BYTES`.
_CHUNK_TOKENS = 4096
_SUMMARY_CHUNKS = 10_000
_RATE_LIMIT_RPM = 2
_WARNINGS_PER_PROCESS = 1
_REBUILD_WAIT_SECONDS = 120.0
_INGEST_WAIT_SECONDS = 120.0
_POLL_SECONDS = 0.05
_BUILDER_LOGGER = "archon_search.community_builder"
_TERMINAL_STATES = {"DONE", "FAILED", "FAILED_EXPIRED", "CANCELLED"}
_CLAUDE_FAILURE_EXIT = 1


@pytest.fixture(autouse=True)
def _no_proxy_for_loopback(monkeypatch: pytest.MonkeyPatch) -> None:
    keep_loopback_out_of_proxies(monkeypatch)


def _graph_toml(provider: str, **extra: object) -> str:
    lines = [
        "[database]",
        f"chunk_size = {_CHUNK_TOKENS}",
        "[graph]",
        "enabled = true",
        f'provider = "{provider}"',
        f'extraction_model = "{_GRAPH_MODEL}"',
        f"community_summary_chunks = {_SUMMARY_CHUNKS}",
    ]
    lines += [f'{key} = "{value}"' if isinstance(value, str) else f"{key} = {value}" for key, value in extra.items()]
    return "\n".join(lines) + "\n"


def _document(topic: str) -> str:
    paragraph = f"{topic} {_PASSAGE_MARKER} " + "storage replication detail " * (_PARAGRAPH_CHARS // 27)
    return "\n\n".join([paragraph] * (_DOC_CHARS // len(paragraph)))


def _install_engine_stub(monkeypatch: pytest.MonkeyPatch) -> None:
    install_graph_engine_stub(monkeypatch, entities=lambda text: [(t, "concept") for t in _TOPICS if t in text])


def _freeze_rate_limit_window(monkeypatch: pytest.MonkeyPatch) -> None:
    """The graph limiter's 60 s window opens when the app is built; freeze its clock so a slow build cannot cross into a
    second window and the cap holds however long ingest and the build take."""
    monkeypatch.setattr(
        llm_composition, "FixedWindowRateLimiter", lambda capacity: FixedWindowRateLimiter(capacity, clock=lambda: 0.0)
    )


def _install_fake_claude(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, **behaviour: Any) -> _fake_claude.FakeClaude:
    fake = _fake_claude.install(tmp_path / "bin", **behaviour)
    monkeypatch.setenv("PATH", f"{fake.path.parent}{os.pathsep}{os.environ['PATH']}")
    return fake


def _auth(api_key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {api_key}"}


def _ingest_corpus(client: TestClient, api_key: str) -> None:
    for topic in _TOPICS:
        ingest_doc(
            client, _COLLECTION, _document(topic), f"/docs/{topic.lower()}.md", api_key=api_key, timeout_s=_INGEST_WAIT_SECONDS
        )


def _rebuild_communities(client: TestClient, api_key: str) -> dict[str, Any]:
    response = client.post(f"/graph/{_COLLECTION}/rebuild-communities", headers=_auth(api_key))
    assert response.status_code == 202, response.text
    job_id = response.json()["job_id"]
    deadline = time.monotonic() + _REBUILD_WAIT_SECONDS
    while time.monotonic() < deadline:
        job = client.get(f"/jobs/{job_id}", headers=_auth(api_key)).json()
        if job["status"] in _TERMINAL_STATES:
            return job
        time.sleep(_POLL_SECONDS)
    pytest.fail(f"community rebuild {job_id} did not finish in {_REBUILD_WAIT_SECONDS}s")


def _stored_communities(db_path: str) -> list[Community]:
    async def _read() -> list[Community]:
        store = GraphStore(db_path)
        await store.connect()
        try:
            return await store.list_community_representatives(_COLLECTION, _NAMESPACE)
        finally:
            await store.disconnect()

    return asyncio.run(_read())


@contextmanager
def _built_communities(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, toml: str
) -> Iterator[tuple[dict[str, Any], list[Community]]]:
    """Boot the app on ``toml``, ingest the corpus, rebuild over HTTP and yield the finished job and the stored communities."""
    _install_engine_stub(monkeypatch)
    _freeze_rate_limit_window(monkeypatch)
    with make_real_app(tmp_path, monkeypatch, toml_content=toml) as (client, config, api_key):
        _ingest_corpus(client, api_key)
        job = _rebuild_communities(client, api_key)
        yield job, _stored_communities(config.db_path)


def _builder_warnings(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING and r.name == _BUILDER_LOGGER]


def _assert_no_passage_in_logs(caplog: pytest.LogCaptureFixture) -> None:
    for record in caplog.records:
        rendered = f"{record.getMessage()} {record.args} {record.exc_text}"
        assert _PASSAGE_MARKER not in rendered, f"a passage reached the log: {record.name} {record.levelname}"


def test_e2e_graph_build_on_claude_cli_stdin_prompt_and_rpm_throttle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """S64: three communities of ~330 KB each on `claude_cli` with `extraction_rate_limit_rpm = 2`: each prompt reaches
    `claude` on standard input and never in its argument list, the third community costs no spawn and keeps no summary, and
    the build completes with one WARNING that carries no passage."""
    caplog.set_level(logging.DEBUG)
    fake = _install_fake_claude(tmp_path, monkeypatch, reply=_SUMMARY)
    toml = _graph_toml("claude_cli", extraction_rate_limit_rpm=_RATE_LIMIT_RPM)

    with _built_communities(tmp_path, monkeypatch, toml) as (job, communities):
        pass

    assert job["status"] == "DONE", job
    assert job["result"] == {"communities_built": len(_TOPICS)}
    assert len(communities) == len(_TOPICS)
    summaries = sorted(c.summary_text or "" for c in communities)
    assert summaries == ["", _SUMMARY, _SUMMARY], "two communities are summarised, the throttled one is not"
    invocations = fake.invocations()
    assert len(invocations) == _RATE_LIMIT_RPM, "the capped call must not spawn a third `claude`"
    assert len({invocation.stdin for invocation in invocations}) == _RATE_LIMIT_RPM, "each community has its own prompt"
    for invocation in invocations:
        assert len(invocation.stdin.encode()) > _LARGE_PROMPT_BYTES
        assert "knowledge-graph summariser" in invocation.stdin
        assert _PASSAGE_MARKER in invocation.stdin
        assert all(len(argument) < _PARAGRAPH_CHARS for argument in invocation.argv), "the prompt must not ride in argv"
        assert not any(_PASSAGE_MARKER in argument or "summariser" in argument for argument in invocation.argv)
    warnings = _builder_warnings(caplog)
    assert len(warnings) == _WARNINGS_PER_PROCESS
    assert "rate limit exhausted" in warnings[0] and f"({_RATE_LIMIT_RPM} rpm)" in warnings[0]
    _assert_no_passage_in_logs(caplog)


@pytest.mark.parametrize(
    ("provider", "reason"),
    [("llama_cpp", "cannot connect to the llama-server"), ("claude_cli", "exit status 1: service unavailable")],
)
def test_e2e_graph_provider_down_communities_without_summary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, provider: str, reason: str
) -> None:
    """S4: with the graph provider down (a refused connection for `llama_cpp`, a `claude` that exits 1 for `claude_cli`)
    the rebuild still ends DONE, every community is stored with no summary text, and the failure is logged once for the
    whole build — three communities fail, one WARNING — and carries no passage."""
    caplog.set_level(logging.DEBUG)
    if provider == "claude_cli":
        fake = _install_fake_claude(tmp_path, monkeypatch, exit_codes=(_CLAUDE_FAILURE_EXIT,), stderr="service unavailable")
        with _built_communities(tmp_path, monkeypatch, _graph_toml(provider)) as built:
            job, communities = built
        assert len(fake.invocations()) == len(_TOPICS), "every community still tried the provider"
    else:
        with unreachable_port() as port:
            toml = _graph_toml(provider, llama_cpp_base_url=f"http://127.0.0.1:{port}")
            with _built_communities(tmp_path, monkeypatch, toml) as built:
                job, communities = built

    assert job["status"] == "DONE", job
    assert job["result"] == {"communities_built": len(_TOPICS)}
    assert len(communities) == len(_TOPICS)
    assert all(c.summary_text is None for c in communities)
    assert all(c.representative_chunk_ids for c in communities), "the communities are otherwise complete"
    warnings = _builder_warnings(caplog)
    assert len(warnings) == 1 and "LLM summary failed" in warnings[0]
    assert reason in warnings[0] and "unexpected" not in warnings[0], "the provider's own failure, not a catch-all"
    _assert_no_passage_in_logs(caplog)
