"""The wizard's yes/no prompt: strict y/n, re-prompts on anything else, drops type-ahead."""
from __future__ import annotations

import sys
from unittest.mock import MagicMock, patch

import pytest

from archon_search.install import prompts
from archon_search.install.prompts import ask_yes_no

_PROMPT = "Proceed? [y/n]: "


def _ask(answers: list[str], *, eof_default: bool = False) -> tuple[bool, MagicMock]:
    fake_input = MagicMock(side_effect=answers)
    with patch("builtins.input", fake_input):
        return ask_yes_no(_PROMPT, eof_default=eof_default), fake_input


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("y", True), ("n", False), ("Y", True), ("N", False), ("  y  ", True), (" N\t", False)],
)
def test_askYesNo_validAnswer_returnsIt(raw: str, expected: bool) -> None:
    result, fake_input = _ask([raw])
    assert result is expected
    assert fake_input.call_count == 1


@pytest.mark.parametrize("invalid", ["", "   ", "yes", "no", "maybe", "\x1bb\x1bfy", "yn", "1"])
def test_askYesNo_invalidAnswer_isRejectedAndAskedAgain(
    invalid: str, capsys: pytest.CaptureFixture[str]
) -> None:
    result, fake_input = _ask([invalid, "y"])
    assert result is True
    assert fake_input.call_count == 2
    assert "Please answer y or n." in capsys.readouterr().out


def test_askYesNo_manyInvalidAnswers_keepsWaitingForAValidOne() -> None:
    result, fake_input = _ask(["", "x", "yes", "nope", "n"])
    assert result is False
    assert fake_input.call_count == 5


@pytest.mark.parametrize("eof_default", [True, False])
def test_askYesNo_eof_returnsTheCallersDefault(eof_default: bool) -> None:
    fake_input = MagicMock(side_effect=EOFError)
    with patch("builtins.input", fake_input):
        assert ask_yes_no(_PROMPT, eof_default=eof_default) is eof_default
    assert fake_input.call_count == 1


def test_askYesNo_eofAfterInvalidAnswer_returnsTheCallersDefault() -> None:
    fake_input = MagicMock(side_effect=["maybe", EOFError])
    with patch("builtins.input", fake_input):
        assert ask_yes_no(_PROMPT, eof_default=True) is True


def test_askYesNo_eofMessage_isPrintedOnEof(capsys: pytest.CaptureFixture[str]) -> None:
    with patch("builtins.input", MagicMock(side_effect=EOFError)):
        ask_yes_no(_PROMPT, eof_default=False, eof_message="No input received (EOF). Using English.")
    assert "No input received (EOF). Using English." in capsys.readouterr().out


class _FakeTty:
    def isatty(self) -> bool:
        return True

    def fileno(self) -> int:
        return 7


@pytest.mark.skipif(sys.platform == "win32", reason="termios is POSIX-only")
def test_askYesNo_onATerminal_discardsTypeAheadBeforeEachRead() -> None:
    """Keys pressed while the wizard was busy (Option+arrow = ESC b / ESC f) must not
    become the front of the answer."""
    import termios

    events: list[str] = []
    answers = iter(["maybe", "y"])

    def fake_input(_prompt: str) -> str:
        events.append("input")
        return next(answers)

    with patch.object(sys, "stdin", _FakeTty()), \
         patch.object(termios, "tcflush", lambda fd, queue: events.append(f"flush:{fd}:{queue}")), \
         patch("builtins.input", fake_input):
        assert ask_yes_no(_PROMPT, eof_default=False) is True

    assert events == [
        f"flush:7:{termios.TCIFLUSH}", "input", f"flush:7:{termios.TCIFLUSH}", "input"
    ]


def test_askYesNo_notATerminal_readsWithoutFlushing() -> None:
    not_a_tty = MagicMock()
    not_a_tty.isatty.return_value = False
    with patch.object(sys, "stdin", not_a_tty), patch("builtins.input", MagicMock(return_value="n")):
        assert ask_yes_no(_PROMPT, eof_default=True) is False
    not_a_tty.fileno.assert_not_called()


def test_discardPendingInput_flushFailure_isIgnored() -> None:
    """A terminal that refuses the flush must not break the prompt."""
    if sys.platform == "win32":
        pytest.skip("termios is POSIX-only")
    import termios

    with patch.object(sys, "stdin", _FakeTty()), \
         patch.object(termios, "tcflush", MagicMock(side_effect=termios.error("nope"))):
        prompts.discard_pending_input()
