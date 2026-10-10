"""Tests for archon_search.uv_tool — recording wizard extras in a uv tool receipt.

A ``uv tool install archon-search`` environment is re-synced to the requirements
recorded in its ``uv-receipt.toml`` by every ``uv tool upgrade``. The helpers
under test rebuild a ``uv tool install`` command that reproduces the receipt plus
one new extra (so uv records it), derive the environment that targets the same
tool / bin directories, and produce a method-aware install hint for error
messages.
"""
from __future__ import annotations

import importlib.metadata
import sys
from pathlib import Path
from typing import Any

import pytest

from archon_search.uv_tool import (
    RECEIPT_FILENAME,
    UnsupportedReceiptError,
    extra_install_hint,
    find_receipt,
    install_extra_command,
    install_option_flags,
    tool_env,
    torch_backend,
)

_BIN_DIR = "/home/u/.local/bin"


def _receipt(
    *requirements: Any,
    entrypoints: list[dict[str, str]] | None = None,
    options: dict[str, Any] | None = None,
    **tool: Any,
) -> dict[str, Any]:
    table: dict[str, Any] = {
        "requirements": list(requirements) or [{"name": "archon-search"}],
        "entrypoints": entrypoints
        if entrypoints is not None
        else [{"name": "archon-search", "install-path": f"{_BIN_DIR}/archon-search", "from": "archon-search"}],
        **tool,
    }
    if options is not None:
        table["options"] = options
    return {"tool": table}


def _command(receipt: dict[str, Any], extra: str = "graph", backend: str | None = None) -> list[str]:
    return install_extra_command(receipt, extra, backend=backend)


# ---------------------------------------------------------------------------
# install_extra_command — supported receipts
# ---------------------------------------------------------------------------


def test_plain_registry_install_adds_the_extra() -> None:
    assert _command(_receipt()) == ["uv", "tool", "install", "archon-search[graph]"]


def test_registry_specifier_and_marker_are_kept() -> None:
    receipt = _receipt(
        {"name": "archon-search", "specifier": ">=26.9, <27", "marker": "python_full_version >= '3.12'"}
    )
    assert _command(receipt) == [
        "uv", "tool", "install", "archon-search[graph]>=26.9, <27 ; python_full_version >= '3.12'",
    ]


def test_existing_extras_are_kept_and_new_extra_appended() -> None:
    receipt = _receipt({"name": "archon-search", "extras": ["code", "multilingual"]})
    assert _command(receipt)[3] == "archon-search[code,multilingual,graph]"


def test_extra_already_recorded_is_not_duplicated() -> None:
    receipt = _receipt({"name": "archon-search", "extras": ["graph", "code"]})
    assert _command(receipt)[3] == "archon-search[graph,code]"


def test_tool_name_is_matched_after_pep503_normalisation() -> None:
    receipt = _receipt({"name": "Archon_Search"})
    assert _command(receipt)[3] == "Archon_Search[graph]"


def test_path_source_renders_a_file_url() -> None:
    receipt = _receipt({"name": "archon-search", "path": "/dist/archon_search-1.0-py3-none-any.whl"})
    assert _command(receipt)[3] == (
        "archon-search[graph] @ file:///dist/archon_search-1.0-py3-none-any.whl"
    )


def test_directory_source_renders_a_file_url() -> None:
    receipt = _receipt({"name": "archon-search", "extras": ["code"], "directory": "/src/archon search"})
    assert _command(receipt)[3] == "archon-search[code,graph] @ file:///src/archon%20search"


def test_url_source_with_subdirectory_and_marker() -> None:
    receipt = _receipt(
        {
            "name": "archon-search",
            "url": "https://example.org/archon.zip",
            "subdirectory": "pkg",
            "marker": "sys_platform == 'linux'",
        }
    )
    assert _command(receipt)[3] == (
        "archon-search[graph] @ https://example.org/archon.zip#subdirectory=pkg ; sys_platform == 'linux'"
    )


def test_url_source_without_subdirectory() -> None:
    receipt = _receipt({"name": "archon-search", "url": "https://example.org/archon-1.0-py3-none-any.whl"})
    assert _command(receipt)[3] == "archon-search[graph] @ https://example.org/archon-1.0-py3-none-any.whl"


