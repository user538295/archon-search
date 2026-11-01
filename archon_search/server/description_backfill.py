"""The description feature's two startup tasks (C5; S33, S34, S86): the reachability probe and the backfill loop.

Both are hosted by the server lifespan (``server/app.py``) when ``[description]`` is enabled and never block boot or raise.

The probe runs once and publishes its verdict on ``app.state.description_reachability`` (``None`` until probed, never
refreshed). It sends no prompt and spends nothing: a cloud provider reads as reachable when its API key is set, an Ollama
or llama.cpp server when its model-list or ``/props`` endpoint answers, and ``claude_cli`` when the ``claude`` program is
found (and only a description configured with ``claude_cli`` looks for it).

The backfill loop waits for the startup sync (in whatever state it ended) and is skipped for the whole run with one INFO
when the sync is suppressed, without waiting for the probe; otherwise it waits for the probe (skipped with one INFO when
the provider is not reachable) and for the context-window resolution, and then asks the lifecycle for one collection at
a time, across every namespace, for each collection that has no description. The probe is independent of the backfill
and of the sync state (it runs whenever the feature is enabled), so an unreachable provider is one WARNING and
``/status`` reachability even when the backfill is off or skipped; S86's "no provider is called" means no generation
call, since the probe sends no prompt and no document text. A down llama-server is also found by the context-window
lookup of every ``llama_cpp`` feature (the same ``GET /props``) and by the startup reachability probe (``GET /v1/models``);
the probe reports it through the shared WARNING (``warn_llama_cpp_unreachable``, one per ``host:port`` label and process,
logged by ``archon_search.llm_client.llama_cpp_client``), so one outage is one WARNING for that server, not one per path.
Its reason is host-free: the WARNING already names the server.
"""

from __future__ import annotations

import asyncio
import dataclasses
import logging
from collections.abc import Iterable
from typing import TYPE_CHECKING, Final, Protocol
from urllib.parse import urlsplit

import httpx

from archon_search.collection_meta import (
    DESCRIPTION_SOURCE_MANUAL,
    DESCRIPTION_STATUS_FAILED,
    CollectionMeta,
)
from archon_search.config import DescriptionConfig
from archon_search.constants import LOCAL_SERVER_PROBE_TIMEOUT_SECONDS
from archon_search.description_lifecycle import (
    GenerationKind,
    GenerationResult,
    GenerationTicket,
    RequestOutcome,
)
from archon_search.llm_client.claude_cli_client import resolve_claude_binary
from archon_search.llm_client.llama_cpp_client import warn_llama_cpp_unreachable
from archon_search.llm_client.once_per_process import warn_once
from archon_search.llm_client.provider_key import provider_key_available
from archon_search.llm_client.server_address import (
    UNKNOWN_HOST,
    ServerAddress,
    is_url,
    parse_server_address,
)
from archon_search.server.provider_dependency import check_provider_package
from archon_search.server.schemas import StartupSyncResult

if TYPE_CHECKING:
    from starlette.datastructures import State

logger = logging.getLogger(__name__)

#: ``warn_once`` key of the unreachable-provider WARNING: the startup validator (BE-36) reuses it, so the finding is
#: logged once per process however many startup paths find it.
DESCRIPTION_UNREACHABLE_WARN_KEY: Final = "description_provider_unreachable"

_OLLAMA: Final = "ollama"
_LLAMA_CPP: Final = "llama_cpp"
_CLAUDE_CLI: Final = "claude_cli"
_API_KEY_VARIABLES: Final = {"anthropic": "ANTHROPIC_API_KEY", "openai": "OPENAI_API_KEY"}
#: The cheap read-only endpoint each local server is asked: Ollama's model list, llama-server's properties.
_PROBE_PATHS: Final = {_OLLAMA: "/api/tags", _LLAMA_CPP: "/props"}
_OLLAMA_DEFAULT_PORT: Final = 11434


@dataclasses.dataclass(frozen=True, slots=True)
class DescriptionReachability:
    """The startup probe's verdict. ``reason`` is operator-readable and free of credentials and paths; ``None`` exactly when reachable."""

    reachable: bool
    reason: str | None


class BackfillStore(Protocol):
    async def get_all_collections_meta(self) -> list[CollectionMeta]: ...


class BackfillLifecycle(Protocol):
    async def request_generation(self, collection: str, namespace: str, kind: GenerationKind) -> GenerationTicket: ...


def local_server_url(description: DescriptionConfig) -> str:
    """The configured server URL of a local (``ollama`` or ``llama_cpp``) description provider."""
    return description.ollama_base_url if description.provider == _OLLAMA else description.llama_cpp_base_url


@dataclasses.dataclass(frozen=True, slots=True)
class StaticProblem:
    """A reason the provider cannot be used. ``reason`` is operator-readable and free of credentials and paths (it reaches
    the log; ``/status`` shows only the probe's yes/no); ``remedy`` is the install command, which only the log line may carry."""

    reason: str
    remedy: str | None = None


