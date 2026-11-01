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
import os
import re
from collections.abc import Callable
from pathlib import Path

import httpx
import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from archon_search.llm_client.llama_cpp_client import LlamaCppClient
from archon_search.llm_client.ollama_client import OllamaClient
from archon_search.llm_client.protocol import TokenBoundingClient
from archon_search.llm_client.token_counter import TokenCounter
from archon_search.paths import (
    TOKEN_COUNTER_MODEL_NAME,
    TOKEN_COUNTER_MODEL_REVISION,
    TOKEN_COUNTER_TOKENIZER_FILE,
    get_token_counter_models_dir,
)
from tests._token_counter_stub import StubEncoding

_SPM_DUMMY_PREFIX_TOKENS = 1
_SPM_SPECIAL_TOKENS = ("<s>", "</s>", "<unk>")
_CI_ENV = "CI"
_SPM_SPECIAL_SPLIT = re.compile("(" + "|".join(re.escape(token) for token in _SPM_SPECIAL_TOKENS) + ")")
_LONE_SURROGATE = "\ud83d"  # a str that cannot be encoded as UTF-8 strictly
# ``1`` next to an astral character, fullwidth and Arabic-Indic digits (not ASCII digit runs), and spaces.
_ADJACENCY_ALPHABET = "1🙂１２٣٠ "
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


def _spm_token_count(text: str) -> int:
    """The worst case of a SentencePiece vocabulary with special tokens.

    The text is split on the special-token strings; every non-empty piece costs the dummy-prefix token plus at most one
    token per byte, and every special token costs one token. Assumption: a special token's text is at least two bytes
    (``<s>``, ``</s>``, ``<unk>``, ...), so it pays for the prefix its neighbour adds.
    """
    cost = 0
    for piece in _SPM_SPECIAL_SPLIT.split(text):
        if piece in _SPM_SPECIAL_TOKENS:
            cost += 1
        elif piece:
            cost += _SPM_DUMMY_PREFIX_TOKENS + _utf8_length(piece)
    return cost


def _per_byte_llama_server(request: httpx.Request) -> httpx.Response:
    """A llama-server whose tokenizer spends one token per byte and one dummy-prefix token (the SentencePiece worst case)."""
    content = json.loads(request.content)["content"]
    return httpx.Response(200, json={"tokens": list(range(_utf8_length(content) + _SPM_DUMMY_PREFIX_TOKENS))})


def _spm_server(request: httpx.Request) -> httpx.Response:
    """A llama-server whose tokenizer is the :func:`_spm_token_count` model."""
    content = json.loads(request.content)["content"]
    return httpx.Response(200, json={"tokens": list(range(_spm_token_count(content)))})


def _llama_client(handler: Callable[[httpx.Request], httpx.Response] = _per_byte_llama_server) -> LlamaCppClient:
    return LlamaCppClient(model="m", base_url="http://llama.test:8080", transport=httpx.MockTransport(handler))


def _counted(client: TokenBoundingClient, text: str) -> int:
    """The client's own count; hypothesis drives plain functions, so each property runs its coroutine to the end."""
    return asyncio.run(client.count_tokens(text))


def _ollama_client() -> OllamaClient:
    return OllamaClient(model="m", base_url="http://ollama.test:11434", context_tokens=8192, token_counter=TokenCounter(load_tokenizer=_OneTokenPerByte))


@pytest.mark.parametrize("text", _EXAMPLES)
def test_no_tokenizer_of_the_two_local_clients_exceeds_its_ceiling_on_the_examples(text: str) -> None:
    for client in (_llama_client(), _ollama_client()):
        assert isinstance(client, TokenBoundingClient)
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