def test_with_requirements_become_with_flags() -> None:
    receipt = _receipt(
        {"name": "archon-search"},
        {"name": "rich"},
        {"name": "httpx", "extras": ["http2"], "specifier": ">=0.27"},
    )
    assert _command(receipt) == [
        "uv", "tool", "install", "archon-search[graph]", "--with", "rich", "--with", "httpx[http2]>=0.27",
    ]


def test_every_supported_option_kind_is_passed_through() -> None:
    receipt = _receipt(
        options={
            "index-url": "https://pypi.org/simple",
            "extra-index-url": ["https://download.pytorch.org/whl/cpu", "https://mirror.example/simple"],
            "find-links": ["file:///links", "file:///links"],
            "no-index": True,
            "index-strategy": "unsafe-best-match",
            "prerelease": "if-necessary-or-explicit",
        }
    )
    assert _command(receipt)[4:] == [
        "--index-url", "https://pypi.org/simple",
        "--extra-index-url", "https://download.pytorch.org/whl/cpu",
        "--extra-index-url", "https://mirror.example/simple",
        "--find-links", "file:///links",
        "--find-links", "file:///links",
        "--no-index",
        "--index-strategy", "unsafe-best-match",
        "--prerelease", "if-necessary-or-explicit",
    ]


def test_false_boolean_option_is_omitted() -> None:
    assert _command(_receipt(options={"no-index": False})) == ["uv", "tool", "install", "archon-search[graph]"]


def test_torch_backend_is_appended_last() -> None:
    receipt = _receipt({"name": "archon-search"}, {"name": "rich"}, options={"find-links": ["file:///l"]})
    assert _command(receipt, backend="cu126") == [
        "uv", "tool", "install", "archon-search[graph]", "--with", "rich",
        "--find-links", "file:///l", "--torch-backend", "cu126",
    ]


def test_empty_optional_tool_lists_are_supported() -> None:
    receipt = _receipt(constraints=[], overrides=[], excludes=[], **{"build-constraint-dependencies": []})
    assert _command(receipt) == ["uv", "tool", "install", "archon-search[graph]"]


def test_entrypoint_without_from_is_a_main_package_entrypoint() -> None:
    """uv writes ``from`` on every entrypoint today (``ToolEntrypoint::to_toml``); receipts
    that predate ``--with-executables-from`` lack it, and all their entrypoints came from
    the tool itself, so re-installing reproduces them."""
    receipt = _receipt(entrypoints=[{"name": "archon-search", "install-path": f"{_BIN_DIR}/archon-search"}])
    assert _command(receipt) == ["uv", "tool", "install", "archon-search[graph]"]


# ---------------------------------------------------------------------------
# install_extra_command — unsupported receipts fail closed
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("receipt", "named"),
    [
        (_receipt(python="3.12"), "python"),
        (_receipt(constraints=[{"name": "numpy", "specifier": "<3"}]), "constraints"),
        (_receipt(overrides=[{"name": "numpy", "specifier": "<3"}]), "overrides"),
        (_receipt(excludes=["numpy"]), "excludes"),
        (_receipt(**{"build-constraint-dependencies": [{"name": "setuptools"}]}), "build-constraint-dependencies"),
        (_receipt(**{"future-key": 1}), "future-key"),
        ({"tool": {"requirements": [], "entrypoints": []}}, "requirements"),
        ({}, "[tool]"),
        (_receipt({"name": "other-tool"}), "other-tool"),
        (_receipt("archon-search[code]"), "archon-search[code]"),
        (_receipt({"name": "archon-search"}, "rich>=13"), "rich>=13"),
        (_receipt({"name": "archon-search", "editable": "/src"}), "editable"),
        (_receipt({"name": "archon-search", "virtual": "/src"}), "virtual"),
        (_receipt({"name": "archon-search", "git": "https://github.com/x/y?rev=1#abc"}), "git"),
        (_receipt({"name": "archon-search", "groups": ["dev"]}), "groups"),
        (_receipt({"name": "archon-search", "index": "https://mirror.example/simple"}), "index"),
        (_receipt({"name": "archon-search", "specifier": 1}), "specifier"),
        (_receipt({"name": "archon-search"}, {"specifier": ">=1"}), "without a name"),
        (_receipt({"name": "archon-search", "extras": "code"}), "extras"),
        (_receipt(entrypoints="archon-search"), "entrypoints"),  # type: ignore[arg-type]
        (_receipt(options=["find-links"]), "[tool.options]"),  # type: ignore[arg-type]
        (_receipt({"name": "archon-search", "conflict": {"package": "x"}}), "conflict"),
        (_receipt({"name": "archon-search", "path": "dist/a.whl"}), "dist/a.whl"),
        (_receipt({"name": "archon-search", "path": "/a.whl", "url": "https://x/a.whl"}), "path"),
        (_receipt({"name": "archon-search", "path": "/a.whl", "specifier": "==1"}), "specifier"),
        (_receipt({"name": "archon-search", "subdirectory": "pkg"}), "subdirectory"),
        (_receipt(options={"resolution": "lowest"}), "resolution"),
        (_receipt(options={"torch-backend": "cpu"}), "torch-backend"),
        (_receipt(options={"find-links": "file:///links"}), "find-links"),
        (
            _receipt(
                entrypoints=[
                    {"name": "archon-search", "install-path": f"{_BIN_DIR}/archon-search", "from": "archon-search"},
                    {"name": "rich-cli", "install-path": f"{_BIN_DIR}/rich-cli", "from": "rich-cli"},
                ]
            ),
            "rich-cli",
        ),
    ],
)
def test_unsupported_receipt_state_raises_naming_it(receipt: dict[str, Any], named: str) -> None:
    with pytest.raises(UnsupportedReceiptError) as excinfo:
        _command(receipt)
    assert named in str(excinfo.value)