def static_provider_problem(description: DescriptionConfig) -> StaticProblem | None:
    """Why the configured provider cannot be used, known without any network call or program lookup: its package is
    missing (C12) or its API key is not set. The package is checked first. ``None`` when neither applies."""
    problem = check_provider_package(description.provider)
    if problem is not None:
        return StaticProblem(problem.summary(), problem.install_hint)
    if not provider_key_available(description.provider):
        variable = _API_KEY_VARIABLES.get(description.provider, _API_KEY_VARIABLES["anthropic"])  # as provider_key_available
        return StaticProblem(f"{variable} is not set")
    return None


def warn_description_unreachable(provider: str, reason: str | None, remedy: str | None = None) -> None:
    """The one WARNING per process that the configured description provider cannot be used. The startup validator and the
    probe both report through it, so the finding is logged once however many startup paths find it. *remedy* (an install
    command) is appended to the reason in the log line only."""
    shown = f"{reason}; run: {remedy}" if remedy else reason
    warn_once(
        logger,
        DESCRIPTION_UNREACHABLE_WARN_KEY,
        "Collection descriptions: provider %s is not reachable (%s); generations will fail until that is fixed",
        provider,
        shown,
    )


async def probe_description_reachability(
    description: DescriptionConfig, *, timeout_seconds: float = LOCAL_SERVER_PROBE_TIMEOUT_SECONDS
) -> DescriptionReachability:
    """Whether the configured description provider can be used, without sending a prompt. Never raises."""
    provider = description.provider
    problem = static_provider_problem(description)
    if problem is not None:
        return DescriptionReachability(False, problem.reason)
    if provider == _CLAUDE_CLI:
        if resolve_claude_binary() is not None:
            return DescriptionReachability(True, None)
        return DescriptionReachability(False, "the 'claude' program was not found on PATH")
    if provider in _PROBE_PATHS:
        return await _probe_local_server(provider, local_server_url(description), timeout_seconds)
    return DescriptionReachability(True, None)


def probed_server_label(provider: str, base_url: str) -> str:
    """``host:port`` of the server the probe of *provider* asks: the name an operator message uses for that server.

    Taken from the RAW configured value, never from the URL :func:`_probe_url` builds: that URL is rebuilt from the netloc and
    may name part of a credential the value did not parse to (``alice:12345/rest@server`` asks ``alice:12345``). So the
    label is :func:`~archon_search.llm_client.server_address.host_port` of the value, ``UNKNOWN_HOST`` when it does not
    parse, except that an ``ollama`` value without a scheme or port gains the 11434 the probe (and the SDK) ask."""
    address = parse_server_address(base_url)
    if address is None:
        return UNKNOWN_HOST
    if provider == _OLLAMA and address.port is None and not is_url(base_url):
        address = ServerAddress(address.host, _OLLAMA_DEFAULT_PORT)
    return address.label


async def _probe_local_server(provider: str, base_url: str, timeout_seconds: float) -> DescriptionReachability:
    # An Ollama reason names the server (the WARNING does not); a llama_cpp reason does not, because its WARNING already
    # reads "the server at <host:port> is not reachable (<reason>)". The reasons carry no parenthesis of their own: both
    # WARNINGs wrap the reason in one.
    host = probed_server_label(provider, base_url)
    named = f" from {host}" if provider == _OLLAMA else ""
    try:
        url = _probe_url(provider, base_url)
        # httpx limits each phase, not the whole request: the outer bound keeps a trickling server from holding the probe.
        async with asyncio.timeout(timeout_seconds), httpx.AsyncClient(timeout=timeout_seconds) as client:
            response = await client.get(url)
    except Exception as failure:  # noqa: BLE001 — every failure reads as "not reachable"; the probe never raises
        return DescriptionReachability(False, f"no answer{named}: {type(failure).__name__}")
    if not response.is_success:
        answered = f"{host} answered" if provider == _OLLAMA else "answered"
        return DescriptionReachability(False, f"{answered} HTTP {response.status_code}")
    return DescriptionReachability(True, None)


def _probe_url(provider: str, base_url: str) -> str:
    """The URL the probe asks. The ``ollama`` SDK reads a scheme-less host the way the wizard writes it (``http://`` and
    port 11434 when none is given), so the probe does too; llama.cpp's client does not, so its value is used as given and
    a scheme-less one reads as not reachable, exactly like its generations. Whether a value has a scheme is
    :func:`~archon_search.llm_client.server_address.is_url`, the question the label asks too."""
    base_url = base_url.strip()
    if provider == _OLLAMA and not is_url(base_url):
        parts = urlsplit(f"//{base_url}")  # keeps any ``user:password@``: the SDK sends it as basic auth too
        base_url = f"http://{parts.netloc}" if parts.port is not None else f"http://{parts.netloc}:{_OLLAMA_DEFAULT_PORT}"
    return base_url.rstrip("/") + _PROBE_PATHS[provider]


