"""``_privacy.mask_query_echo``: a provider's failure text, with every echo of the query masked, is safe to log.

Shared by HyDE and RAG Fusion (the failure text is the only provider-controlled string that reaches a log line).
"""
from __future__ import annotations

import json

import pytest

from archon_search._privacy import _MIN_CUT_ECHO_CHARS, _echo_forms, mask_query_echo

_CUT = 2000
_MAX_ECHO = 2000  # the most of a query a failure text is masked against (the clients cut a server text at 200)
_QUERY = 'say "hello"\n  to   the café — a longish query'
_CUT_MARKER = "..."
_MIN_FRAGMENT = _MIN_CUT_ECHO_CHARS


def test_masks_the_query_as_sent() -> None:
    assert mask_query_echo(f"bad request: {_QUERY} rejected", _QUERY, cut_chars=_CUT) == "bad request: [query] rejected"


@pytest.mark.parametrize(
    "echo",
    [
        " ".join(_QUERY.split()),
        json.dumps(_QUERY)[1:-1],
        json.dumps(_QUERY, ensure_ascii=False)[1:-1],
        _QUERY.encode("unicode_escape").decode("ascii"),
    ],
    ids=["collapsed", "json-ascii", "json-unicode", "escape-encoded"],
)
def test_masks_every_encoding_a_server_may_echo(echo: str) -> None:
    masked = mask_query_echo(f"server said: {echo}", _QUERY, cut_chars=_CUT)

    assert masked == "server said: [query]"


def test_masks_the_cut_form_of_a_long_query() -> None:
    """The prompt carries only the first ``cut_chars`` of a long query, so that is what a server echoes back."""
    long_query = "x" * (_CUT + 500)

    masked = mask_query_echo(f"echo: {long_query[:_CUT]} end", long_query, cut_chars=_CUT)

    assert masked == "echo: [query] end"


def test_masks_an_echo_cut_off_by_the_text_bound() -> None:
    """The clients cut a server text at a length and append ``...``: the part of the echo that survives is masked."""
    query = "a rather long and unique question about internal deployment keys"
    survivor = query[:40]

    assert mask_query_echo(f"error: {survivor}", query, cut_chars=_CUT) == "error: [query]"
    assert mask_query_echo(f"error: {survivor}{_CUT_MARKER}", query, cut_chars=_CUT) == f"error: [query]{_CUT_MARKER}"


def test_a_short_tail_that_merely_starts_the_query_is_left_alone() -> None:
    """A closing fragment shorter than the bound is indistinguishable from an ordinary word and stays."""
    assert mask_query_echo("connection refused by the", "the quick brown fox jumps over", cut_chars=_CUT) == (
        "connection refused by the"
    )


def test_text_without_the_query_is_unchanged() -> None:
    text = "ollama: connection refused (127.0.0.1:11434)"

    assert mask_query_echo(text, _QUERY, cut_chars=_CUT) == text


def test_an_empty_query_masks_nothing() -> None:
    assert mask_query_echo("some failure", "", cut_chars=_CUT) == "some failure"


def test_masks_an_encoded_echo_that_the_clients_then_whitespace_collapsed() -> None:
    """A server JSON-encodes the query, then the client collapses whitespace: the result matches neither form alone."""
    query = 'quoted "word"   and   caf\u00e9'
    echo = " ".join(json.dumps(query)[1:-1].split())

    assert mask_query_echo(f"error: {echo}", query, cut_chars=_CUT) == "error: [query]"


def test_masks_the_python_repr_form_of_a_query_holding_both_quote_kinds() -> None:
    query = "it's a \"quoted\" question"

    assert mask_query_echo(f"input_value={repr(query)[1:-1]}", query, cut_chars=_CUT) == "input_value=[query]"


def test_only_the_part_of_the_query_the_prompt_carries_can_come_back() -> None:
    """Masking works from the first ``cut_chars`` characters, so a query of any length costs the same."""
    query = "y" * 5_000_000

    assert mask_query_echo(f"echo: {query[:_CUT]}", query, cut_chars=_CUT) == "echo: [query]"


def test_a_query_sent_whole_is_masked_from_a_bounded_prefix_whatever_its_length() -> None:
    """A cloud provider is sent the whole of a query of many megabytes; masking its failure text must not scale with it."""
    query = "confidential " * 400_000  # about 5 MB
    largest_json_expansion = 6  # ``\uXXXX`` for one character

    forms = _echo_forms(query, cut_chars=len(query))

    assert max(len(form) for form in forms) <= _MAX_ECHO * largest_json_expansion
    assert mask_query_echo(f"error: {query[:60]}{_CUT_MARKER}", query, cut_chars=len(query)) == f"error: [query]{_CUT_MARKER}"


@pytest.mark.parametrize(("tail_chars", "masked"), [(_MIN_FRAGMENT - 1, False), (_MIN_FRAGMENT, True)])
def test_the_cut_echo_bound_masks_a_closing_fragment_from_twelve_characters(tail_chars: int, masked: bool) -> None:
    query = "confidential merger plan with Contoso"

    result = mask_query_echo(f"error: {query[:tail_chars]}", query, cut_chars=_CUT)

    assert (result == "error: [query]") is masked


def test_masking_twice_changes_nothing_more() -> None:
    once = mask_query_echo(f"error: {_QUERY}", _QUERY, cut_chars=_CUT)

    assert mask_query_echo(once, _QUERY, cut_chars=_CUT) == once