# ---------------------------------------------------------------------------
# torch_backend
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("version", "backend"),
    [
        ("2.14.1+cpu", "cpu"),
        ("2.14.1+cu126", "cu126"),
        ("2.14.1+rocm6.3", "rocm6.3"),
        ("2.14.1+rocm6.2.4", "rocm6.2.4"),
        ("2.14.1+xpu", "xpu"),
        ("2.14.1", None),
    ],
)
def test_torch_backend_from_local_version_tag(version: str, backend: str | None) -> None:
    assert torch_backend(version) == backend


@pytest.mark.parametrize("version", ["2.14.1+git1234", "2.14.1+cuda", "2.14.1+rocm"])
def test_unmapped_torch_local_tag_is_unsupported(version: str) -> None:
    """A specialised build uv has no backend for must not be silently swapped by a re-resolve."""
    with pytest.raises(UnsupportedReceiptError, match=version.partition("+")[2]):
        torch_backend(version)


# ---------------------------------------------------------------------------
# install_option_flags
# ---------------------------------------------------------------------------


def test_install_option_flags_renders_the_receipt_options() -> None:
    receipt = _receipt(options={"index-url": "https://idx/simple", "find-links": ["file:///l"], "no-index": True})
    assert install_option_flags(receipt) == [
        "--index-url", "https://idx/simple", "--find-links", "file:///l", "--no-index",
    ]


def test_install_option_flags_without_options() -> None:
    assert install_option_flags(_receipt()) == []


@pytest.mark.parametrize(
    "receipt",
    [{}, _receipt(options={"exclude-newer": "2026-01-01"})],
    ids=["no-tool-table", "unsupported-option"],
)
def test_install_option_flags_raises_when_options_cannot_be_rendered(receipt: dict[str, Any]) -> None:
    with pytest.raises(UnsupportedReceiptError):
        install_option_flags(receipt)


# ---------------------------------------------------------------------------
# find_receipt
# ---------------------------------------------------------------------------


def _tool_prefix(root: Path, receipt_text: str = '[tool]\nrequirements = [{ name = "archon-search" }]\n') -> Path:
    prefix = root / "tools" / "archon-search"
    prefix.mkdir(parents=True)
    (prefix / RECEIPT_FILENAME).write_text(receipt_text)
    return prefix


def test_find_receipt_in_a_uv_tool_env(tmp_path: Path) -> None:
    prefix = _tool_prefix(tmp_path)
    assert find_receipt(prefix) == prefix / RECEIPT_FILENAME


def test_find_receipt_ignores_an_env_named_after_another_tool(tmp_path: Path) -> None:
    prefix = tmp_path / "other-tool"
    prefix.mkdir()
    (prefix / RECEIPT_FILENAME).write_text("[tool]\n")
    assert find_receipt(prefix) is None


def test_find_receipt_without_a_receipt(tmp_path: Path) -> None:
    prefix = tmp_path / "archon-search"
    prefix.mkdir()
    assert find_receipt(prefix) is None


def test_find_receipt_matches_the_tool_name_after_pep503_normalisation(tmp_path: Path) -> None:
    prefix = tmp_path / "Archon_Search"
    prefix.mkdir()
    (prefix / RECEIPT_FILENAME).write_text("[tool]\n")
    assert find_receipt(prefix) == prefix / RECEIPT_FILENAME