async def run_description_probe(state: State, description: DescriptionConfig) -> None:
    """The probe task: publish the verdict on ``state.description_reachability`` and log one WARNING when the provider is
    not usable. Never raises (a probe bug reads as unreachable). A llama-server that does not answer is reported through
    ``warn_llama_cpp_unreachable``, the one WARNING per server (``host:port``) that the context-window lookup and the
    startup reachability probe use too, so the paths that find the same outage log it once; that line is attributed to
    ``archon_search.llm_client.llama_cpp_client``, not to this module's logger. Only that network verdict goes through the
    per-server key: a probe that failed unexpectedly is no statement about the server and takes the generic
    ``warn_description_unreachable`` WARNING."""
    try:
        verdict = await probe_description_reachability(description)
    except Exception as failure:  # noqa: BLE001 — never raises: an unexpected probe failure is a finding, not a crash
        verdict = DescriptionReachability(False, f"the probe failed unexpectedly ({type(failure).__name__})")
        state.description_reachability = verdict
        warn_description_unreachable(description.provider, verdict.reason)  # no network verdict: no per-server key
        return
    state.description_reachability = verdict
    if verdict.reachable:
        return
    if description.provider == _LLAMA_CPP:
        warn_llama_cpp_unreachable(local_server_url(description), verdict.reason)
    else:
        warn_description_unreachable(description.provider, verdict.reason)


def backfill_candidates(metas: Iterable[CollectionMeta]) -> list[CollectionMeta]:
    """The collections the backfill fills, in the order it visits them: namespace, then name.

    Only a collection with documents and no description, whose stored status is not ``failed`` (a failed collection
    waits for its next qualifying ingest or ``collection describe``) and whose description is not manual.
    """
    picked = [
        meta
        for meta in metas
        if not meta.description
        and meta.description_status != DESCRIPTION_STATUS_FAILED
        and meta.description_source != DESCRIPTION_SOURCE_MANUAL
        and meta.doc_count > 0
    ]
    return sorted(picked, key=lambda meta: (meta.namespace, meta.name))


async def run_description_backfill(state: State, store: BackfillStore, lifecycle: BackfillLifecycle) -> None:
    """The backfill task (S33, S34, S86). Ends when every candidate was asked for, when the service is closed, or when the
    task is cancelled at shutdown (a generation in flight is the lifecycle's to cancel and drain). Never raises."""
    try:
        await _backfill(state, store, lifecycle)
    except Exception as failure:  # noqa: BLE001 — never raises: a backfill bug ends the backfill, not the server
        logger.warning("Description backfill stopped unexpectedly (%s)", type(failure).__name__)


async def _backfill(state: State, store: BackfillStore, lifecycle: BackfillLifecycle) -> None:
    sync_task = state._startup_sync_task
    if sync_task is not None:
        await asyncio.wait({sync_task})  # any end state: done, failed or crashed
    if state.sync_result == StartupSyncResult.SUPPRESSED:
        logger.info(
            "Description backfill skipped: the startup sync is suppressed (the previous run died mid-ingest, so the "
            "index may be stale); run POST /sync, then restart to backfill"
        )
        return
    probe_task = state._description_probe_task
    if probe_task is not None:
        await asyncio.wait({probe_task})
    reachability: DescriptionReachability | None = state.description_reachability
    if reachability is None or not reachability.reachable:
        reason = "the provider was not probed" if reachability is None else reachability.reason
        logger.info("Description backfill skipped: the description provider is not reachable (%s)", reason)
        return
    window_task = state._llm_window_task
    if window_task is not None:  # the client's context window decides how much of a collection one prompt may hold
        await asyncio.wait({window_task})

    try:
        metas = await store.get_all_collections_meta()
    except Exception as failure:  # noqa: BLE001 — an unreadable store ends the backfill, not the server
        logger.warning("Description backfill stopped: the collections could not be listed (%s)", type(failure).__name__)
        return
    generated = failed = 0
    for meta in backfill_candidates(metas):
        ticket = await lifecycle.request_generation(meta.name, meta.namespace, GenerationKind.BACKFILL)
        if ticket.outcome is RequestOutcome.CLOSED:
            return
        if ticket.outcome is not RequestOutcome.ACCEPTED:  # already running, manual, described meanwhile, gone, unreadable
            continue
        settlement = await ticket.settled
        if settlement.result is GenerationResult.SHUTTING_DOWN:
            return
        generated += settlement.result is GenerationResult.GENERATED
        failed += settlement.result is GenerationResult.FAILED
    logger.info("Description backfill finished: %d generated, %d failed", generated, failed)
