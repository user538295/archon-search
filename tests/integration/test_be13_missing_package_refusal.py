"""BE-13 (collection descriptions, S65, C12) — HyDE and RAG Fusion refuse to start on a missing provider package.

The refusal lives in ``create_app`` (``_check_provider_deps``), not in the shared client: a feature the operator
turned on whose provider package is absent is a configuration error, unlike graph enrichment, which only warns.
Booting goes through the real TOML loader and ``create_app`` of ``make_real_app``; the package is masked in
``sys.modules`` so ``import <package>`` raises ``ModuleNotFoundError``.
"""

from __future__ import annotations

import logging
import sys
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from archon_search.config import ConfigError
from archon_search.server.schemas import WarmupResult
from archon_search.uv_tool import extra_install_hint
from tests._fake_model_server import FakeModelServer, keep_loopback_out_of_proxies
from tests._install_hint_leak import (
    assert_install_hint_logged_not_sent,
    patch_install_hint,
)
from tests.integration.conftest import ingest_doc, make_real_app

pytestmark = pytest.mark.integration

_COLLECTION = "be13-lost-package"
_WARMUP_WAIT_SECONDS = 30.0

#: provider -> (extra named in the install hint, model line the section needs)
_PROVIDERS = {
    "ollama": ("ollama", 'model = "qwen2.5:3b"\n'),
    "openai": ("openai-provider", 'model = "gpt-4o-mini"\n'),
    "anthropic": ("hyde", ""),
}


def _toml(section: str, provider: str) -> str:
    _, model_line = _PROVIDERS[provider]
    return f'[{section}]\nenabled = true\nprovider = "{provider}"\n{model_line}'


def _refusal_message(section: str, provider: str) -> str:
    extra, _ = _PROVIDERS[provider]
    reason = (
        "enabled=true with provider='anthropic' but the 'anthropic' package is not installed"
        if provider == "anthropic"
        else f"provider='{provider}' but the '{provider}' package is not installed"
    )
    return f"[{section}] {reason}; run: {extra_install_hint(extra)}"


def _assert_refuses_to_start(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, section: str, provider: str) -> None:
    monkeypatch.setitem(sys.modules, provider, None)

    with (
        pytest.raises(ConfigError) as refusal,
        make_real_app(tmp_path, monkeypatch, toml_content=_toml(section, provider)),
    ):
        pytest.fail(f"the app started although the [{section}] provider package is missing")

    assert str(refusal.value) == _refusal_message(section, provider)


@pytest.mark.parametrize("provider", sorted(_PROVIDERS))
def test_hyde_missing_package_refuses_to_start(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, provider: str) -> None:
    """S65: a masked ``ollama``, ``openai`` or ``anthropic`` package stops startup with the error naming it."""
    _assert_refuses_to_start(tmp_path, monkeypatch, "hyde", provider)


@pytest.mark.parametrize("provider", sorted(_PROVIDERS))
def test_rag_fusion_missing_package_refuses_to_start(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, provider: str
) -> None:
    """S65: the same refusal for RAG Fusion, naming the ``rag_fusion`` section."""
    _assert_refuses_to_start(tmp_path, monkeypatch, "rag_fusion", provider)


@pytest.fixture
def _no_proxy_for_loopback(monkeypatch: pytest.MonkeyPatch) -> None:
    """A developer's HTTP_PROXY must not capture the loopback requests of the fake model server."""
    keep_loopback_out_of_proxies(monkeypatch)


def _await_warmup(client: TestClient) -> None:
    """Wait for the lifespan warm-up: a search that starts before it ends goes lexical-only and skips HyDE."""
    deadline = time.monotonic() + _WARMUP_WAIT_SECONDS
    while getattr(client.app.state, "warmup_result", None) in (None, WarmupResult.PENDING):
        assert time.monotonic() < deadline, "lifespan warm-up did not finish"
        time.sleep(0.01)


@pytest.mark.usefixtures("_no_proxy_for_loopback")
def test_package_lost_after_startup_falls_back_and_keeps_the_install_command_off_the_wire(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A package that disappears after boot is no client error: the search answers 200 without HyDE, and the
    install command, which can carry credentials in a uv tool environment, reaches the server log only."""
    patch_install_hint(monkeypatch, "archon_search.uv_tool")
    caplog.set_level(logging.WARNING)
    with FakeModelServer() as server:
        toml = (
            f'[hyde]\nenabled = true\nprovider = "ollama"\nmodel = "qwen2.5:3b"\nollama_base_url = "{server.base_url}"\n'
        )
        with make_real_app(tmp_path, monkeypatch, toml_content=toml) as (client, _cfg, key):
            ingest_doc(client, _COLLECTION, "A document about the rollout.", "/docs/rollout.md", api_key=key)
            _await_warmup(client)
            monkeypatch.setitem(sys.modules, "ollama", None)  # the package is gone after the boot check passed
            response = client.post(
                "/search",
                json={"query": "rollout", "collection": _COLLECTION, "hyde": True},
                headers={"Authorization": f"Bearer {key}"},
            )

    assert response.status_code == 200
    assert response.json()["hyde_applied"] is False
    assert_install_hint_logged_not_sent(caplog, response.text)
