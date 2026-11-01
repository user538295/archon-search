"""BE-36 (collection-descriptions plan, C5, C12, S36, S37, S38, S54, S56, S75, S84): the description startup findings.

The validator is plain functions over a ``DescriptionConfig``, so these tests call it directly and read the log; the real
app, its probe and a fake model server are exercised in ``tests/integration/test_be36_description_startup.py``.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from archon_search.config import DescriptionConfig, load_config
from archon_search.server import description_backfill, description_startup
from archon_search.server.description_backfill import (
    host_port,
    local_server_url,
    probe_description_reachability,
    static_provider_problem,
)
from archon_search.server.description_startup import (
    is_loopback_host,
    log_description_startup,
    off_notice,
    warn_hybrid_without_descriptions,
)
from archon_search.server.provider_dependency import check_provider_package
from archon_search.uv_tool import extra_install_hint

#: The validator logs under its own name; the unreachable-provider WARNING is the probe's shared one (BE-35's logger).
_LOGGERS = {description_startup.__name__, description_backfill.__name__}
_KEY_VARIABLE = "ANTHROPIC_API_KEY"
_OPENAI_KEY_VARIABLE = "OPENAI_API_KEY"
_PROVIDER_EXTRA = {"ollama": "ollama", "openai": "openai-provider", "anthropic": "hyde"}


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        # loopback
        ("127.0.0.1", True),
        ("127.1.2.3", True),
        ("[::1]", True),
        ("[::1]:11434", True),
        ("::1", True),
        ("localhost", True),
        ("localhost.", True),
        ("LOCALHOST", True),
        ("localhost:11434", True),
        ("http://localhost:11434", True),
        ("http://127.0.0.1:8080/v1", True),
        ("http://user:secret@localhost:11434", True),
        ("::ffff:127.0.0.1", True),
        ("http://[::ffff:127.0.0.1]:11434", True),
        ("  http://localhost:11434  ", True),
        # not loopback
        ("0.0.0.0", False),
        ("http://0.0.0.0:11434", False),
        ("192.168.1.5", False),
        ("http://192.168.1.5:11434", False),
        ("host.docker.internal", False),
        ("http://host.docker.internal:11434", False),
        ("example.com", False),
        ("http://example.com", False),
        ("localhost.example.com", False),
        ("http://localhost@example.com", False),
        ("localhost..", False),
        ("::ffff:192.168.1.5", False),
        ("[::]", False),
        ("[::1", False),
        ("[127.0.0.1]", False),
        ("", False),
        ("   ", False),
        ("http://", False),
        ("http://[::1", False),
    ],
)
def test_is_loopback_host_table(value: str, expected: bool) -> None:
    assert is_loopback_host(value) is expected


def _section(**changes: Any) -> DescriptionConfig:
    return DescriptionConfig(**changes)


def _enabled(**changes: Any) -> DescriptionConfig:
    return _section(enabled=True, **changes)


def test_off_notice_only_when_key_set_and_section_absent(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(_KEY_VARIABLE, "sk-test")
    notice = off_notice(section_present=False)
    assert notice is not None
    assert "[description]" in notice
    assert "enabled = true" in notice
    assert "enabled = false" in notice  # says how to silence it

    assert off_notice(section_present=True) is None  # `enabled = false` (or any section) silences it

    monkeypatch.delenv(_KEY_VARIABLE)
    assert off_notice(section_present=False) is None  # no key, no notice


def test_off_notice_is_silenced_by_a_written_disabled_section(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Through the real loader: no section keeps the notice, ``enabled = false`` silences it."""
    monkeypatch.setenv(_KEY_VARIABLE, "sk-test")
    absent = tmp_path / "absent.toml"
    absent.write_text("[server]\nport = 8765\n", encoding="utf-8")
    written = tmp_path / "written.toml"
    written.write_text("[description]\nenabled = false\n", encoding="utf-8")

    assert off_notice(section_present=load_config(absent).description_section_present) is not None
    assert off_notice(section_present=load_config(written).description_section_present) is None
    assert off_notice(section_present=load_config(tmp_path / "missing.toml").description_section_present) is not None


def _records(caplog: pytest.LogCaptureFixture, level: int) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.name in _LOGGERS and r.levelno == level]


def _log(config: DescriptionConfig, *, section_present: bool = True) -> None:
    log_description_startup(config, section_present=section_present)


