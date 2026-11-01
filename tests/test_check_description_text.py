"""``check_description_text`` (collection-descriptions plan, S61): the one rule for a description's text.

The write operation, the ``PUT`` route's body and the generator's cleaning all answer to it, so what it refuses and what
it lets through is pinned here once.
"""
from __future__ import annotations

import pytest

from archon_search.constants import MAX_DESCRIPTION_CHARS
from archon_search.description_write import check_description_text
from tests._description_text_cases import REJECTED_TEXTS


@pytest.mark.parametrize("text", [pytest.param(text, id=case) for case, text in REJECTED_TEXTS.items()])
def test_the_check_refuses_empty_multiline_control_and_overlong_text(text: str) -> None:
    with pytest.raises(ValueError):
        check_description_text(text)


@pytest.mark.parametrize(
    "text",
    [
        pytest.param("x" * MAX_DESCRIPTION_CHARS, id="exactly-the-limit"),
        pytest.param("  padded on both sides  ", id="surrounding-spaces-kept"),
        pytest.param("Zażółć gęślą jaźń: 日本語のメモ — notes", id="non-ascii-plain-text"),
        pytest.param("family \U0001f468\u200d\U0001f469\u200d\U0001f467 photos", id="zero-width-joiner-inside-text"),
        pytest.param("می\u200cخواهم notes", id="zero-width-non-joiner-inside-text"),
    ],
)
def test_the_check_accepts_one_line_of_plain_text(text: str) -> None:
    check_description_text(text)


@pytest.mark.parametrize(
    "secret",
    [
        pytest.param("private-planning-detail\n" + "x" * MAX_DESCRIPTION_CHARS, id="over-the-limit"),
        pytest.param("private-planning-detail\nsecond line", id="line-break"),
        pytest.param("private-planning-detail\x00", id="control-character"),
    ],
)
def test_the_check_error_never_echoes_the_text(secret: str) -> None:
    with pytest.raises(ValueError) as refused:
        check_description_text(secret)

    assert "private-planning-detail" not in str(refused.value)
