"""``token_ceiling``: the request-free upper bound of ``count_tokens`` that lets a short query skip the count.

The invariant (documented in ``llm_client/fit.py``, ``protocol.py`` and Architecture 210): a token covers at least one
byte of the UTF-8 text, so a byte-level or byte-fallback tokenizer cannot produce more tokens than the text has bytes.
Two real tokenizers add a little, and each client states its own bound:

* ``llama_cpp``: ``/tokenize`` adds no BOS (``add_special`` defaults to false) but a SentencePiece vocabulary prepends a
  dummy-prefix token, so a text of N bytes can cost N + 1 tokens (``"\u20ac"`` on a Llama 2 vocabulary: the prefix token plus its
  three byte tokens).
* ``ollama``: the estimate counts every digit run as its digits plus one placeholder token, so it can exceed the byte
  length by one per digit run; the gpt2 special-token literal ``<|endoftext|>`` is charged its bytes, not one token.

The worst case a byte-level tokenizer can show is one token per byte, so the property tests count with exactly that.
"""

from __future__ import annotations

import asyncio
import importlib
import json
import os
import re
import time
from collections.abc import Callable
from pathlib import Path
from types import ModuleType
from typing import Any

import httpx
import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from archon_search.hyde import HYDE_MAX_OUTPUT_TOKENS, HYDE_PROMPT_TEMPLATE
from archon_search.llm_client.fit import fit_query_to_window
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
_TOKENIZERS_MODULE = "tokenizers"
_ABSENT_MODULE = "archon_search_absent_tokenizers_stand_in"
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


def _import_tokenizers(module_name: str = _TOKENIZERS_MODULE) -> ModuleType:
    """Import ``tokenizers``; skip when it is missing locally, but raise on CI: a skip there would turn the real-vocabulary tests vacuously green."""
    if os.environ.get(_CI_ENV):
        return importlib.import_module(module_name)
    return pytest.importorskip(module_name)


