"""Tests for Task 4.1 — _prompt_fasttext_license() in archon_search/install.py."""
from __future__ import annotations

import pytest

from archon_search.install import _prompt_fasttext_license


# ---------------------------------------------------------------------------
# _prompt_fasttext_license
# ---------------------------------------------------------------------------


def test_fasttext_license_accepted_flag():
    """accept_fasttext_license=True returns without printing or prompting."""
    # Should not raise, should not call input()
    _prompt_fasttext_license(non_interactive=False, accept_fasttext_license=True)


def test_fasttext_license_accepted_flag_non_interactive():
    """accept_fasttext_license=True returns even in non-interactive mode (flag takes priority)."""
    # non_interactive is irrelevant when accept_fasttext_license=True
    _prompt_fasttext_license(non_interactive=True, accept_fasttext_license=True)


def test_fasttext_license_accepted_flag_does_not_call_input(monkeypatch):
    """accept_fasttext_license=True must NOT call input()."""
    called = []
    monkeypatch.setattr("builtins.input", lambda _: called.append(True) or "")
    _prompt_fasttext_license(non_interactive=False, accept_fasttext_license=True)
    assert called == [], "input() must not be called when accept_fasttext_license=True"


def test_fasttext_license_non_interactive_declines(capsys):
    """non_interactive=True raises SystemExit(1); warning block was printed."""
    with pytest.raises(SystemExit) as exc_info:
        _prompt_fasttext_license(non_interactive=True, accept_fasttext_license=False)
    assert exc_info.value.code == 1
    captured = capsys.readouterr()
    assert "CC-BY-SA 3.0" in captured.out


def test_fasttext_license_non_interactive_prints_decline_message(capsys):
    """Non-interactive mode prints a decline message before raising SystemExit."""
    with pytest.raises(SystemExit):
        _prompt_fasttext_license(non_interactive=True, accept_fasttext_license=False)
    captured = capsys.readouterr()
    assert "Non-interactive" in captured.out or "non-interactive" in captured.out.lower()


def test_fasttext_license_interactive_accept(monkeypatch):
    """Interactive: typing 'accept' returns without raising."""
    monkeypatch.setattr("builtins.input", lambda _: "accept")
    _prompt_fasttext_license(non_interactive=False, accept_fasttext_license=False)  # must not raise


def test_fasttext_license_interactive_decline(monkeypatch):
    """Interactive: typing 'no' raises SystemExit(1)."""
    monkeypatch.setattr("builtins.input", lambda _: "no")
    with pytest.raises(SystemExit) as exc_info:
        _prompt_fasttext_license(non_interactive=False, accept_fasttext_license=False)
    assert exc_info.value.code == 1


def test_fasttext_license_interactive_decline_empty(monkeypatch):
    """Interactive: typing empty string raises SystemExit(1)."""
    monkeypatch.setattr("builtins.input", lambda _: "")
    with pytest.raises(SystemExit) as exc_info:
        _prompt_fasttext_license(non_interactive=False, accept_fasttext_license=False)
    assert exc_info.value.code == 1


def test_fasttext_license_warning_message_content(capsys, monkeypatch):
    """Warning block must mention lid.176.ftz, fasttext, and CC-BY-SA 3.0."""
    monkeypatch.setattr("builtins.input", lambda _: "accept")
    _prompt_fasttext_license(non_interactive=False, accept_fasttext_license=False)
    captured = capsys.readouterr()
    assert "lid.176.ftz" in captured.out
    assert "fasttext" in captured.out.lower()
    assert "CC-BY-SA 3.0" in captured.out


def test_fasttext_license_decline_message(monkeypatch, capsys):
    """Interactive decline prints 'License not accepted. Aborting.' before SystemExit."""
    monkeypatch.setattr("builtins.input", lambda _: "decline")
    with pytest.raises(SystemExit):
        _prompt_fasttext_license(non_interactive=False, accept_fasttext_license=False)
    captured = capsys.readouterr()
    assert "not accepted" in captured.out.lower() or "aborting" in captured.out.lower()