@pytest.mark.parametrize("lone_surrogates", [1, 2, 5])
def test_a_text_that_cannot_be_encoded_as_utf8_has_a_ceiling_of_three_bytes_per_lone_surrogate(lone_surrogates: int) -> None:
    """``surrogatepass`` encodes a lone surrogate as the three bytes of its (invalid) UTF-8 form."""
    text = _LONE_SURROGATE * lone_surrogates

    assert _llama_client().token_ceiling(text) == 3 * lone_surrogates + _SPM_DUMMY_PREFIX_TOKENS
    assert TokenCounter.ceiling(text) == 3 * lone_surrogates
    # llama_cpp is asked for its ceiling only: a lone surrogate cannot be JSON-encoded onto the wire to be counted.
    assert _counted(_ollama_client(), text) == 3 * lone_surrogates


# --- the ceiling is not looser than the model's worst case on the examples ---------------------------------------------


@pytest.mark.parametrize("text", [text for text in _EXAMPLES if text])
def test_neither_ceiling_is_looser_than_the_worst_case_model_on_the_examples(text: str) -> None:
    """Pins only that the ceiling is not looser than the simulated worst case on these inputs (no special-token string among them)."""
    assert _spm_token_count(text) <= _llama_client().token_ceiling(text)
    assert _counted(_ollama_client(), text) <= TokenCounter.ceiling(text)


def test_the_llama_ceiling_is_reached_by_a_text_without_a_special_token_and_is_not_by_the_empty_text() -> None:
    client = _llama_client(_spm_server)

    assert _counted(client, "a") == 2 == client.token_ceiling("a")
    assert _counted(client, "") == 0 < client.token_ceiling("")


# --- the SentencePiece model: special-token strings split a text, each piece pays its own dummy prefix -------------------


@pytest.mark.parametrize("text", ["<s>a<s>a", "a<s>a", "<s>", "<s><s>", "a</s>b<unk>c", "<s>🙂<s>€<s>", "x<s>", "<s>x"])
def test_the_llama_ceiling_holds_when_special_tokens_split_a_text_into_pieces_that_each_pay_a_prefix(text: str) -> None:
    assert _counted(_llama_client(_spm_server), text) <= _llama_client().token_ceiling(text)


def test_the_simulator_pays_a_prefix_per_piece_and_one_token_per_special_token() -> None:
    assert _spm_token_count("a<s>a") == (1 + 1) + 1 + (1 + 1)
    assert _spm_token_count("<s>") == 1
    assert _spm_token_count("<s>a<s>a") == 1 + 2 + 1 + 2


@settings(max_examples=300, deadline=None)
@given(st.lists(st.sampled_from([*_SPM_SPECIAL_TOKENS, "a", "ab", " ", "é", "🙂", "\u20ac", "1"]), max_size=30).map("".join))
def test_the_llama_ceiling_holds_for_texts_built_from_special_tokens_and_fragments(text: str) -> None:
    assert _counted(_llama_client(_spm_server), text) <= _llama_client().token_ceiling(text)


@settings(max_examples=200, deadline=None)
@given(st.text())
def test_the_llama_ceiling_holds_for_any_text_under_the_sentencepiece_model(text: str) -> None:
    assert _counted(_llama_client(_spm_server), text) <= _llama_client().token_ceiling(text)


# --- adjacency and non-ASCII digits ------------------------------------------------------------------------------------


@settings(max_examples=300, deadline=None)
@given(st.text(alphabet=_ADJACENCY_ALPHABET, max_size=40))
def test_the_ceiling_holds_for_digits_next_to_astral_characters_and_non_ascii_digits(text: str) -> None:
    for client in (_llama_client(), _llama_client(_spm_server), _ollama_client()):
        assert _counted(client, text) <= client.token_ceiling(text)


@pytest.mark.parametrize("text", ["1🙂2", "🙂1🙂", "１２３ 123", "١٢٣4٥", "1 🙂 2 ３"])
def test_the_ollama_ceiling_charges_only_ascii_digit_runs(text: str) -> None:
    ascii_runs = len(re.findall(r"[0-9]+", text))

    assert TokenCounter.ceiling(text) == _utf8_length(text) + ascii_runs
    assert _counted(_ollama_client(), text) <= TokenCounter.ceiling(text)


# --- the real pinned gpt2 vocabulary (never downloaded: skipped locally, a failure on CI, when it is not cached) ---------

