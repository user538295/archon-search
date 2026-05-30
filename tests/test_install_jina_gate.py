"""Tests for the Jina license gate functions in archon_search/install.py (Task 3.2)."""
from __future__ import annotations

import pytest

from archon_search.profiles import JINA_RERANKER_MODEL, get_profile
from archon_search.install import _requires_jina_license, _prompt_jina_license


# ---------------------------------------------------------------------------
# _requires_jina_license
# ---------------------------------------------------------------------------

def test_requires_jina_license_true_for_multilingual_balanced():
    profile = get_profile("balanced", True)
    assert profile.reranker == JINA_RERANKER_MODEL
    assert _requires_jina_license(profile) is True


def test_requires_jina_license_false_for_english():
    for name in ("minimal", "balanced", "max"):
        profile = get_profile(name, False)
        assert _requires_jina_license(profile) is False


def test_requires_jina_license_false_for_multilingual_minimal():
    profile = get_profile("minimal", True)
    assert profile.reranker is None
    assert _requires_jina_license(profile) is False


# ---------------------------------------------------------------------------
# _prompt_jina_license
# ---------------------------------------------------------------------------

def test_prompt_jina_non_interactive_raises_systemexit(capsys):
    """non_interactive=True without accept flag → SystemExit(1); input() NOT called."""
    with pytest.raises(SystemExit) as exc_info:
        _prompt_jina_license(non_interactive=True, accept_jina_license=False)
    assert exc_info.value.code == 1
    # Verify the warning block was printed
    captured = capsys.readouterr()
    assert "CC-BY-NC-4.0" in captured.out


def test_prompt_jina_accept_does_not_raise(monkeypatch):
    """Interactive: typing 'accept' → no exception."""
    monkeypatch.setattr("builtins.input", lambda _: "accept")
    _prompt_jina_license(non_interactive=False, accept_jina_license=False)  # must not raise


def test_prompt_jina_accept_uppercase_does_not_raise(monkeypatch):
    """Interactive: typing 'ACCEPT' → no exception (case-insensitive)."""
    monkeypatch.setattr("builtins.input", lambda _: "ACCEPT")
    _prompt_jina_license(non_interactive=False, accept_jina_license=False)


def test_prompt_jina_accept_with_whitespace_does_not_raise(monkeypatch):
    """Interactive: typing ' accept ' → no exception (strip applied)."""
    monkeypatch.setattr("builtins.input", lambda _: " accept ")
    _prompt_jina_license(non_interactive=False, accept_jina_license=False)


def test_prompt_jina_decline_raises_systemexit(monkeypatch):
    """Interactive: typing 'no' → SystemExit(1)."""
    monkeypatch.setattr("builtins.input", lambda _: "no")
    with pytest.raises(SystemExit) as exc_info:
        _prompt_jina_license(non_interactive=False, accept_jina_license=False)
    assert exc_info.value.code == 1


def test_prompt_jina_accept_jina_license_flag_skips_prompt(monkeypatch):
    """accept_jina_license=True → no SystemExit, no input() called (even with non_interactive=True)."""
    called = []
    monkeypatch.setattr("builtins.input", lambda _: called.append(True) or "")
    _prompt_jina_license(non_interactive=True, accept_jina_license=True)  # must not raise
    assert called == [], "input() must not be called when accept_jina_license=True"