def test_disabled_description_logs_nothing_but_the_off_notice(
    caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(_KEY_VARIABLE, raising=False)
    config = _section(enabled=False, provider="openai", think=True, include_restricted_chunks=True)
    with caplog.at_level(logging.DEBUG):
        _log(config, section_present=True)
        _log(config, section_present=False)  # no key: no notice
    assert [r for r in caplog.records if r.name in _LOGGERS] == []

    monkeypatch.setenv(_KEY_VARIABLE, "sk-test")
    with caplog.at_level(logging.DEBUG):
        _log(_section(), section_present=False)
    assert len(_records(caplog, logging.INFO)) == 1
    assert _records(caplog, logging.WARNING) == []


def test_enabled_description_logs_no_off_notice(caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(_KEY_VARIABLE, "sk-test")
    notice = off_notice(section_present=False)
    assert notice is not None
    with caplog.at_level(logging.INFO):
        _log(_enabled(provider="ollama"), section_present=False)
    assert notice not in _records(caplog, logging.INFO)


@pytest.mark.parametrize("provider", ["anthropic", "openai", "claude_cli"])
def test_think_set_is_ignored_with_a_warning_for_providers_without_a_switch(
    provider: str, caplog: pytest.LogCaptureFixture
) -> None:
    for think in (True, False):
        caplog.clear()
        with caplog.at_level(logging.WARNING):
            _log(_enabled(provider=provider, think=think))
        ignored = [m for m in _records(caplog, logging.WARNING) if "[description].think" in m]
        assert len(ignored) == 1, ignored
        assert provider in ignored[0]
        assert "ignored" in ignored[0]
        assert str(think).lower() in ignored[0]


@pytest.mark.parametrize("provider", ["anthropic", "openai", "claude_cli", "ollama", "llama_cpp"])
def test_think_default_never_warns(provider: str, caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING):
        _log(_enabled(provider=provider, think=None))
    assert not any("[description].think" in m for m in _records(caplog, logging.WARNING))


@pytest.mark.parametrize("provider", ["ollama", "llama_cpp"])
def test_think_is_not_reported_for_local_providers(provider: str, caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING):
        _log(_enabled(provider=provider, think=True))
    assert not any("[description].think" in m for m in _records(caplog, logging.WARNING))


@pytest.mark.parametrize("provider", ["anthropic", "openai", "claude_cli"])
def test_restricted_chunks_switch_is_ignored_with_a_warning_for_cloud_and_claude_cli(
    provider: str, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.WARNING):
        _log(_enabled(provider=provider, include_restricted_chunks=True))
    restricted = [m for m in _records(caplog, logging.WARNING) if "include_restricted_chunks" in m]
    assert len(restricted) == 1, restricted
    assert provider in restricted[0]
    assert "ignored" in restricted[0]


@pytest.mark.parametrize(
    ("provider", "url_field", "url"),
    [
        ("ollama", "ollama_base_url", "http://127.0.0.1:11434"),
        ("llama_cpp", "llama_cpp_base_url", "http://localhost:8080"),
    ],
)
def test_restricted_chunks_on_a_loopback_local_provider_gets_one_warning_naming_provider_and_host(
    provider: str, url_field: str, url: str, caplog: pytest.LogCaptureFixture
) -> None:
    config = _enabled(provider=provider, include_restricted_chunks=True, **{url_field: url})
    with caplog.at_level(logging.WARNING):
        _log(config)
    restricted = [m for m in _records(caplog, logging.WARNING) if "include_restricted_chunks" in m]
    assert len(restricted) == 1, restricted
    host = url.split("//")[1]
    assert provider in restricted[0]
    assert host in restricted[0]
    assert "every reader" in restricted[0]


def test_restricted_chunks_on_a_remote_local_provider_gets_a_second_warning_without_credentials(
    caplog: pytest.LogCaptureFixture,
) -> None:
    config = _enabled(
        provider="ollama",
        ollama_base_url="http://user:secret@gpu-box.internal:11434/",
        include_restricted_chunks=True,
    )
    with caplog.at_level(logging.WARNING):
        _log(config)
    restricted = [m for m in _records(caplog, logging.WARNING) if "include_restricted_chunks" in m]
    assert len(restricted) == 2, restricted
    assert all("gpu-box.internal:11434" in m for m in restricted)
    assert "not loopback" in restricted[1]
    assert not any("secret" in r.getMessage() for r in caplog.records)


def test_restricted_chunks_off_never_warns(caplog: pytest.LogCaptureFixture) -> None:
    for provider in ("anthropic", "ollama"):
        with caplog.at_level(logging.WARNING):
            _log(_enabled(provider=provider))
    assert not any("include_restricted_chunks" in m for m in _records(caplog, logging.WARNING))


@pytest.mark.parametrize("provider", ["anthropic", "openai", "claude_cli"])
def test_privacy_info_for_cloud_providers(provider: str, caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.INFO):
        _log(_enabled(provider=provider))
    infos = [m for m in _records(caplog, logging.INFO) if "leave the machine" in m]
    assert len(infos) == 1, infos
    assert provider in infos[0]
    assert "retry" in infos[0]


@pytest.mark.parametrize(
    ("provider", "url_field"),
    [("ollama", "ollama_base_url"), ("llama_cpp", "llama_cpp_base_url")],
)
def test_privacy_info_for_local_providers_only_on_a_non_loopback_host(
    provider: str, url_field: str, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.INFO):
        _log(_enabled(provider=provider, **{url_field: "http://127.0.0.1:1"}))
    assert not any("leave the machine" in m for m in _records(caplog, logging.INFO))

    caplog.clear()
    with caplog.at_level(logging.INFO):
        _log(_enabled(provider=provider, **{url_field: "http://user:secret@192.168.1.5:1/v1"}))
    infos = [m for m in _records(caplog, logging.INFO) if "leave the machine" in m]
    assert len(infos) == 1, infos
    assert "192.168.1.5:1" in infos[0]
    assert not any("secret" in r.getMessage() for r in caplog.records)


def test_missing_key_for_a_cloud_provider_warns_once_under_the_shared_unreachable_key(
    caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(_KEY_VARIABLE, raising=False)
    config = _enabled(provider="anthropic")
    with caplog.at_level(logging.WARNING):
        _log(config)
        _log(config)  # a second boot in the same process says nothing more
    unusable = [m for m in _records(caplog, logging.WARNING) if "not reachable" in m]
    assert len(unusable) == 1, unusable
    assert _KEY_VARIABLE in unusable[0]


@pytest.mark.parametrize(("provider", "extra"), sorted(_PROVIDER_EXTRA.items()))
def test_missing_package_warns_with_the_install_hint(
    provider: str, extra: str, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setitem(sys.modules, provider, None)
    with caplog.at_level(logging.WARNING):
        _log(_enabled(provider=provider))
    warnings = _records(caplog, logging.WARNING)
    assert len(warnings) == 1, warnings
    assert f"'{provider}' package is not installed" in warnings[0]
    assert extra_install_hint(extra) in warnings[0]


@pytest.mark.parametrize("provider", ["llama_cpp", "claude_cli"])
def test_providers_without_a_package_never_report_a_missing_one(
    provider: str, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    for package in ("anthropic", "openai", "ollama"):
        monkeypatch.setitem(sys.modules, package, None)
    with caplog.at_level(logging.WARNING):
        _log(_enabled(provider=provider))
    assert not any("package" in m for m in _records(caplog, logging.WARNING))


@pytest.mark.parametrize(("provider", "extra"), sorted(_PROVIDER_EXTRA.items()))
async def test_a_missing_package_reads_as_not_reachable_with_a_reason_free_of_the_install_command(
    provider: str, extra: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The probe's verdict (shown by ``/status``) names the package but never the install command, which a ``uv tool``
    install builds from the receipt and may carry index credentials or paths; the package is checked before the key."""
    monkeypatch.setitem(sys.modules, provider, None)
    monkeypatch.setenv(_KEY_VARIABLE, "sk-test")
    monkeypatch.setenv(_OPENAI_KEY_VARIABLE, "sk-test")
    verdict = await probe_description_reachability(_enabled(provider=provider))
    assert verdict.reachable is False
    assert verdict.reason == f"the '{provider}' package is not installed"
    assert extra_install_hint(extra) not in verdict.reason


@pytest.mark.parametrize(
    "error",
    [
        RuntimeError("the SDK refuses this pydantic version"),
        ModuleNotFoundError("No module named 'httpx'", name="httpx"),  # a dependency is absent, not the SDK
    ],
    ids=["non-import-error", "other-module-missing"],
)
def test_a_package_that_fails_to_import_offers_no_install_command(
    error: BaseException, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken(name: str) -> None:
        raise error

    monkeypatch.setattr("archon_search.server.provider_dependency.importlib", SimpleNamespace(import_module=broken))
    problem = check_provider_package("ollama")
    assert problem is not None
    assert problem.install_hint is None
    assert problem.summary() == f"the 'ollama' package failed to import ({type(error).__name__})"

    with caplog.at_level(logging.WARNING):
        _log(_enabled(provider="ollama"))
    warnings = _records(caplog, logging.WARNING)
    assert len(warnings) == 1, warnings
    assert "failed to import" in warnings[0]
    assert "run:" not in warnings[0]


def test_static_problem_names_a_missing_package_before_a_missing_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(_OPENAI_KEY_VARIABLE, raising=False)
    monkeypatch.setitem(sys.modules, "openai", None)
    problem = static_provider_problem(_enabled(provider="openai"))
    assert problem is not None
    assert problem.reason == "the 'openai' package is not installed"
    assert problem.remedy == extra_install_hint("openai-provider")


def test_static_problem_names_a_missing_key_without_a_remedy(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(_OPENAI_KEY_VARIABLE, raising=False)
    problem = static_provider_problem(_enabled(provider="openai"))
    assert problem is not None
    assert problem.reason == f"{_OPENAI_KEY_VARIABLE} is not set"
    assert problem.remedy is None


@pytest.mark.parametrize("provider", ["llama_cpp", "claude_cli"])
def test_static_problem_is_none_for_providers_that_need_no_package_or_key(provider: str) -> None:
    assert static_provider_problem(_enabled(provider=provider)) is None


def test_host_port_names_host_and_port_only() -> None:
    assert host_port("http://user:secret@gpu-box.internal:11434/api?token=x") == "gpu-box.internal:11434"
    assert host_port("localhost:11434") == "localhost:11434"
    assert host_port("http://[::1") == "the configured host"
    assert host_port("") == "the configured host"


def test_local_server_url_follows_the_provider() -> None:
    config = _enabled(ollama_base_url="http://ollama-host:1", llama_cpp_base_url="http://llama-host:2")
    config.provider = "ollama"
    assert local_server_url(config) == "http://ollama-host:1"
    config.provider = "llama_cpp"
    assert local_server_url(config) == "http://llama-host:2"


def test_a_failing_check_costs_only_its_own_finding(caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(_description: DescriptionConfig) -> None:
        raise RuntimeError("secret internals")

    monkeypatch.setattr(description_startup, "static_provider_problem", boom)
    with caplog.at_level(logging.INFO):
        _log(_enabled(provider="anthropic", think=True))  # must not raise
    warnings = _records(caplog, logging.WARNING)
    assert any("_warn_unusable could not be completed (RuntimeError)" in m for m in warnings)
    assert any("[description].think" in m for m in warnings)  # the next checks still ran
    assert any("leave the machine" in m for m in _records(caplog, logging.INFO))
    assert not any("secret internals" in r.getMessage() for r in caplog.records)
    assert all(r.exc_info is None for r in caplog.records)


class _Store:
    def __init__(self, metas: list[Any] | Exception) -> None:
        self._metas = metas

    async def get_all_collections_meta(self) -> list[Any]:
        if isinstance(self._metas, Exception):
            raise self._metas
        return self._metas


async def test_hybrid_without_descriptions_warns_naming_the_centroid_fallback(caplog: pytest.LogCaptureFixture) -> None:
    store = _Store(
        [
            SimpleNamespace(name="a", description="x"),
            SimpleNamespace(name="b", description=""),
            SimpleNamespace(name="c", description=""),
        ]
    )
    with caplog.at_level(logging.WARNING):
        await warn_hybrid_without_descriptions("hybrid", store)
    warnings = _records(caplog, logging.WARNING)
    assert len(warnings) == 1, warnings
    assert "centroid" in warnings[0]
    assert "2 of 3" in warnings[0]


@pytest.mark.parametrize(
    ("strategy", "metas"),
    [
        ("centroid", [SimpleNamespace(name="b", description="")]),
        ("hybrid", [SimpleNamespace(name="a", description="x")]),
        ("hybrid", []),
    ],
    ids=["centroid-strategy", "all-described", "no-collections"],
)
async def test_hybrid_check_is_silent_otherwise(strategy: str, metas: list[Any], caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.DEBUG):
        await warn_hybrid_without_descriptions(strategy, _Store(metas))
    assert [r for r in caplog.records if r.name in _LOGGERS] == []


async def test_hybrid_check_never_raises_on_an_unreadable_store(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING):
        await warn_hybrid_without_descriptions("hybrid", _Store(RuntimeError("disk gone")))
    assert any("could not be completed (RuntimeError)" in m for m in _records(caplog, logging.WARNING))
    assert not any("disk gone" in r.getMessage() for r in caplog.records)
