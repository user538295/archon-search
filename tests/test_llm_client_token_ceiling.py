"""``token_ceiling``: the request-free upper bound of ``count_tokens`` that lets a short query skip the count.

The invariant (documented in ``llm_client/fit.py``, ``protocol.py`` and Architecture 210): a token covers at least one
byte of the UTF-8 text, so a byte-level or byte-fallback tokenizer cannot produce more tokens than the text has bytes.
Two real tokenizers add a little, and each client states its own bound:

* ``llama_cpp``: ``/tokenize`` adds no BOS (``add_special`` defaults to false) but a SentencePiece vocabulary prepends a
  dummy-prefix token, so a text of N bytes can cost N + 1 tokens (``"\u20ac"`` on a Llama 2 vocabulary: the prefix token plus its
  three byte tokens).
* ``ollama``: the estimate counts every digit run as its digits plus one placeholder token, so it can exceed the byte
  length by one per digit run.

The worst case a byte-level tokenizer can show is one token per byte, so the property tests count with exactly that.
"""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from archon_search.llm_client.llama_cpp_client import LlamaCppClient
from archon_search.llm_client.ollama_client import OllamaClient
from archon_search.llm_client.protocol import TokenCountingClient
from archon_search.llm_client.token_counter import TokenCounter
from tests._token_counter_stub import StubEncoding

_SPM_DUMMY_PREFIX_TOKENS = 1
_SURROGATE_PAIR_TEXT = "\ud83d"  # a lone surrogate: a str that cannot be encoded as UTF-8 strictly
_EXAMPLES = [
    "",
    "plain english query about the migration rollout",
    "検索拡張生成 とは何ですか",
    "كيف أقوم بترحيل البيانات؟",
    "emoji 🙂🙃 and a family 👨‍👩‍👧‍👦 and a flag 🇭🇺 and a skin tone 👍🏽",
    "combining e\u0301 and zero-width\u200bspace",
    "1 2 3 4 5 6 7 8 9",
    "id 20260101123456789 and 3.14159 and 0x1F600",
    "\u20ac\u20ac\u20ac",
    "tab\tand\nnewline",
]


def _utf8_length(text: str) -> int:
    return len(text.encode("utf-8", "surrogatepass"))


class _OneTokenPerByte:
    """The worst case for any byte-level or byte-fallback tokenizer: every byte of the text is a token of its own."""

    def encode(self, text: str, add_special_tokens: bool = True) -> StubEncoding:
        return StubEncoding(ids=list(range(_utf8_length(text))))


def _per_byte_llama_server(request: httpx.Request) -> httpx.Response:
    """A llama-server whose tokenizer spends one token per byte and one dummy-prefix token (the SentencePiece worst case)."""
    content = json.loads(request.content)["content"]
    return httpx.Response(200, json={"tokens": list(range(_utf8_length(content) + _SPM_DUMMY_PREFIX_TOKENS))})


def _llama_client() -> LlamaCppClient:
    return LlamaCppClient(model="m", base_url="http://llama.test:8080", transport=httpx.MockTransport(_per_byte_llama_server))


def _counted(client: TokenCountingClient, text: str) -> int:
    """The client's own count; hypothesis drives plain functions, so each property runs its coroutine to the end."""
    return asyncio.run(client.count_tokens(text))


def _ollama_client() -> OllamaClient:
    return OllamaClient(model="m", base_url="http://ollama.test:11434", context_tokens=8192, token_counter=TokenCounter(load_tokenizer=_OneTokenPerByte))


@pytest.mark.parametrize("text", _EXAMPLES)
def test_no_tokenizer_of_the_two_local_clients_exceeds_its_ceiling_on_the_examples(text: str) -> None:
    for client in (_llama_client(), _ollama_client()):
        assert isinstance(client, TokenCountingClient)
        assert _counted(client, text) <= client.token_ceiling(text), (type(client).__name__, text)


@settings(max_examples=300, deadline=None)
@given(st.text())
def test_the_ceiling_holds_for_any_text_with_a_token_per_byte_tokenizer(text: str) -> None:
    for client in (_llama_client(), _ollama_client()):
        assert _counted(client, text) <= client.token_ceiling(text)


@settings(max_examples=200, deadline=None)
@given(st.text(alphabet=st.characters(min_codepoint=0x10000, max_codepoint=0x10FFFF), max_size=40))
def test_the_ceiling_holds_for_text_made_only_of_emoji_and_other_astral_characters(text: str) -> None:
    for client in (_llama_client(), _ollama_client()):
        assert _counted(client, text) <= client.token_ceiling(text)


@settings(max_examples=200, deadline=None)
@given(st.text(alphabet="0123456789 .,-", max_size=60))
def test_the_ceiling_holds_for_digit_heavy_text(text: str) -> None:
    for client in (_llama_client(), _ollama_client()):
        assert _counted(client, text) <= client.token_ceiling(text)


def test_the_llama_ceiling_is_the_byte_length_plus_the_sentencepiece_prefix() -> None:
    client = _llama_client()

    assert client.token_ceiling("") == _SPM_DUMMY_PREFIX_TOKENS
    assert client.token_ceiling("abc") == 3 + _SPM_DUMMY_PREFIX_TOKENS
    assert client.token_ceiling("\u20ac") == 3 + _SPM_DUMMY_PREFIX_TOKENS  # three bytes
    assert client.token_ceiling("🙂") == 4 + _SPM_DUMMY_PREFIX_TOKENS  # four bytes


def test_the_byte_length_alone_is_not_a_bound_for_the_ollama_estimate() -> None:
    """Why the Ollama ceiling is not just the byte length: every digit run costs its digits plus one placeholder token, so
    ``1 2 3`` (five bytes) is estimated at more than five tokens."""
    counter = TokenCounter(load_tokenizer=_OneTokenPerByte)
    text = "1 2 3"

    assert counter.count(text) > _utf8_length(text)
    assert counter.count(text) <= TokenCounter.ceiling(text)


def test_the_ollama_ceiling_adds_one_per_digit_run() -> None:
    assert TokenCounter.ceiling("") == 0
    assert TokenCounter.ceiling("abc") == 3
    assert TokenCounter.ceiling("a1b22c333") == 9 + 3  # nine bytes, three digit runs
    assert TokenCounter.ceiling("🙂") == 4


@pytest.mark.parametrize("client", [_llama_client(), _ollama_client()], ids=["llama_cpp", "ollama"])
def test_a_text_that_cannot_be_encoded_as_utf8_still_has_a_ceiling(client: TokenCountingClient) -> None:
    assert client.token_ceiling(_SURROGATE_PAIR_TEXT) >= 1