_REAL_GPT2_EXAMPLES = [
    *_EXAMPLES,
    "<|endoftext|>",
    "a<|endoftext|>b<|endoftext|><|endoftext|>",
    "1🙂2 ١٢٣ １２３",
    "e\u0301\u0301\u0301 a\u0308 \u0915\u094d\u0937",
    "ǆ ﬃ ß İ ½ ² ① 𝟏𝟐𝟑",
    "x" * 500,
    "9" * 200,
    "\u202e rtl \u200f mark \ufeff bom \x00 nul \x1b esc",
]


def _require_cached_tokenizer(tokenizer_file: Path) -> None:
    """Skip when the pinned tokenizer is not cached, but fail on CI: a skip there would turn the real-vocabulary tests vacuously green."""
    if tokenizer_file.is_file():
        return
    message = f"the pinned gpt2 tokenizer is not cached at {tokenizer_file} (run `archon-search prewarm`)"
    if os.environ.get(_CI_ENV):
        pytest.fail(message, pytrace=False)
    pytest.skip(message)


def test_an_uncached_tokenizer_fails_on_ci(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv(_CI_ENV, "true")

    with pytest.raises(pytest.fail.Exception, match="not cached"):
        _require_cached_tokenizer(tmp_path / "absent" / TOKEN_COUNTER_TOKENIZER_FILE)


def test_an_uncached_tokenizer_skips_outside_ci(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.delenv(_CI_ENV, raising=False)

    with pytest.raises(pytest.skip.Exception, match="not cached"):
        _require_cached_tokenizer(tmp_path / "absent" / TOKEN_COUNTER_TOKENIZER_FILE)


@pytest.fixture(scope="module")
def real_gpt2_counter() -> TokenCounter:
    """A counter over the real pinned gpt2 ``tokenizer.json``, loaded straight from the model cache.

    The autouse fixtures stub ``load_gpt2_tokenizer`` and block the network, so the file is read with
    ``tokenizers.Tokenizer.from_file``; a missing cache skips the test locally and fails it on CI (``CI`` set), where the
    PR workflow prefetches it. A module-scoped fixture runs before the per-test data-dir redirection, so the cache is the
    operator's own.
    """
    snapshot = get_token_counter_models_dir() / f"models--{TOKEN_COUNTER_MODEL_NAME.replace('/', '--')}" / "snapshots" / TOKEN_COUNTER_MODEL_REVISION
    tokenizer_file = snapshot / TOKEN_COUNTER_TOKENIZER_FILE
    _require_cached_tokenizer(tokenizer_file)
    tokenizers = pytest.importorskip("tokenizers")
    tokenizer = tokenizers.Tokenizer.from_file(str(tokenizer_file))
    return TokenCounter(load_tokenizer=lambda: tokenizer)


@pytest.mark.parametrize("text", _REAL_GPT2_EXAMPLES)
def test_the_ollama_ceiling_holds_on_the_real_gpt2_vocabulary_for_the_examples(real_gpt2_counter: TokenCounter, text: str) -> None:
    assert real_gpt2_counter.count(text) <= TokenCounter.ceiling(text)


@settings(max_examples=300, deadline=None)
@given(st.text(alphabet=st.characters(codec="utf-8")))  # a lone surrogate makes the real ``encode`` raise
def test_the_ollama_ceiling_holds_on_the_real_gpt2_vocabulary_for_any_text(real_gpt2_counter: TokenCounter, text: str) -> None:
    assert real_gpt2_counter.count(text) <= TokenCounter.ceiling(text)


@settings(max_examples=300, deadline=None)
@given(st.lists(st.sampled_from(["<|endoftext|>", "1", "🙂", "١", "１", " ", "é", "e\u0301", "中", "a", "99", "\u200d"]), max_size=40).map("".join))
def test_the_ollama_ceiling_holds_on_the_real_gpt2_vocabulary_for_adjacent_digits_emoji_and_special_tokens(
    real_gpt2_counter: TokenCounter, text: str
) -> None:
    assert real_gpt2_counter.count(text) <= TokenCounter.ceiling(text)
