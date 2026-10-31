"""BE-28 (S49): the Claude Agent SDK is neither a declared dependency, a locked package, nor named by the code.

The description generator talks to a model through the shared ``llm_client`` layer, so nothing imports the SDK any more.
"""
from __future__ import annotations

from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
PACKAGE_DIR = REPO_ROOT / "archon_search"
_NORMALISED_PACKAGE_NAME = "claude-agent-sdk"
_MIN_PRODUCTION_MODULES = 50  # positive control: the scan really walked the package


def _names_the_package(text: str) -> bool:
    """True for ``claude-agent-sdk``, ``claude_agent_sdk`` and ``claude.agent.sdk`` in any case (PEP 503 spellings)."""
    return _NORMALISED_PACKAGE_NAME in text.lower().replace("_", "-").replace(".", "-")


@pytest.mark.parametrize("manifest", ["pyproject.toml", "uv.lock"])
def test_sdk_not_declared_in_pyproject_or_lock(manifest: str) -> None:
    assert not _names_the_package((REPO_ROOT / manifest).read_text(encoding="utf-8")), f"{manifest} still names the removed package"


def test_no_production_module_names_the_sdk() -> None:
    """Any mention counts: an ``import`` at module scope or in a function, ``__import__`` or ``importlib`` by name."""
    modules = sorted(PACKAGE_DIR.rglob("*.py"))
    assert len(modules) >= _MIN_PRODUCTION_MODULES
    namers = [str(path.relative_to(REPO_ROOT)) for path in modules if _names_the_package(path.read_text(encoding="utf-8"))]
    assert namers == [], f"modules still name the removed SDK: {namers}"
