"""BE-16 (collection-descriptions plan, C12, S16, S65) — a graph provider whose package is missing warns and boots.

A missing ``openai``, ``ollama`` or ``anthropic`` package for ``[graph]`` is no startup error: ``create_app`` logs one
WARNING that names the package and the install command, composes no graph client (graph enrichment stays off: no
community summary, no model call), and the server starts. The same masked package still refuses to start for HyDE
(S65, ``test_be13_missing_package_refusal.py``). Booting goes through the real TOML loader and ``create_app`` of
``make_real_app``; the package is masked in ``sys.modules`` so ``import <package>`` raises ``ModuleNotFoundError``.
"""

from __future__ import annotations

import logging
import sys
import time
from pathlib import Path
from typing import Any

import pytest

from archon_search.config import ConfigError
from archon_search.server import app as app_module
from archon_search.uv_tool import extra_install_hint
from tests._fake_model_server import keep_loopback_out_of_proxies
from tests._graph_engine_stub import install_graph_engine_stub
from tests._install_hint_leak import INSTALL_HINT_SENTINEL, patch_install_hint
from tests.integration.conftest import make_real_app

pytestmark = pytest.mark.integration

_APP_LOGGER = "archon_search.server.app"
_VALIDATION_WAIT_SECONDS = 60.0
_POLL_SECONDS = 0.01
_GRAPH_MODEL = "graph-model"
_NO_PACKAGE_PROVIDERS = ("llama_cpp", "claude_cli")
_MASKED_PACKAGES = ("anthropic", "openai", "ollama")
#: provider -> (package, extra named in the install hint)
_PACKAGE_OF = {
    "ollama": ("ollama", "ollama"),
    "openai": ("openai", "openai-provider"),
    "anthropic": ("anthropic", "hyde"),
}


@pytest.fixture(autouse=True)
def _no_proxy_for_loopback(monkeypatch: pytest.MonkeyPatch) -> None:
    keep_loopback_out_of_proxies(monkeypatch)


def _graph_toml(provider: str, *, enabled: bool = True) -> str:
    lines = ["[graph]", f"enabled = {str(enabled).lower()}", f'provider = "{provider}"', f'extraction_model = "{_GRAPH_MODEL}"']
    return "\n".join(lines) + "\n"


def _warnings(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING and r.name == _APP_LOGGER]


def _await_validation(client) -> None:
    deadline = time.monotonic() + _VALIDATION_WAIT_SECONDS
    while client.app.state.model_validation is None:
        assert time.monotonic() < deadline, "the model validation did not finish in time"
        time.sleep(_POLL_SECONDS)


def _assert_warns_and_boots_with_graph_off(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog, provider: str) -> None:
    package, extra = _PACKAGE_OF[provider]
    install_graph_engine_stub(monkeypatch)
    monkeypatch.setitem(sys.modules, package, None)
    built: list[dict[str, Any]] = []
    real_build = app_module.build_llm_client

    def _spy(**kwargs: Any) -> Any:
        built.append(kwargs)
        return real_build(**kwargs)

    monkeypatch.setattr(app_module, "build_llm_client", _spy)
    caplog.set_level(logging.WARNING)
    with make_real_app(tmp_path, monkeypatch, toml_content=_graph_toml(provider)) as (client, config, _key):
        _await_validation(client)
        assert client.get("/health").status_code == 200
        assert client.app.state.graph_client is None, "graph enrichment must stay off: no client, no summaries"
        assert config.graph.enabled is True
        assert client.app.state.model_validation.provider_warnings == [], "the package gap is a log line, not a probe warning"
    assert [call for call in built if call["model"] == _GRAPH_MODEL] == [], "no graph client is composed, even for the probe"

    named = [m for m in _warnings(caplog) if f"'{package}' package is not installed" in m]
    assert len(named) == 1, _warnings(caplog)
    assert "[graph]" in named[0]
    assert f"run: {extra_install_hint(extra)}" in named[0]
    assert "graph enrichment stays off" in named[0]


