"""Documentation contract tests pinning README claims about the MCP tool
count and names, the default server port, and the auth-exempt path set
against the actual source."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from archon_search.config import SearchConfig, load_config
from archon_search.server.middleware_auth import _EXEMPT_PATHS

_REPO_ROOT = Path(__file__).parents[1]
README_PATH = _REPO_ROOT / "README.md"
MCP_PATH = _REPO_ROOT / "archon_search" / "server" / "mcp.py"


def test_readme_mcp_tool_count_matches_source() -> None:
    mcp_lines = MCP_PATH.read_text(encoding="utf-8").splitlines()
    gate_index = next(
        i for i, line in enumerate(mcp_lines) if "if key_store is not None:" in line
    )
    gate_indent = len(mcp_lines[gate_index]) - len(mcp_lines[gate_index].lstrip())
    gate_end_index = next(
        i
        for i, line in enumerate(mcp_lines[gate_index + 1 :], start=gate_index + 1)
        if line.strip() and (len(line) - len(line.lstrip())) <= gate_indent
    )

    tool_line_indexes = [i for i, line in enumerate(mcp_lines) if "@app.tool()" in line]
    gated = sum(1 for i in tool_line_indexes if gate_index < i < gate_end_index)
    unconditional = len(tool_line_indexes) - gated
    total = unconditional + gated

    assert total == 20, f"expected 20 @app.tool() registrations, found {total}"
    assert unconditional == 16
    assert gated == 4

    readme = README_PATH.read_text(encoding="utf-8")
    assert re.search(r"16 tools always register", readme)
    assert re.search(r"\b20 total\b", readme)

    mcp_source = MCP_PATH.read_text(encoding="utf-8")
    tool_names = re.findall(r"@app\.tool\(\)\s*\n\s*async def (\w+)", mcp_source)
    for name in tool_names:
        assert re.search(rf"`{name}`", readme), f"README MCP tools section must document `{name}`"


def test_readme_default_port_matches_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("ARCHON_SEARCH_HOST", raising=False)
    monkeypatch.delenv("ARCHON_SEARCH_PORT", raising=False)

    readme = README_PATH.read_text(encoding="utf-8")
    default_host = SearchConfig.__dataclass_fields__["host"].default
    default_port = SearchConfig.__dataclass_fields__["port"].default
    assert f"{default_host}:{default_port}" in readme, (
        f"README must reference the actual default host/port {default_host}:{default_port}"
    )

    serve_host = load_config(tmp_path / "nonexistent.toml", serve=True).host
    assert serve_host == "0.0.0.0"
    assert f"{serve_host}:{default_port}" in readme, (
        f"README Quick Start must reference the real `serve` bind address {serve_host}:{default_port}"
    )


def test_readme_documents_auth_exempt_paths() -> None:
    readme = README_PATH.read_text(encoding="utf-8")
    auth_section = readme.split("## Authentication", 1)[1].split("## Configuration", 1)[0]
    for path in _EXEMPT_PATHS:
        assert path in auth_section, f"README Authentication section must list exempt path {path}"
