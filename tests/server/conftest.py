"""Shared fixtures for the server route tests."""
from __future__ import annotations

import sys
import types

import pytest


@pytest.fixture(scope="module")
def spacy_stub():
    """Ensure ``sys.modules["spacy"]`` is importable, and always restore it.

    Graph-enabled apps refuse to start without spaCy, so these modules used to
    install a bare stub — and three of them never removed it, shadowing the
    REAL spaCy for every later test in the same xdist worker (2026-08-19-030).
    That was harmless until a test needed genuine ``spacy.util``, at which
    point it surfaced as a cross-module flake under ``-n 8``.

    One fixture rather than three byte-identical copies: this is subtle
    teardown logic whose entire purpose is cross-test isolation, and the
    original fix had to find and patch all three at once (C2-B-15).

    Note the stub branch is normally dead in a dev checkout — spaCy is a hard
    dev dependency and xdist imports every collected module before running any
    test, so ``"spacy" in sys.modules`` is already true. It exists for
    environments without the ``[graph]`` extra.
    """
    if "spacy" in sys.modules:
        yield
        return
    sys.modules["spacy"] = types.ModuleType("spacy")
    try:
        yield
    finally:
        sys.modules.pop("spacy", None)
