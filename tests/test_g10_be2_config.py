"""G10 BE-2 — provider + ollama_base_url config fields for HyDEConfig / RAGFusionConfig.

Tests:
- Default provider is "anthropic"
- Default ollama_base_url is "http://localhost:11434"
- Invalid provider raises ConfigError
- Missing ollama package with provider="ollama" raises ConfigError
- Missing openai package with provider="openai" raises ConfigError
- Empty model with non-anthropic provider raises ConfigError
- Non-anthropic provider with explicit model is OK
- Anthropic provider with empty model is OK (sentinel kept)
- Empty and whitespace-only ollama_base_url raises ConfigError
- rag_fusion with ollama + absent package raises ConfigError
- path_home_allowlist contains config.py entry with correct line number and SHA
- openai provider with empty model raises ConfigError (C2-I-15)
- rag_fusion ollama provider with empty model raises ConfigError (C2-I-14)
- _apply_toml rejects empty model string with ollama provider (C2-I-11)
"""
from __future__ import annotations

import sys
import types
from pathlib import Path

import pytest

from archon_search.config import ConfigError, HyDEConfig, RAGFusionConfig


# ---------------------------------------------------------------------------
# Default values
# ---------------------------------------------------------------------------


def test_config_defaults_provider_is_anthropic() -> None:
    """HyDEConfig and RAGFusionConfig must default to provider='anthropic'."""
    assert HyDEConfig().provider == "anthropic"
    assert RAGFusionConfig().provider == "anthropic"


def test_config_ollama_base_url_default() -> None:
    """HyDEConfig.ollama_base_url must default to 'http://localhost:11434'."""
    assert HyDEConfig().ollama_base_url == "http://localhost:11434"
    assert RAGFusionConfig().ollama_base_url == "http://localhost:11434"


# ---------------------------------------------------------------------------
# _apply_toml validation
# ---------------------------------------------------------------------------


def _make_toml_doc(section: str, **kwargs: object) -> object:
    """Build a minimal tomlkit document with ``section`` and ``kwargs`` fields."""
    import tomlkit  # noqa: PLC0415

    doc = tomlkit.document()
    tbl = tomlkit.table()
    for k, v in kwargs.items():
        tbl[k] = v
    doc[section] = tbl
    return doc


def test_config_invalid_provider_raises_config_error() -> None:
    """_apply_toml with provider='foobar' must raise ConfigError for hyde and rag_fusion."""
    from archon_search.config import SearchConfig, _apply_toml  # noqa: PLC0415

    # HyDE
    config = SearchConfig()
    doc = _make_toml_doc("hyde", provider="foobar")
    with pytest.raises(ConfigError, match="provider"):
        _apply_toml(config, doc)

    # RAG Fusion
    config2 = SearchConfig()
    doc2 = _make_toml_doc("rag_fusion", provider="foobar")
    with pytest.raises(ConfigError, match="provider"):
        _apply_toml(config2, doc2)


# ---------------------------------------------------------------------------
# Ollama package absent guard (startup ConfigError)
# ---------------------------------------------------------------------------