def test_find_receipt_ignores_a_directory_named_like_the_receipt(tmp_path: Path) -> None:
    prefix = tmp_path / "archon-search"
    (prefix / RECEIPT_FILENAME).mkdir(parents=True)
    assert find_receipt(prefix) is None


# ---------------------------------------------------------------------------
# tool_env
# ---------------------------------------------------------------------------


def test_tool_env_targets_the_receipts_tool_and_bin_dirs(tmp_path: Path) -> None:
    receipt_path = tmp_path / "tools" / "archon-search" / RECEIPT_FILENAME
    environ = {"PATH": "/usr/bin", "UV_PYTHON": "3.13", "UV_TOOL_DIR": "/elsewhere"}
    env = tool_env(receipt_path, _receipt(), environ)
    assert env == {"PATH": "/usr/bin", "UV_TOOL_DIR": str(tmp_path / "tools"), "UV_TOOL_BIN_DIR": _BIN_DIR}
    assert environ["UV_PYTHON"] == "3.13", "the caller's mapping must not be mutated"


def test_tool_env_without_a_main_entrypoint_leaves_bin_dir_unset(tmp_path: Path) -> None:
    receipt_path = tmp_path / "tools" / "archon-search" / RECEIPT_FILENAME
    env = tool_env(receipt_path, _receipt(entrypoints=[]), {"HOME": "/home/u"})
    assert env == {"HOME": "/home/u", "UV_TOOL_DIR": str(tmp_path / "tools")}


# ---------------------------------------------------------------------------
# extra_install_hint
# ---------------------------------------------------------------------------


@pytest.fixture
def torch_version(monkeypatch: pytest.MonkeyPatch) -> Any:
    """Set the installed torch version the code under test reads (None = not installed)."""
    real_version = importlib.metadata.version

    def _set(version: str | None) -> None:
        def _fake(name: str) -> str:
            if name != "torch":
                return real_version(name)
            if version is None:
                raise importlib.metadata.PackageNotFoundError(name)
            return version

        monkeypatch.setattr(importlib.metadata, "version", _fake)

    return _set


def test_hint_in_a_uv_tool_env_is_the_receipt_preserving_command(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, torch_version: Any
) -> None:
    prefix = _tool_prefix(
        tmp_path,
        '[tool]\nrequirements = [{ name = "archon-search", extras = ["code"] }, { name = "rich" }]\n'
        '[tool.options]\nfind-links = ["file:///links"]\n',
    )
    monkeypatch.setattr(sys, "prefix", str(prefix))
    torch_version("2.14.1+cpu")
    assert extra_install_hint("graph") == (
        "uv tool install 'archon-search[code,graph]' --with rich --find-links file:///links --torch-backend cpu"
    )


def test_hint_in_a_uv_tool_env_without_torch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, torch_version: Any
) -> None:
    monkeypatch.setattr(sys, "prefix", str(_tool_prefix(tmp_path)))
    torch_version(None)
    assert extra_install_hint("multilingual") == "uv tool install 'archon-search[multilingual]'"


@pytest.mark.parametrize(
    "receipt_text",
    [
        '[tool]\nrequirements = [{ name = "archon-search" }]\npython = "3.12"\n',
        "[tool\nnot toml",
    ],
    ids=["unsupported", "unreadable"],
)
def test_hint_in_a_uv_tool_env_with_an_unreproducible_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, receipt_text: str
) -> None:
    monkeypatch.setattr(sys, "prefix", str(_tool_prefix(tmp_path, receipt_text)))
    hint = extra_install_hint("graph")
    assert hint.startswith("uv tool install 'archon-search[graph]'  # ")
    for option in ("--with", "--python", "--torch-backend"):
        assert option in hint


def test_hint_with_an_unmapped_torch_build_is_the_plain_command(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, torch_version: Any
) -> None:
    monkeypatch.setattr(sys, "prefix", str(_tool_prefix(tmp_path)))
    torch_version("2.14.1+git1234")
    assert extra_install_hint("graph").startswith("uv tool install 'archon-search[graph]'  # ")


def test_hint_outside_a_uv_tool_env_is_pip(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "prefix", str(tmp_path / ".venv"))
    assert extra_install_hint("graph") == "pip install 'archon-search[graph]'"
