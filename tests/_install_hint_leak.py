"""Shared fixtures for the "missing optional extra" wire tests (graph, jobs, HyDE package lost after boot).

In a uv tool env ``uv_tool.extra_install_hint`` returns a receipt-derived install
command that can carry ``--index-url`` credentials, ``--find-links`` and local
source paths. The tests assert the command goes to the server log only: graph-extra and
jobs answer a fixed ``*_DEPENDENCY_DETAIL`` constant, and a HyDE call whose package is
gone answers 200 with ``hyde_applied: false`` and no package text at all.
"""
from __future__ import annotations

import logging

import pytest

INSTALL_HINT_SECRET = "https://ci:t0ken@pypi.corp.example/simple"
INSTALL_HINT_PATH = "/Users/secret/wheels"
INSTALL_HINT_SENTINEL = (
    f"uv tool install --index-url {INSTALL_HINT_SECRET} --find-links {INSTALL_HINT_PATH} 'archon-search[x]'"
)


def patch_install_hint(monkeypatch: pytest.MonkeyPatch, module: str) -> None:
    """Make *module*'s ``extra_install_hint`` return the credential-bearing sentinel command."""
    monkeypatch.setattr(f"{module}.extra_install_hint", lambda extra: INSTALL_HINT_SENTINEL)


def assert_install_hint_logged_not_sent(caplog: pytest.LogCaptureFixture, wire: str) -> None:
    """The install command reaches the server log as a WARNING and never the wire."""
    assert INSTALL_HINT_SECRET not in wire, f"install command leaked onto the wire: {wire!r}"
    assert INSTALL_HINT_PATH not in wire, f"install command leaked onto the wire: {wire!r}"
    assert any(
        r.levelno == logging.WARNING and INSTALL_HINT_SENTINEL in r.getMessage() for r in caplog.records
    ), f"install command not logged as a warning: {[r.getMessage() for r in caplog.records]!r}"
