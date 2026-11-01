"""BE-36 (collection-descriptions plan, C5, C12, S36, S37, S38, S51, S75): the description startup findings on a real app.

Real lifespan and a ``FakeModelServer`` (or a port nothing listens on) as the provider; ``caplog`` reads what an operator
would read in the log. The validator's own rules are pinned in ``tests/server/test_description_startup.py``.

Run with: ``uv run pytest tests/integration/test_be36_description_startup.py -m integration``.
"""

from __future__ import annotations

import contextlib
import logging
import secrets
import sys
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from archon_search.collection_meta import DESCRIPTION_STATUS_FAILED
from archon_search.server import description_backfill, description_startup
from tests._fake_model_server import (
    FakeModelServer,
    keep_loopback_out_of_proxies,
    unreachable_port,
)
from tests._install_hint_leak import (
    INSTALL_HINT_SECRET,
    INSTALL_HINT_SENTINEL,
    patch_install_hint,
)
from tests.integration.conftest import ingest_file_via_path
from tests.integration.conftest import make_real_app as _make_real_app

pytestmark = pytest.mark.integration

_MODEL = "description-model"
_WINDOW = 8192
_KEY_VARIABLE = "ANTHROPIC_API_KEY"
_NON_LOOPBACK_URL = "http://192.0.2.1:11434"  # TEST-NET-1: never answers; the host guard refuses the connection anyway
_WAIT_SECONDS = 30.0
_POLL_SECONDS = 0.01
_PROVIDER_TIMEOUT_SECONDS = 10
_TEAM = "team-a"
_COLLECTION = "startup-notes"
_STARTUP_TASKS = (
    "_startup_sync_task",
    "_llm_window_task",
    "_description_probe_task",
    "_description_backfill_task",
    "_description_hybrid_task",
)
_LEAVE_THE_MACHINE = "leave the machine"
_LOGGERS = {description_startup.__name__, description_backfill.__name__}


@pytest.fixture(autouse=True)
def _no_proxy_for_loopback(monkeypatch: pytest.MonkeyPatch) -> None:
    keep_loopback_out_of_proxies(monkeypatch)


def _wait_for(condition: Callable[[], bool], what: str) -> None:
    deadline = time.monotonic() + _WAIT_SECONDS
    while not condition():
        assert time.monotonic() < deadline, (
            f"{what} did not happen within {_WAIT_SECONDS}s"
        )
        time.sleep(_POLL_SECONDS)


def _ollama_description(base_url: str, *, extra: str = "") -> str:
    return (
        "[description]\n"
        "enabled = true\n"
        'provider = "ollama"\n'
        f'model = "{_MODEL}"\n'
        f'ollama_base_url = "{base_url}"\n'
        f"ollama_context_tokens = {_WINDOW}\n"
        f"timeout_seconds = {_PROVIDER_TIMEOUT_SECONDS}\n"
        f"{extra}"
    )


def _messages(caplog: pytest.LogCaptureFixture, level: int) -> list[str]:
    return [
        r.getMessage()
        for r in caplog.records
        if r.name in _LOGGERS and r.levelno == level
    ]


def _probed(client: TestClient) -> Any:
    _wait_for(
        lambda: client.app.state.description_reachability is not None,
        "the startup probe",
    )
    return client.app.state.description_reachability


@contextlib.contextmanager
def make_real_app(*args: Any, **kwargs: Any) -> Iterator[tuple[TestClient, Any, str]]:
    """``make_real_app`` that lets the startup tasks (the context-window resolution, the probe, the hybrid check, the
    backfill, the startup sync) end before the app shuts down: a task cancelled mid-request leaves a pooled connection
    that is closed on a dead loop at garbage collection and fails an unrelated later test."""
    with _make_real_app(*args, **kwargs) as (client, config, key):
        try:
            yield client, config, key
        finally:
            state = client.app.state
            for name in _STARTUP_TASKS:
                task = getattr(state, name)
                if task is not None:
                    _wait_for(task.done, f"the {name} to end")


