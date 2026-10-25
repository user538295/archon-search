"""Shared yes/no prompt for the install wizard.

One rule for every y/n question the wizard asks: the answer is ``y`` or ``n`` (any case) and
nothing else. Anything else is refused and the question is asked again, so a stray keystroke or a
typo can neither pick a default nor abort the install.
"""
from __future__ import annotations

import contextlib
import sys

try:
    import termios
except ImportError:  # Windows has no termios; its console does not echo-queue type-ahead this way.
    termios = None  # type: ignore[assignment]

INVALID_ANSWER_MESSAGE = "Please answer y or n."


def discard_pending_input() -> None:
    """Drop keystrokes typed while the wizard was busy, so they cannot prefix the next answer.

    The terminal queues what is typed during a long download (Option+arrow is ``ESC b`` /
    ``ESC f``) and ``input()`` would hand it back glued to the answer. A no-op when stdin is
    not a terminal; never raises.
    """
    if termios is None:
        return
    with contextlib.suppress(AttributeError, ValueError, OSError, termios.error):
        if sys.stdin.isatty():
            termios.tcflush(sys.stdin.fileno(), termios.TCIFLUSH)


def ask_yes_no(prompt: str, *, eof_default: bool, eof_message: str | None = None) -> bool:
    """Ask *prompt* until the answer is exactly ``y`` or ``n`` (case-insensitive, trimmed).

    ``eof_default`` is returned, after printing *eof_message* when given, if stdin closes: a
    closed input can never produce a valid answer, so waiting would hang forever.
    """
    while True:
        discard_pending_input()
        try:
            answer = input(prompt).strip().lower()
        except EOFError:
            if eof_message:
                print(eof_message)
            return eof_default
        if answer == "y":
            return True
        if answer == "n":
            return False
        print(INVALID_ANSWER_MESSAGE)