def test_a_missing_tokenizers_package_fails_on_ci(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(_CI_ENV, "true")

    with pytest.raises(ImportError, match=_ABSENT_MODULE):
        _import_tokenizers(_ABSENT_MODULE)


def test_a_missing_tokenizers_package_skips_outside_ci(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(_CI_ENV, raising=False)

    with pytest.raises(pytest.skip.Exception, match=_ABSENT_MODULE):
        _import_tokenizers(_ABSENT_MODULE)


@pytest.mark.parametrize("on_ci", [True, False])
def test_an_installed_tokenizers_package_is_imported_on_and_off_ci(monkeypatch: pytest.MonkeyPatch, on_ci: bool) -> None:
    if on_ci:
        monkeypatch.setenv(_CI_ENV, "true")
    else:
        monkeypatch.delenv(_CI_ENV, raising=False)

    assert _import_tokenizers().__name__ == _TOKENIZERS_MODULE


@pytest.fixture(scope="module")
def pinned_tokenizer_file() -> Path:
    """The cached pinned gpt2 ``tokenizer.json``; a missing cache skips the test locally and fails it on CI (``CI`` set).

    Every lane that runs the default suite prefetches the tokenizer (the PR and release workflows,
    ``scripts/test-unlocked-deps.sh``). A module-scoped fixture runs before the per-test data-dir redirection, so the
    cache is the operator's own.
    """
    snapshot = get_token_counter_models_dir() / f"models--{TOKEN_COUNTER_MODEL_NAME.replace('/', '--')}" / "snapshots" / TOKEN_COUNTER_MODEL_REVISION
    tokenizer_file = snapshot / TOKEN_COUNTER_TOKENIZER_FILE
    _require_cached_tokenizer(tokenizer_file)
    return tokenizer_file


@pytest.fixture(scope="module")
def real_gpt2_counter(pinned_tokenizer_file: Path) -> TokenCounter:
    """A counter over the real pinned gpt2 ``tokenizer.json``, loaded straight from the model cache.

    The autouse fixtures stub ``load_gpt2_tokenizer`` and block the network, so the file is read with
    ``tokenizers.Tokenizer.from_file``; a missing ``tokenizers`` package skips the test locally and fails it on CI.
    """
    tokenizers = _import_tokenizers()
    tokenizer = tokenizers.Tokenizer.from_file(str(pinned_tokenizer_file))
    return TokenCounter(load_tokenizer=lambda: tokenizer)


@pytest.mark.parametrize("text", _REAL_GPT2_EXAMPLES)
def test_the_ollama_ceiling_holds_on_the_real_gpt2_vocabulary_for_the_examples(real_gpt2_counter: TokenCounter, text: str) -> None:
    assert real_gpt2_counter.count(text) <= TokenCounter.ceiling(text)


@settings(max_examples=300, deadline=None)
@given(st.text(alphabet=st.characters(codec="utf-8")))  # lone surrogates have their own tests below
def test_the_ollama_ceiling_holds_on_the_real_gpt2_vocabulary_for_any_text(real_gpt2_counter: TokenCounter, text: str) -> None:
    assert real_gpt2_counter.count(text) <= TokenCounter.ceiling(text)


@settings(max_examples=300, deadline=None)
@given(st.lists(st.sampled_from(["<|endoftext|>", "1", "🙂", "١", "１", " ", "é", "e\u0301", "中", "a", "99", "\u200d"]), max_size=40).map("".join))
def test_the_ollama_ceiling_holds_on_the_real_gpt2_vocabulary_for_adjacent_digits_emoji_and_special_tokens(
    real_gpt2_counter: TokenCounter, text: str
) -> None:
    assert real_gpt2_counter.count(text) <= TokenCounter.ceiling(text)


_SPECIAL_TOKEN_LITERAL = "<|endoftext|>"
_LITERAL_COUNT = 20
_OVERLONG_LITERAL_COUNT = 10_000  # 130 000 characters: far more than any window holds


def test_the_pinned_gpt2_vocabulary_has_no_added_token_but_the_literal_the_counter_charges_by_its_bytes(pinned_tokenizer_file: Path) -> None:
    """``token_counter`` removes this one literal from the text; a second added token in the pinned vocabulary would be counted as one."""
    tokenizers = _import_tokenizers()
    tokenizer = tokenizers.Tokenizer.from_file(str(pinned_tokenizer_file))

    assert [token.content for token in tokenizer.get_added_tokens_decoder().values()] == [_SPECIAL_TOKEN_LITERAL]


def test_the_ollama_estimate_of_special_token_literals_is_not_below_a_tokenizer_that_splits_them(
    real_gpt2_counter: TokenCounter, pinned_tokenizer_file: Path
) -> None:
    """The literal is one token to the pinned gpt2 vocabulary but a run of plain BPE pieces to a Llama or Qwen one.

    The stand-in for the splitting model is the same vocabulary with its added token removed (its byte-level BPE of
    the characters): 7 tokens per literal. The estimate must not fall below that, or the budget lets a prompt through
    that overruns the window.
    """
    tokenizers = _import_tokenizers()
    data = json.loads(pinned_tokenizer_file.read_text(encoding="utf-8"))
    data["added_tokens"] = []
    splitting = tokenizers.Tokenizer.from_str(json.dumps(data))
    text = _SPECIAL_TOKEN_LITERAL * _LITERAL_COUNT
    splitting_count = len(splitting.encode(text, add_special_tokens=False).ids)

    assert splitting_count > _LITERAL_COUNT, "the stand-in reads the literal as more than one token"
    assert real_gpt2_counter.count(text) >= splitting_count


@pytest.mark.asyncio
async def test_a_query_of_special_token_literals_is_cut_to_what_a_splitting_tokenizer_still_fits_in_the_window(
    real_gpt2_counter: TokenCounter, pinned_tokenizer_file: Path
) -> None:
    """The sizing step end to end: the query head ``fit_query_to_window`` sends, read by a vocabulary that splits the
    literal, plus the template and the HyDE answer ceiling, stays inside the window (the 4096 an unset ``ollama_context_tokens``
    assumes)."""
    tokenizers = _import_tokenizers()
    data = json.loads(pinned_tokenizer_file.read_text(encoding="utf-8"))
    data["added_tokens"] = []
    splitting = tokenizers.Tokenizer.from_str(json.dumps(data))
    client = OllamaClient(model="m", base_url="http://ollama.test:11434", context_tokens=None, token_counter=real_gpt2_counter)
    window = client.context_window().size
    template = HYDE_PROMPT_TEMPLATE.format(query="")

    sent = await fit_query_to_window(
        client,
        template,
        _SPECIAL_TOKEN_LITERAL * _OVERLONG_LITERAL_COUNT,
        provider="ollama",
        max_output_tokens=HYDE_MAX_OUTPUT_TOKENS,
    )

    prompt_tokens = len(splitting.encode(HYDE_PROMPT_TEMPLATE.format(query=sent), add_special_tokens=False).ids)
    assert sent, "the window has room for part of the query"
    assert prompt_tokens + HYDE_MAX_OUTPUT_TOKENS <= window


# --- a special-token literal re-formed by removing another, and lone surrogates ------------------------------------------

_NESTED_LITERAL = "<|endof<|endoftext|>text|>"  # removing the inner literal joins the halves into a second one
_LITERAL_FRAGMENTS = ("<|endoftext|>", "<|", "|>", "<|endof", "text|>", "end", "of", "text", "<", ">", "|", "a", "🙂", "1", "\ud83d", "\ude00")
_DEEP_NESTING = 100_000  # 1.3 million characters, each literal inside the next
_DEEP_NESTING_BOUND_SECONDS = 10.0  # a quadratic removal takes minutes here, the single pass well under a second
_LONE_SURROGATE_BYTES = 3  # what ``surrogatepass`` encodes one as
_LONE_SURROGATES = "\ud83d\ude00"
_DROP_SURROGATES = str.maketrans("", "", _LONE_SURROGATES)


class _LiteralIsOneToken:
    """The pinned gpt2 reading of a text: a byte per token, except that each ``<|endoftext|>`` it finds is a single token."""

    def encode(self, text: str, add_special_tokens: bool = True) -> StubEncoding:
        return StubEncoding(ids=list(range(_utf8_length(text) - (len(_SPECIAL_TOKEN_LITERAL) - 1) * text.count(_SPECIAL_TOKEN_LITERAL))))


def _literal_stub_counter() -> TokenCounter:
    return TokenCounter(load_tokenizer=_LiteralIsOneToken)


def test_a_literal_re_formed_by_removing_another_is_still_charged_by_its_bytes() -> None:
    counter = _literal_stub_counter()

    assert counter.count(_NESTED_LITERAL) == len(_NESTED_LITERAL)  # two literals of 13 bytes, nothing left for the tokenizer
    assert counter.count(_NESTED_LITERAL) <= TokenCounter.ceiling(_NESTED_LITERAL)


def test_a_literal_re_formed_by_removing_another_is_charged_by_its_bytes_on_the_real_gpt2_vocabulary(real_gpt2_counter: TokenCounter) -> None:
    assert real_gpt2_counter.count(_NESTED_LITERAL) == len(_NESTED_LITERAL)


@settings(max_examples=500, deadline=None)
@given(st.lists(st.sampled_from(_LITERAL_FRAGMENTS), max_size=30).map("".join))
def test_the_stub_estimate_of_texts_built_from_fragments_of_the_special_token_literal_reaches_the_ceiling(text: str) -> None:
    """A byte-per-token tokenizer that reads each literal it finds as one token: with every literal removed first, the
    estimate is exactly the ceiling, so a literal that survives the removal (counted as 1) would show as a shortfall."""
    assert _literal_stub_counter().count(text) == TokenCounter.ceiling(text)


@pytest.fixture(scope="module")
def splitting_gpt2_tokenizer(pinned_tokenizer_file: Path) -> Any:
    """The pinned vocabulary without its added token: how a Llama or Qwen byte-level BPE reads ``<|endoftext|>`` (as plain pieces)."""
    tokenizers = _import_tokenizers()
    data = json.loads(pinned_tokenizer_file.read_text(encoding="utf-8"))
    data["added_tokens"] = []
    return tokenizers.Tokenizer.from_str(json.dumps(data))


@settings(max_examples=500, deadline=None)
@given(st.lists(st.sampled_from(_LITERAL_FRAGMENTS), max_size=30).map("".join))
def test_the_real_gpt2_estimate_of_texts_built_from_fragments_of_the_special_token_literal_is_between_a_splitting_tokenizer_and_the_ceiling(
    real_gpt2_counter: TokenCounter, splitting_gpt2_tokenizer: Any, text: str
) -> None:
    surrogates = sum(character in _LONE_SURROGATES for character in text)
    readable = text.translate(_DROP_SURROGATES)  # the splitting stand-in refuses a lone surrogate too
    splitting_count = len(splitting_gpt2_tokenizer.encode(readable, add_special_tokens=False).ids) + _LONE_SURROGATE_BYTES * surrogates

    assert splitting_count <= real_gpt2_counter.count(text) <= TokenCounter.ceiling(text)


def test_literals_nested_a_hundred_thousand_deep_are_all_charged_in_linear_time() -> None:
    text = "<|endof" * _DEEP_NESTING + _SPECIAL_TOKEN_LITERAL + "text|>" * _DEEP_NESTING
    counter = _literal_stub_counter()
    started = time.perf_counter()

    counted = counter.count(text)

    assert time.perf_counter() - started < _DEEP_NESTING_BOUND_SECONDS
    assert counted == len(text)  # every character belongs to one of the nested literals


def test_a_lone_surrogate_is_charged_the_bytes_of_its_utf8_form_on_the_real_gpt2_vocabulary(real_gpt2_counter: TokenCounter) -> None:
    """A prompt can hold one (a bad document); the tokenizer refuses it, so the counter must not pass it on."""
    assert real_gpt2_counter.count(_LONE_SURROGATE) == _LONE_SURROGATE_BYTES
    assert real_gpt2_counter.count("before \ud83d after \ude00") == real_gpt2_counter.count("before  after ") + 2 * _LONE_SURROGATE_BYTES


def test_a_lone_surrogate_between_the_halves_of_a_literal_does_not_hide_it() -> None:
    text = "<|endof\ud83dtext|>"
    counter = _literal_stub_counter()

    assert counter.count(text) == len(_SPECIAL_TOKEN_LITERAL) + _LONE_SURROGATE_BYTES
    assert counter.count(text) <= TokenCounter.ceiling(text)