def test_config_ollama_package_absent_raises_config_error(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """create_app() with provider='ollama' + ollama package absent → ConfigError."""
    monkeypatch.setitem(sys.modules, "ollama", None)  # type: ignore[arg-type]
    monkeypatch.setenv("ARCHON_SEARCH_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("ARCHON_SEARCH_API_KEY", "test-key-abc123")

    from archon_search.config import HyDEConfig, SearchConfig  # noqa: PLC0415
    from archon_search.jobs.store import JobStore  # noqa: PLC0415
    from archon_search.server.app import create_app  # noqa: PLC0415

    config = SearchConfig()
    config.hyde = HyDEConfig(provider="ollama")

    with pytest.raises(ConfigError, match="ollama"):
        create_app(config, JobStore())


# ---------------------------------------------------------------------------
# Q5: empty model with non-anthropic provider raises ConfigError
# ---------------------------------------------------------------------------


def test_config_empty_model_with_non_anthropic_provider_raises_config_error(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """provider='ollama', model='' → ConfigError at startup naming the field."""
    monkeypatch.setenv("ARCHON_SEARCH_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("ARCHON_SEARCH_API_KEY", "test-key-abc123")

    # Make sure ollama IS importable (so only the empty-model guard fires)
    import types  # noqa: PLC0415
    fake_ollama = types.ModuleType("ollama")
    monkeypatch.setitem(sys.modules, "ollama", fake_ollama)

    from archon_search.config import HyDEConfig, SearchConfig  # noqa: PLC0415
    from archon_search.jobs.store import JobStore  # noqa: PLC0415
    from archon_search.server.app import create_app  # noqa: PLC0415

    config = SearchConfig()
    config.hyde = HyDEConfig(provider="ollama", model="")

    with pytest.raises(ConfigError, match="model"):
        create_app(config, JobStore())


def test_config_non_anthropic_with_explicit_model_ok(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """provider='ollama', model='llama3.2' (non-empty) → no ConfigError."""
    import types  # noqa: PLC0415
    fake_ollama = types.ModuleType("ollama")
    monkeypatch.setitem(sys.modules, "ollama", fake_ollama)

    monkeypatch.setenv("ARCHON_SEARCH_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("ARCHON_SEARCH_API_KEY", "test-key-abc123")

    from archon_search.config import HyDEConfig, SearchConfig  # noqa: PLC0415
    from archon_search.jobs.store import JobStore  # noqa: PLC0415
    from archon_search.server.app import create_app  # noqa: PLC0415

    config = SearchConfig()
    config.hyde = HyDEConfig(provider="ollama", model="llama3.2")

    # Should not raise ConfigError (may raise other errors related to real LanceDB,
    # but not the provider/model config guard).
    try:
        create_app(config, JobStore())
    except ConfigError as exc:
        pytest.fail(f"ConfigError raised unexpectedly: {exc}")


def test_config_anthropic_with_empty_model_ok(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """provider='anthropic', model='' → no ConfigError (sentinel only for non-Anthropic)."""
    monkeypatch.setenv("ARCHON_SEARCH_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("ARCHON_SEARCH_API_KEY", "test-key-abc123")

    from archon_search.config import HyDEConfig, SearchConfig  # noqa: PLC0415
    from archon_search.jobs.store import JobStore  # noqa: PLC0415
    from archon_search.server.app import create_app  # noqa: PLC0415

    config = SearchConfig()
    config.hyde = HyDEConfig(provider="anthropic", model="")

    try:
        create_app(config, JobStore())
    except ConfigError as exc:
        pytest.fail(f"ConfigError raised unexpectedly: {exc}")


# ---------------------------------------------------------------------------
# C1-I-21: openai package absent → ConfigError
# ---------------------------------------------------------------------------


def test_config_openai_package_absent_raises_config_error(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """create_app() with provider='openai' + openai package absent → ConfigError."""
    monkeypatch.setitem(sys.modules, "openai", None)  # type: ignore[arg-type]
    monkeypatch.setenv("ARCHON_SEARCH_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("ARCHON_SEARCH_API_KEY", "test-key-abc123")

    from archon_search.config import HyDEConfig, SearchConfig  # noqa: PLC0415
    from archon_search.jobs.store import JobStore  # noqa: PLC0415
    from archon_search.server.app import create_app  # noqa: PLC0415

    config = SearchConfig()
    config.hyde = HyDEConfig(provider="openai", model="gpt-4o-mini")

    with pytest.raises(ConfigError, match="openai"):
        create_app(config, JobStore())


# ---------------------------------------------------------------------------
# C1-I-22: empty and whitespace-only ollama_base_url raises ConfigError
# ---------------------------------------------------------------------------


def test_config_ollama_base_url_empty_raises_config_error() -> None:
    """_apply_toml with ollama_base_url='' must raise ConfigError for hyde."""
    from archon_search.config import SearchConfig, _apply_toml  # noqa: PLC0415

    config = SearchConfig()
    doc = _make_toml_doc("hyde", ollama_base_url="")
    with pytest.raises(ConfigError, match="ollama_base_url"):
        _apply_toml(config, doc)


def test_config_ollama_base_url_whitespace_raises_config_error() -> None:
    """_apply_toml with ollama_base_url='   ' (whitespace-only) must raise ConfigError."""
    from archon_search.config import SearchConfig, _apply_toml  # noqa: PLC0415

    config = SearchConfig()
    doc = _make_toml_doc("hyde", ollama_base_url="   ")
    with pytest.raises(ConfigError, match="ollama_base_url"):
        _apply_toml(config, doc)


def test_config_rag_fusion_ollama_base_url_whitespace_raises_config_error() -> None:
    """_apply_toml with [rag_fusion].ollama_base_url='   ' must raise ConfigError."""
    from archon_search.config import SearchConfig, _apply_toml  # noqa: PLC0415

    config = SearchConfig()
    doc = _make_toml_doc("rag_fusion", ollama_base_url="   ")
    with pytest.raises(ConfigError, match="ollama_base_url"):
        _apply_toml(config, doc)


# ---------------------------------------------------------------------------
# C1-I-23: rag_fusion with provider='ollama' + absent package → ConfigError
# ---------------------------------------------------------------------------


def test_config_rag_fusion_ollama_package_absent_raises_config_error(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """create_app() with rag_fusion provider='ollama' + ollama package absent → ConfigError."""
    monkeypatch.setitem(sys.modules, "ollama", None)  # type: ignore[arg-type]
    monkeypatch.setenv("ARCHON_SEARCH_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("ARCHON_SEARCH_API_KEY", "test-key-abc123")

    from archon_search.config import RAGFusionConfig, SearchConfig  # noqa: PLC0415
    from archon_search.jobs.store import JobStore  # noqa: PLC0415
    from archon_search.server.app import create_app  # noqa: PLC0415

    config = SearchConfig()
    config.rag_fusion = RAGFusionConfig(provider="ollama", model="llama3.2")

    with pytest.raises(ConfigError, match="ollama"):
        create_app(config, JobStore())


# ---------------------------------------------------------------------------
# C1-I-20: path_home_allowlist contains config.py entry with correct line number and SHA
# ---------------------------------------------------------------------------

_EXPECTED_CONFIG_LINE_NO = 319
_EXPECTED_CONFIG_SHA = "8c6844f3268afa9c4a632945843e776075a111407a0540b8a29041b99d669043"
_CONFIG_REL_PATH = "archon_search/config.py"


def test_path_home_allowlist_line_number_updated() -> None:
    """tests/path_home_allowlist.txt must contain the config.py entry with the correct
    line number (310) and the expected SHA — proving the allowlist was updated after
    the Path.home() callsite moved.
    """
    allowlist_path = Path(__file__).resolve().parent / "path_home_allowlist.txt"
    assert allowlist_path.exists(), f"Allowlist file not found: {allowlist_path}"

    expected_entry = f"{_CONFIG_REL_PATH}:{_EXPECTED_CONFIG_LINE_NO}:{_EXPECTED_CONFIG_SHA}"
    lines = allowlist_path.read_text(encoding="utf-8").splitlines()
    data_lines = [ln for ln in lines if ln.strip() and not ln.startswith("#")]

    assert expected_entry in data_lines, (
        f"Expected allowlist entry not found:\n  {expected_entry}\n"
        f"Actual config.py entries in allowlist:\n"
        + "\n".join(f"  {ln}" for ln in data_lines if _CONFIG_REL_PATH in ln)
    )


# ---------------------------------------------------------------------------
# C2-I-15: openai provider with empty model raises ConfigError
# ---------------------------------------------------------------------------


def test_config_openai_empty_model_raises_config_error(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """provider='openai' + openai package present + model='' → ConfigError at startup."""
    fake_openai = types.ModuleType("openai")
    monkeypatch.setitem(sys.modules, "openai", fake_openai)
    monkeypatch.setenv("ARCHON_SEARCH_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("ARCHON_SEARCH_API_KEY", "test-key-abc123")

    from archon_search.config import HyDEConfig, SearchConfig  # noqa: PLC0415
    from archon_search.jobs.store import JobStore  # noqa: PLC0415
    from archon_search.server.app import create_app  # noqa: PLC0415

    config = SearchConfig()
    config.hyde = HyDEConfig(provider="openai", model="")

    with pytest.raises(ConfigError, match="model"):
        create_app(config, JobStore())


# ---------------------------------------------------------------------------
# C2-I-14: rag_fusion ollama provider with empty model raises ConfigError
# ---------------------------------------------------------------------------


def test_config_rag_fusion_empty_model_raises_config_error(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """rag_fusion provider='ollama' + ollama package present + model='' → ConfigError at startup."""
    fake_ollama = types.ModuleType("ollama")
    monkeypatch.setitem(sys.modules, "ollama", fake_ollama)
    monkeypatch.setenv("ARCHON_SEARCH_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("ARCHON_SEARCH_API_KEY", "test-key-abc123")

    from archon_search.config import RAGFusionConfig, SearchConfig  # noqa: PLC0415
    from archon_search.jobs.store import JobStore  # noqa: PLC0415
    from archon_search.server.app import create_app  # noqa: PLC0415

    config = SearchConfig()
    config.rag_fusion = RAGFusionConfig(provider="ollama", model="")

    with pytest.raises(ConfigError, match="model"):
        create_app(config, JobStore())


# ---------------------------------------------------------------------------
# C2-I-11: _apply_toml rejects empty model string with non-anthropic provider
# ---------------------------------------------------------------------------


def test_config_toml_empty_model_with_ollama_provider_raises_config_error() -> None:
    """_apply_toml with [hyde] provider='ollama' and model='' raises ConfigError.

    _apply_toml validates model non-emptiness independently of provider — the
    generic empty-string guard at config.py:615-616 fires before _check_provider_deps.
    """
    from archon_search.config import SearchConfig, _apply_toml  # noqa: PLC0415

    config = SearchConfig()
    doc = _make_toml_doc("hyde", provider="ollama", model="")
    with pytest.raises(ConfigError, match="model"):
        _apply_toml(config, doc)
