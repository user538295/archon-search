"""BE-3 (S11): the gpt2-based token counter and its pinned tokenizer snapshot."""

from __future__ import annotations

import json
import re
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

import pytest
from tokenizers import Tokenizer
from tokenizers.models import WordLevel
from tokenizers.pre_tokenizers import Whitespace

from archon_search.llm_client.token_counter import TokenCounter, load_gpt2_tokenizer
from archon_search.paths import (
    TOKEN_COUNTER_MODEL_FILES,
    TOKEN_COUNTER_MODEL_NAME,
    TOKEN_COUNTER_MODEL_REVISION,
    TOKEN_COUNTER_TOKENIZER_FILE,
    get_models_dir,
    get_token_counter_models_dir,
)
from tests._token_counter_stub import StubTokenizer

_THREAD_COUNT = 8
_LOAD_DELAY_SECONDS = 0.05
_COMMIT_HASH = re.compile(r"[0-9a-f]{40}")

_IMPORT_PROBE = """\
import json
import sys

import archon_search.llm_client
import archon_search.llm_client.token_counter

print(json.dumps(sorted(m for m in ("tokenizers", "huggingface_hub") if m in sys.modules)))
"""


def test_counter_counts_tokens_with_stub_tokenizer() -> None:
    stub = StubTokenizer()
    counter = TokenCounter(load_tokenizer=lambda: stub)

    assert counter.count("describe this collection in one sentence") == 6
    assert stub.calls == ["describe this collection in one sentence"]


def test_counter_loads_the_tokenizer_once_on_first_count() -> None:
    loads: list[int] = []

    def load() -> StubTokenizer:
        loads.append(1)
        return StubTokenizer()

    counter = TokenCounter(load_tokenizer=load)
    assert loads == [], "constructing the counter must not load the tokenizer"

    counter.count("a b")
    counter.count("c d e")

    assert loads == [1]


def test_failed_load_is_raised_and_not_cached() -> None:
    attempts: list[int] = []

    def flaky_load() -> StubTokenizer:
        attempts.append(1)
        if len(attempts) == 1:
            raise RuntimeError("hub unreachable")
        return StubTokenizer()

    counter = TokenCounter(load_tokenizer=flaky_load)

    with pytest.raises(RuntimeError, match="hub unreachable"):
        counter.count("a b")
    assert counter.count("a b") == 2
    assert attempts == [1, 1]


def test_counter_counts_an_empty_text_as_zero() -> None:
    assert TokenCounter(load_tokenizer=StubTokenizer).count("") == 0


def test_counter_not_imported_at_cli_start() -> None:
    """Importing the client package and its counter module loads neither the tokenizer library nor the hub client."""
    result = subprocess.run(
        [sys.executable, "-c", _IMPORT_PROBE], capture_output=True, text=True, timeout=60, check=False
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == []


def test_pinned_snapshot_has_revision_and_files() -> None:
    assert TOKEN_COUNTER_MODEL_NAME == "openai-community/gpt2"
    assert _COMMIT_HASH.fullmatch(TOKEN_COUNTER_MODEL_REVISION)
    assert TOKEN_COUNTER_MODEL_FILES == (TOKEN_COUNTER_TOKENIZER_FILE,) == ("tokenizer.json",)


def test_token_counter_cache_dir_is_under_the_models_dir() -> None:
    assert get_token_counter_models_dir() == get_models_dir() / "token-counter"


def test_loader_fetches_only_the_pinned_files_and_reads_the_snapshot(tmp_path: Path) -> None:
    tokenizer = Tokenizer(WordLevel({"hello": 0, "world": 1, "[UNK]": 2}, unk_token="[UNK]"))
    tokenizer.pre_tokenizer = Whitespace()
    tokenizer.save(str(tmp_path / TOKEN_COUNTER_TOKENIZER_FILE))

    with patch("archon_search.model_store.ensure_pinned_snapshot", return_value=tmp_path) as ensure:
        loaded = load_gpt2_tokenizer()

    ensure.assert_called_once_with(
        TOKEN_COUNTER_MODEL_NAME,
        TOKEN_COUNTER_MODEL_REVISION,
        TOKEN_COUNTER_MODEL_FILES,
        get_token_counter_models_dir(),
    )
    assert TokenCounter(load_tokenizer=lambda: loaded).count("hello world hello") == 3


@pytest.mark.parametrize("text", ["one two", ""])
def test_counter_does_not_add_special_tokens(text: str) -> None:
    stub = StubTokenizer()

    TokenCounter(load_tokenizer=lambda: stub).count(text)

    assert stub.add_special_tokens_seen == [False]


def test_default_counter_loads_through_the_module_loader_looked_up_at_call_time(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The suite's autouse stub replaces ``load_gpt2_tokenizer``; a default counter must pick the replacement up."""
    stub = StubTokenizer()
    monkeypatch.setattr("archon_search.llm_client.token_counter.load_gpt2_tokenizer", lambda: stub)

    assert TokenCounter().count("three little words") == 3


def test_concurrent_first_counts_load_the_tokenizer_once() -> None:
    thread_count = _THREAD_COUNT
    start = threading.Barrier(thread_count)
    loads: list[int] = []

    def slow_load() -> StubTokenizer:
        loads.append(1)
        time.sleep(_LOAD_DELAY_SECONDS)
        return StubTokenizer()

    counter = TokenCounter(load_tokenizer=slow_load)

    def count() -> int:
        start.wait()
        return counter.count("a b c")

    with ThreadPoolExecutor(max_workers=thread_count) as pool:
        counts = list(pool.map(lambda _: count(), range(thread_count)))

    assert counts == [3] * thread_count
    assert loads == [1]
