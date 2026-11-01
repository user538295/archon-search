"""Every startup ask of a local model server gives it the same time to answer.

The context-window lookup, the reachability probe of ``validate_models_async`` and the description probe all ask the same
llama-server (or Ollama) at boot. With different bounds a server that answers in 4 s is up for one and down for another: the
validation probe then reports ``llama_cpp_ok = false`` (and skips the graph extraction probe) for a server the window lookup
just read a window from. The bound is one constant; these tests read what each path actually hands to its request.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Self

import httpx
import pytest

from archon_search import model_validation
from archon_search.config import DescriptionConfig, SearchConfig
from archon_search.llm_client.llama_cpp_client import LlamaCppClient
from archon_search.llm_client.ollama_client import OllamaClient
from archon_search.server import description_backfill
from archon_search.server.llm_composition import FeatureClient, resolve_context_windows
from tests._fake_model_server import keep_loopback_out_of_proxies, unreachable_port

_SERVER = "http://127.0.0.1:9"
_OLLAMA_WINDOW = 4096


@pytest.fixture(autouse=True)
def _no_proxy_for_loopback(monkeypatch: pytest.MonkeyPatch) -> None:
    keep_loopback_out_of_proxies(monkeypatch)


class _RecordingHttpClient:
    """Stands in for ``httpx.AsyncClient``: remembers the ``timeout`` it was built with and answers 200."""

    timeouts: list[float]

    def __init__(self, *, timeout: float, **_: object) -> None:
        type(self).timeouts.append(timeout)

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        return None

    async def get(self, url: str) -> httpx.Response:
        return httpx.Response(200, request=httpx.Request("GET", url))


def _recorder() -> type[_RecordingHttpClient]:
    return type("_Recorder", (_RecordingHttpClient,), {"timeouts": []})


async def _validation_probe_timeout(monkeypatch: pytest.MonkeyPatch) -> float:
    recorder = _recorder()
    monkeypatch.setattr(model_validation.httpx, "AsyncClient", recorder)
    config = SearchConfig()
    config.hyde.enabled = True
    config.hyde.provider = "llama_cpp"
    config.hyde.llama_cpp_base_url = _SERVER

    assert await model_validation._probe_llama_cpp(config) == {"127.0.0.1:9": True}

    [timeout] = recorder.timeouts
    return timeout


async def _description_probe_timeout(monkeypatch: pytest.MonkeyPatch) -> float:
    recorder = _recorder()
    monkeypatch.setattr(description_backfill.httpx, "AsyncClient", recorder)

    verdict = await description_backfill.probe_description_reachability(
        DescriptionConfig(enabled=True, provider="llama_cpp", llama_cpp_base_url=_SERVER)
    )

    assert verdict.reachable is True
    [timeout] = recorder.timeouts
    return timeout


async def _window_lookup_timeouts(monkeypatch: pytest.MonkeyPatch) -> tuple[float, float]:
    """The bound of the llama-server window read and of the Ollama model-details read."""
    asked: dict[str, float] = {}

    async def read_props(self: LlamaCppClient, timeout_seconds: float) -> None:
        asked["llama_cpp"] = timeout_seconds

    async def read_details(self: OllamaClient, timeout_seconds: float) -> None:
        asked["ollama"] = timeout_seconds

    monkeypatch.setattr(LlamaCppClient, "probe_context_window", read_props)
    monkeypatch.setattr(OllamaClient, "model_max_context_tokens", read_details)
    with unreachable_port() as port:  # the SDK guard of the suite builds an Ollama client only for a port a test holds
        ollama = OllamaClient(model="m", base_url=f"http://127.0.0.1:{port}", context_tokens=_OLLAMA_WINDOW)
        await resolve_context_windows(
            [FeatureClient("hyde", LlamaCppClient(model="m", base_url=_SERVER)), FeatureClient("graph", ollama)]
        )
    return asked["llama_cpp"], asked["ollama"]


async def test_every_startup_probe_of_a_local_server_waits_equally_long(monkeypatch: pytest.MonkeyPatch) -> None:
    llama_window, ollama_window = await _window_lookup_timeouts(monkeypatch)
    bounds = SimpleNamespace(
        llama_window=llama_window,
        ollama_window=ollama_window,
        validation_probe=await _validation_probe_timeout(monkeypatch),
        description_probe=await _description_probe_timeout(monkeypatch),
    )

    assert len(set(vars(bounds).values())) == 1, vars(bounds)