def test_enabled_but_unusable_provider_warns_and_boots(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """S51, C5: an unreachable local server boots, warns once and is reported not reachable."""
    with unreachable_port() as port, caplog.at_level(logging.INFO):
        toml = _ollama_description(f"http://127.0.0.1:{port}")
        with make_real_app(tmp_path, monkeypatch, toml_content=toml) as (
            client,
            _config,
            _key,
        ):
            assert client.get("/health").status_code == 200
            assert _probed(client).reachable is False
    unusable = [m for m in _messages(caplog, logging.WARNING) if "not reachable" in m]
    assert len(unusable) == 1, unusable


def test_enabled_cloud_provider_without_key_warns_once_and_boots(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.delenv(_KEY_VARIABLE, raising=False)
    toml = '[description]\nenabled = true\nprovider = "anthropic"\n'
    with (
        caplog.at_level(logging.INFO),
        make_real_app(tmp_path, monkeypatch, toml_content=toml) as (client, _config, _key),
    ):
        assert client.get("/health").status_code == 200
        assert _probed(client).reachable is False
    unusable = [m for m in _messages(caplog, logging.WARNING) if "not reachable" in m]
    # The probe alone would also log one "not reachable" WARNING; the unit test pins that the validator reports it too.
    assert len(unusable) == 1, unusable
    assert _KEY_VARIABLE in unusable[0]


def test_every_provider_down_boots(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """S51: HyDE, RAG Fusion and descriptions all point at a dead server; the server still boots and answers."""
    with unreachable_port() as port, caplog.at_level(logging.INFO):
        url = f"http://127.0.0.1:{port}"
        features = "".join(
            f'[{name}]\nenabled = true\nprovider = "ollama"\nmodel = "{_MODEL}"\nollama_base_url = "{url}"\n'
            f"ollama_context_tokens = {_WINDOW}\n"
            for name in ("hyde", "rag_fusion")
        )
        with make_real_app(
            tmp_path, monkeypatch, toml_content=features + _ollama_description(url)
        ) as (client, _config, _key):
            assert client.get("/health").status_code == 200
            assert _probed(client).reachable is False
    assert (
        len([m for m in _messages(caplog, logging.WARNING) if "not reachable" in m])
        == 1
    )


def test_privacy_info_for_cloud_and_non_loopback_hosts_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """S38: anthropic and a remote Ollama host get the INFO; a loopback Ollama does not."""
    monkeypatch.setenv(_KEY_VARIABLE, "sk-ant-test")
    with (
        caplog.at_level(logging.INFO),
        make_real_app(
            tmp_path / "cloud",
            monkeypatch,
            toml_content='[description]\nenabled = true\nprovider = "anthropic"\n',
        ),
    ):
        pass
    cloud = [m for m in _messages(caplog, logging.INFO) if _LEAVE_THE_MACHINE in m]
    assert len(cloud) == 1 and "anthropic" in cloud[0], cloud

    caplog.clear()
    with (
        FakeModelServer() as server,
        caplog.at_level(logging.INFO),
        make_real_app(
            tmp_path / "loopback",
            monkeypatch,
            toml_content=_ollama_description(server.base_url),
        ),
    ):
        pass
    assert not [m for m in _messages(caplog, logging.INFO) if _LEAVE_THE_MACHINE in m]

    caplog.clear()
    with (
        caplog.at_level(logging.INFO),
        make_real_app(
            tmp_path / "remote",
            monkeypatch,
            toml_content=_ollama_description(_NON_LOOPBACK_URL),
        ),
    ):
        pass
    remote = [m for m in _messages(caplog, logging.INFO) if _LEAVE_THE_MACHINE in m]
    assert len(remote) == 1 and "192.0.2.1:11434" in remote[0], remote


def test_off_notice_at_boot_follows_key_and_section(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """S36: key set and no section -> one INFO; ``enabled = false`` written -> nothing."""
    monkeypatch.setenv(_KEY_VARIABLE, "sk-ant-test")
    with caplog.at_level(logging.INFO), make_real_app(tmp_path / "absent", monkeypatch, toml_content="[search]\n"):
        pass
    assert (
        len([m for m in _messages(caplog, logging.INFO) if "[description]" in m]) == 1
    )

    caplog.clear()
    with (
        caplog.at_level(logging.INFO),
        make_real_app(
            tmp_path / "written",
            monkeypatch,
            toml_content="[description]\nenabled = false\n",
        ),
    ):
        pass
    assert _messages(caplog, logging.INFO) == []
    assert _messages(caplog, logging.WARNING) == []


def _seed_collection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, namespaces: dict[str, str]
) -> None:
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    (corpus / "notes.txt").write_text(
        "Archon search keeps collections of local documents searchable.\n" * 20,
        encoding="utf-8",
    )
    with make_real_app(
        tmp_path,
        monkeypatch,
        toml_content="[description]\nenabled = false\n",
        namespaces=namespaces,
    ) as (
        client,
        _config,
        admin_key,
    ):
        ingest_file_via_path(client, _COLLECTION, str(corpus), api_key=admin_key)


def test_hybrid_without_descriptions_warns_centroid_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """S37: hybrid routing with an undescribed collection warns and names the centroid; the centroid strategy stays quiet."""
    namespaces = {secrets.token_hex(32): _TEAM}
    _seed_collection(tmp_path, monkeypatch, namespaces)

    with (
        caplog.at_level(logging.WARNING),
        make_real_app(
            tmp_path,
            monkeypatch,
            toml_content='[routing]\nrouting_strategy = "hybrid"\n',
            namespaces=namespaces,
        ) as (client, _config, _key),
    ):
        _wait_for(lambda: client.app.state._description_hybrid_task.done(), "the hybrid check")
    hybrid = [m for m in _messages(caplog, logging.WARNING) if "hybrid" in m]
    assert len(hybrid) == 1, hybrid
    assert "centroid" in hybrid[0]
    assert "1 of 1" in hybrid[0]

    caplog.clear()
    with (
        caplog.at_level(logging.WARNING),
        make_real_app(tmp_path, monkeypatch, toml_content="[search]\n", namespaces=namespaces) as (client, _config, _key),
    ):
        _wait_for(lambda: client.app.state._description_hybrid_task.done(), "the hybrid check")
    assert not [m for m in _messages(caplog, logging.WARNING) if "centroid" in m]


def test_missing_description_package_warns_and_generation_fails_visibly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """C12, S75: the ``ollama`` package is missing; the server boots with one WARNING naming it and the install command,
    the probe reports the provider not reachable, a generation fails visibly (status ``failed``, one WARNING for every
    attempt of the process) and nothing reaches the model server."""
    namespaces = {secrets.token_hex(32): _TEAM}
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    (corpus / "notes.txt").write_text(
        "Archon search keeps collections of local documents searchable.\n" * 20,
        encoding="utf-8",
    )
    patch_install_hint(monkeypatch, "archon_search.server.provider_dependency")
    monkeypatch.setitem(sys.modules, "ollama", None)
    with FakeModelServer() as server, caplog.at_level(logging.INFO):
        toml = _ollama_description(
            server.base_url, extra="backfill_on_startup = true\n"
        )
        with make_real_app(
            tmp_path, monkeypatch, toml_content=toml, namespaces=namespaces
        ) as (client, _config, admin_key):
            assert client.get("/health").status_code == 200
            verdict = _probed(client)
            assert verdict.reachable is False
            assert "ollama" in verdict.reason
            assert INSTALL_HINT_SECRET not in verdict.reason  # what /status shows never carries the command
            _wait_for(
                client.app.state._description_backfill_task.done, "the backfill task"
            )  # skipped, not failed attempts
            assert server.requests == []

            headers = {"Authorization": f"Bearer {admin_key}"}
            for attempt in range(2):
                name = f"{_COLLECTION}-{attempt}"
                ingest_file_via_path(client, name, str(corpus), api_key=admin_key)
                _wait_for(
                    lambda name=name: (
                        client.get(f"/collections/{name}", headers=headers).json()[
                            "description_status"
                        ]
                        == DESCRIPTION_STATUS_FAILED
                    ),
                    f"status failed of {name}",
                )
            assert server.requests == []

    package_warnings = [
        m for m in _messages(caplog, logging.WARNING) if "package is not installed" in m
    ]
    assert len(package_warnings) == 1, package_warnings
    assert "'ollama'" in package_warnings[0]
    assert INSTALL_HINT_SENTINEL in package_warnings[0]  # the install command reaches the log only
    skipped = [m for m in _messages(caplog, logging.INFO) if "backfill skipped" in m]
    assert len(skipped) == 1, skipped
    generation = [
        r.getMessage()
        for r in caplog.records
        if r.levelno == logging.WARNING
        and "Description generation failed" in r.getMessage()
    ]
    assert len(generation) == 1, (
        generation
    )  # one WARNING per process however many attempts fail
