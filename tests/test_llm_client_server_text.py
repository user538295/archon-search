"""BE-6 (collection-descriptions plan, C1): ``bounded_server_text`` keeps server error text short and free of the prompt."""

from __future__ import annotations

from archon_search.llm_client.server_text import (
    MAX_SERVER_TEXT_CHARS,
    bounded_server_text,
)

_PROMPT = 'say "hi"\nthen stop'


def test_raw_prompt_echo_is_masked() -> None:
    assert bounded_server_text(f"bad input: {_PROMPT}", _PROMPT) == "bad input: [prompt]"


def test_json_escaped_prompt_echo_is_masked() -> None:
    assert bounded_server_text('{"echo": "say \\"hi\\"\\nthen stop"}', _PROMPT) == '{"echo": "[prompt]"}'


def test_empty_prompt_masks_nothing() -> None:
    assert bounded_server_text("plain error", "") == "plain error"


def test_text_is_collapsed_to_one_line() -> None:
    assert bounded_server_text("first\n\n  second\tthird", "") == "first second third"


def test_text_at_the_limit_is_kept_and_one_over_is_cut() -> None:
    at_limit = "x" * MAX_SERVER_TEXT_CHARS

    assert bounded_server_text(at_limit, "") == at_limit
    assert bounded_server_text(at_limit + "y", "") == f"{at_limit}..."


def test_python_repr_prompt_echo_is_masked() -> None:
    """The Anthropic SDK builds its error message from the body's dict repr, which escapes quotes and newlines the Python way."""
    assert bounded_server_text(f"Error code: 400 - {{'message': {_PROMPT!r}}}", _PROMPT) == "Error code: 400 - {'message': '[prompt]'}"
