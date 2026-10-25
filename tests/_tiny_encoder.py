"""Tiny synthetic BERT / cross-encoder snapshots (random weights, WordLevel tokenizer) for runtime tests.

Lets the embedder, reranker and loader tests run the real PyTorch code path without downloading weights.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING

from archon_search.model_specs import Architecture, EmbedderSpec, Pooling, RerankerSpec

if TYPE_CHECKING:
    import torch

PAD, UNK, CLS, SEP = "[PAD]", "[UNK]", "[CLS]", "[SEP]"
WORDS: tuple[str, ...] = tuple(f"w{i}" for i in range(20))
VOCAB: dict[str, int] = {token: index for index, token in enumerate((PAD, UNK, CLS, SEP, *WORDS))}
HIDDEN_SIZE = 32
TYPE_VOCAB_SIZE = 2
MAX_POSITIONS = 64
SEED = 1234
CROSS_ENCODER_INIT_RANGE = 0.5
#: How long ``OverlapMeter`` keeps one call "in flight"; long enough for racing threads to overlap.
OVERLAP_HOLD_SECONDS = 0.05


def token_ids(text: str) -> list[int]:
    """Ids ``[CLS] <words> [SEP]`` the tiny tokenizer produces for *text*, computed by hand."""
    return [VOCAB[CLS], *(VOCAB[word] for word in text.split()), VOCAB[SEP]]


def _write_tokenizer(directory: Path) -> None:
    """Write the WordLevel ``tokenizer.json``: ``[CLS] a [SEP]`` singles, ``[CLS] a [SEP] b [SEP]`` pairs."""
    from tokenizers import Tokenizer, models, pre_tokenizers, processors

    tokenizer = Tokenizer(models.WordLevel(VOCAB, unk_token=UNK))
    tokenizer.pre_tokenizer = pre_tokenizers.WhitespaceSplit()
    tokenizer.post_processor = processors.TemplateProcessing(
        single=f"{CLS} $A {SEP}",
        pair=f"{CLS} $A {SEP} $B:1 {SEP}:1",
        special_tokens=[(CLS, VOCAB[CLS]), (SEP, VOCAB[SEP])],
    )
    directory.mkdir(parents=True, exist_ok=True)
    tokenizer.save(str(directory / "tokenizer.json"))


def build_snapshot(directory: Path) -> Path:
    """Write ``config.json``, ``model.safetensors`` and ``tokenizer.json`` into *directory*."""
    import torch
    from transformers import BertConfig, BertModel

    _write_tokenizer(directory)
    config = BertConfig(
        vocab_size=len(VOCAB),
        hidden_size=HIDDEN_SIZE,
        num_hidden_layers=1,
        num_attention_heads=2,
        intermediate_size=64,
        max_position_embeddings=MAX_POSITIONS,
        type_vocab_size=TYPE_VOCAB_SIZE,
        pad_token_id=VOCAB[PAD],
    )
    torch.manual_seed(SEED)
    BertModel(config).save_pretrained(directory)
    return directory


def build_cross_encoder_snapshot(directory: Path, architecture: Architecture) -> Path:
    """Write a one-label tiny cross-encoder (``bert`` with token types, or ``xlm_roberta`` without) into *directory*."""
    import torch
    from transformers import (
        BertConfig,
        BertForSequenceClassification,
        XLMRobertaConfig,
        XLMRobertaForSequenceClassification,
    )

    _write_tokenizer(directory)
    shape = {
        "vocab_size": len(VOCAB),
        "hidden_size": HIDDEN_SIZE,
        "num_hidden_layers": 1,
        "num_attention_heads": 2,
        "intermediate_size": 64,
        "pad_token_id": VOCAB[PAD],
        "num_labels": 1,
        "initializer_range": CROSS_ENCODER_INIT_RANGE,  # the 0.02 default gives ~1e-3 logits that hide any input change
    }
    torch.manual_seed(SEED)
    if architecture == "bert":
        config = BertConfig(max_position_embeddings=MAX_POSITIONS, type_vocab_size=TYPE_VOCAB_SIZE, **shape)
        BertForSequenceClassification(config).save_pretrained(directory)
    else:  # RoBERTa-style position ids start at ``pad_token_id + 1``, hence the headroom
        config = XLMRobertaConfig(max_position_embeddings=MAX_POSITIONS + 2, type_vocab_size=1, **shape)
        XLMRobertaForSequenceClassification(config).save_pretrained(directory)
    return directory


class OverlapMeter:
    """A stand-in for a model constructor that records how many calls were in flight at once.

    ``build`` makes the returned model (default: a one-weight ``Linear``). Patch it over a
    ``from_pretrained`` or a model class, run the code under test from several threads, then read
    ``calls`` and ``peak``.
    """

    def __init__(self, build: Callable[..., torch.nn.Module] | None = None) -> None:
        self._build = build
        self._guard = threading.Lock()
        self._in_flight = 0
        self.calls = 0
        self.peak = 0

    def __call__(self, *args: object, **kwargs: object) -> torch.nn.Module:
        import torch

        with self._guard:
            self._in_flight += 1
            self.calls += 1
            self.peak = max(self.peak, self._in_flight)
        time.sleep(OVERLAP_HOLD_SECONDS)
        try:
            return self._build(*args, **kwargs) if self._build is not None else torch.nn.Linear(1, 1)
        finally:
            with self._guard:
                self._in_flight -= 1


def run_concurrently(call: Callable[[], object], threads: int) -> None:
    """Run *call* on *threads* threads started together; re-raise the first exception any of them raised."""
    barrier = threading.Barrier(threads)
    failures: list[BaseException] = []

    def work() -> None:
        barrier.wait()
        try:
            call()
        except BaseException as exc:  # noqa: BLE001 — test helper collects any failure raised by a worker thread for later assertion
            failures.append(exc)

    workers = [threading.Thread(target=work) for _ in range(threads)]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join()
    if failures:
        raise failures[0]


def tiny_spec(*, pooling: Pooling = "cls", normalize: bool = True, max_length: int = MAX_POSITIONS) -> EmbedderSpec:
    """An ``EmbedderSpec`` describing the tiny snapshot."""
    return EmbedderSpec(
        name="tiny/bert",
        repo="tiny/bert",
        revision="0" * 40,
        max_length=max_length,
        pooling=pooling,
        normalize=normalize,
        dim=HIDDEN_SIZE,
        weight_mb=0.0,
    )


def tiny_reranker_spec(
    architecture: Architecture = "bert", *, uses_token_type_ids: bool = True, max_length: int = MAX_POSITIONS
) -> RerankerSpec:
    """A ``RerankerSpec`` describing a tiny cross-encoder snapshot."""
    return RerankerSpec(
        name=f"tiny/{architecture}-reranker",
        repo=f"tiny/{architecture}-reranker",
        revision="0" * 40,
        max_length=max_length,
        architecture=architecture,
        uses_token_type_ids=uses_token_type_ids,
        weight_mb=0.0,
    )