def test_graph_missing_ollama_package_warns_and_boots_with_graph_off(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """S16: a masked ``ollama`` package is a WARNING naming the package and the install command; the app boots."""
    _assert_warns_and_boots_with_graph_off(tmp_path, monkeypatch, caplog, "ollama")


def test_graph_missing_openai_package_warns_and_boots_with_graph_off(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """S16: the same for ``openai``."""
    _assert_warns_and_boots_with_graph_off(tmp_path, monkeypatch, caplog, "openai")


def test_graph_missing_anthropic_package_warns_and_boots_with_graph_off(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """C12: ``anthropic`` needs its package too (extra ``hyde``), so it gets the same graph treatment."""
    _assert_warns_and_boots_with_graph_off(tmp_path, monkeypatch, caplog, "anthropic")


@pytest.mark.parametrize("provider", ["ollama", "openai"])
def test_missing_package_refuses_hyde_but_not_graph(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, provider: str
) -> None:
    """S65: with one package masked, ``[hyde]`` on that provider refuses to start while ``[graph]`` only warns."""
    package, extra = _PACKAGE_OF[provider]
    install_graph_engine_stub(monkeypatch)
    monkeypatch.setitem(sys.modules, package, None)
    caplog.set_level(logging.WARNING)
    hyde_toml = f'[hyde]\nenabled = true\nprovider = "{provider}"\nmodel = "some-model"\n'

    with (
        pytest.raises(ConfigError) as refusal,
        make_real_app(tmp_path, monkeypatch, toml_content=hyde_toml),
    ):
        pytest.fail("HyDE started although its provider package is missing")
    assert str(refusal.value) == (
        f"[hyde] provider='{provider}' but the '{package}' package is not installed; run: {extra_install_hint(extra)}"
    )

    with make_real_app(tmp_path, monkeypatch, toml_content=_graph_toml(provider)) as (client, _config, _key):
        assert client.get("/health").status_code == 200
        assert client.app.state.graph_client is None


@pytest.mark.parametrize("provider", _NO_PACKAGE_PROVIDERS)
def test_graph_provider_without_a_package_is_never_guarded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, provider: str
) -> None:
    """``llama_cpp`` (httpx is core) and ``claude_cli`` (a binary) need no package: masking all three SDKs changes nothing."""
    install_graph_engine_stub(monkeypatch)
    for package in _MASKED_PACKAGES:
        monkeypatch.setitem(sys.modules, package, None)
    caplog.set_level(logging.WARNING)

    with make_real_app(tmp_path, monkeypatch, toml_content=_graph_toml(provider)) as (client, _config, _key):
        assert client.app.state.graph_client is not None

    assert not [m for m in _warnings(caplog) if "package is not installed" in m]


def test_disabled_graph_with_a_missing_package_stays_quiet(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A graph that is off needs nothing: no WARNING about a package it would not use."""
    monkeypatch.setitem(sys.modules, "ollama", None)
    caplog.set_level(logging.WARNING)

    with make_real_app(tmp_path, monkeypatch, toml_content=_graph_toml("ollama", enabled=False)) as (client, _c, _k):
        assert client.get("/health").status_code == 200

    assert not [m for m in _warnings(caplog) if "package is not installed" in m]


def test_graph_install_command_is_logged_and_never_answered(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """The install command can carry credentials in a uv tool environment: it reaches the server log, no response."""
    install_graph_engine_stub(monkeypatch)
    patch_install_hint(monkeypatch, "archon_search.server.provider_dependency")
    monkeypatch.setitem(sys.modules, "openai", None)
    caplog.set_level(logging.WARNING)

    with make_real_app(tmp_path, monkeypatch, toml_content=_graph_toml("openai")) as (client, _config, key):
        wire = client.get("/status", headers={"Authorization": f"Bearer {key}"}).text

    assert INSTALL_HINT_SENTINEL not in wire
    assert any(INSTALL_HINT_SENTINEL in m for m in _warnings(caplog))
