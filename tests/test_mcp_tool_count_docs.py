"""The documented MCP tool counts and key-management tool names must match the registered tools.

Wire truth (derived from code, never hardcoded): ``mcp.create_app`` registers a base tool set
always and adds the key-management tools only when it receives a ``key_store``; the shipped
server always passes one.

Doc contract:

* ``DeveloperGuide/05_mcp_integration.md`` Principle 2 is the canonical statement — its bold
  heading states the total, its "always register" sentence states the base count, and its
  key-management parenthetical names exactly the gated tools.
* The ``tools/list`` check in 05 expects the total in ``result.tools``.
* ``Architecture/600_api_reference_or_public_interface.md`` "## MCP tools" states the same total
  and base count. Other docs link to 05 instead of repeating the numbers, except ``README.md``,
  whose copy ``test_readme_contract.py`` guards.
"""
from __future__ import annotations

import re
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from fastmcp.tools import Tool

from archon_search.key_manager import KeyStore
from tests._mcp_tools import registered_tools

pytestmark = pytest.mark.xdist_group("mcp")

_REPO_ROOT = Path(__file__).resolve().parent.parent
_DEV_GUIDE = _REPO_ROOT / "Documentation" / "DeveloperGuide" / "05_mcp_integration.md"
_API_REF = _REPO_ROOT / "Documentation" / "Architecture" / "600_api_reference_or_public_interface.md"

_NUMBER_WORDS = (
    "zero", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten", "eleven",
    "twelve", "thirteen", "fourteen", "fifteen", "sixteen", "seventeen", "eighteen", "nineteen", "twenty",
)
_NUMBER = r"(\d+|[A-Za-z]+)"
_PRINCIPLE_TOTAL = re.compile(rf"^2\. \*\*{_NUMBER} tools, named exactly as in `mcp\.py`\.\*\*", re.MULTILINE)
_ALWAYS_REGISTER = re.compile(rf"\b{_NUMBER} always register\b")
_KEY_TOOLS = re.compile(r"the \w+ key-management tools \(([^)]*)\) register only when `mcp\.create_app` receives")
_TOOLS_LIST_RESULT = re.compile(rf"a `result\.tools` array of {_NUMBER} entries")
_API_REF_COUNTS = re.compile(rf"\*\*{_NUMBER} tools total\*\* — the {_NUMBER} \S+ tools always register")
_CODE_SPAN = re.compile(r"`([^`]+)`")


def _as_int(token: str) -> int:
    """Read a count written as digits or (up to twenty) as an English word, any case."""
    return int(token) if token.isdigit() else _NUMBER_WORDS.index(token.lower())


def _single(pattern: re.Pattern[str], doc: str, what: str) -> re.Match[str]:
    matches = list(pattern.finditer(doc))
    assert len(matches) == 1, f"presence anchor: expected exactly one {what}, found {len(matches)}"
    return matches[0]


@pytest.fixture(scope="module")
def base_tools() -> dict[str, Tool]:
    return registered_tools(key_store=None)


@pytest.fixture(scope="module")
def all_tools() -> dict[str, Tool]:
    return registered_tools(key_store=MagicMock(spec=KeyStore))


@pytest.fixture(scope="module")
def key_tools(base_tools: dict[str, Tool], all_tools: dict[str, Tool]) -> set[str]:
    assert set(base_tools) < set(all_tools), "wire-truth drift: key_store no longer adds tools"
    return set(all_tools) - set(base_tools)


def test_dev_guide_principle_states_the_total_and_base_counts(
    base_tools: dict[str, Tool], all_tools: dict[str, Tool],
) -> None:
    doc = _DEV_GUIDE.read_text(encoding="utf-8")

    total = _single(_PRINCIPLE_TOTAL, doc, "Principle 2 tool-count heading")
    assert _as_int(total.group(1)) == len(all_tools), (
        f"05 Principle 2 says {total.group(1)} tools; the shipped server registers {len(all_tools)}"
    )
    always = _single(_ALWAYS_REGISTER, doc, "'always register' sentence")
    assert _as_int(always.group(1)) == len(base_tools), (
        f"05 says {always.group(1)} tools always register; {len(base_tools)} register without a key_store"
    )


def test_dev_guide_names_exactly_the_key_store_gated_tools(key_tools: set[str]) -> None:
    listed = _single(_KEY_TOOLS, _DEV_GUIDE.read_text(encoding="utf-8"), "key-management tool list")
    documented = set(_CODE_SPAN.findall(listed.group(1)))
    assert documented == key_tools, (
        f"05 key-management tool list drifted: omits {sorted(key_tools - documented)}, "
        f"invents {sorted(documented - key_tools)}"
    )


def test_dev_guide_tools_list_check_expects_the_total(all_tools: dict[str, Tool]) -> None:
    entries = _single(_TOOLS_LIST_RESULT, _DEV_GUIDE.read_text(encoding="utf-8"), "`result.tools` count")
    assert _as_int(entries.group(1)) == len(all_tools), (
        f"05 `tools/list` check expects {entries.group(1)} entries; the shipped server returns {len(all_tools)}"
    )


def test_api_reference_states_the_total_and_base_counts(
    base_tools: dict[str, Tool], all_tools: dict[str, Tool],
) -> None:
    counts = _single(_API_REF_COUNTS, _API_REF.read_text(encoding="utf-8"), "600 MCP tool-count sentence")
    assert (_as_int(counts.group(1)), _as_int(counts.group(2))) == (len(all_tools), len(base_tools)), (
        f"600 says {counts.group(1)} total / {counts.group(2)} always; "
        f"code registers {len(all_tools)} / {len(base_tools)}"
    )
