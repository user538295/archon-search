"""Frozen specs of the models the PyTorch runtime supports.

Pure data: importing this module must not import ``torch``, ``transformers`` or
``tokenizers`` (the CLI imports it for validation and download sizes). Every
value is frozen from ``tests/fixtures/runtime_parity/model_specs.json``, which
records what the previous runtime produced; a test keeps the two in step.

Names are the registry names users configure (a collection's ``embedding_model``,
``reranker_model``); ``repo`` is the Hugging Face repository holding the
PyTorch weights, pinned to ``revision``. The two differ for the ``snowflake/``
embedders (lower-case registry name, ``Snowflake/`` hub repo) and for the
``Xenova/ms-marco`` rerankers.

Pooling: ``cls`` is the first token, ``mean`` the attention-masked mean,
``last_token`` the hidden state of the last non-padding token (right-padded
batches). No spec carries a query prompt: texts are embedded raw.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Literal

Pooling = Literal["cls", "mean", "last_token"]
Architecture = Literal["bert", "xlm_roberta", "jina_xlm_roberta"]


@dataclass(frozen=True)
class EmbedderSpec:
    """One supported embedder.

    Invariants: ``max_length`` is the truncation length of the frozen outputs;
    ``normalize`` is False for models whose stored vectors are not unit length;
    ``weight_mb`` is the planned download size of ``model.safetensors``.
    """

    name: str
    repo: str
    revision: str
    max_length: int
    pooling: Pooling
    normalize: bool
    dim: int
    weight_mb: float


@dataclass(frozen=True)
class RerankerSpec:
    """One supported cross-encoder reranker (raw logits, no activation)."""

    name: str
    repo: str
    revision: str
    max_length: int
    architecture: Architecture
    uses_token_type_ids: bool
    weight_mb: float


class UnsupportedModelError(ValueError):
    """Raised for a model name outside the registry.

    ``args[0]`` is ``unsupported model <name!r>; supported: <comma list>``.
    """

    def __init__(self, name: str, supported: tuple[str, ...]) -> None:
        super().__init__(f"unsupported model {name!r}; supported: {', '.join(supported)}")
        self.name = name
        self.supported = supported


_BGE_MAX_LENGTH = 512
_MXBAI_MAX_LENGTH = 512
_ARCTIC_MAX_LENGTH = 512
_GTE_MAX_LENGTH = 512
_MINILM_L6_MAX_LENGTH = 256
_MULTILINGUAL_MINILM_MAX_LENGTH = 128
_MPNET_MAX_LENGTH = 512
_E5_MAX_LENGTH = 512
_GRANITE_MAX_LENGTH = 8192
_QWEN3_MAX_LENGTH = 32768
_RERANKER_MAX_LENGTH = 512
_JINA_RERANKER_MAX_LENGTH = 1024
_RERANKER_V2_M3_MAX_LENGTH = 8192


def _embedders(*specs: EmbedderSpec) -> Mapping[str, EmbedderSpec]:
    return MappingProxyType({spec.name: spec for spec in specs})


def _rerankers(*specs: RerankerSpec) -> Mapping[str, RerankerSpec]:
    return MappingProxyType({spec.name: spec for spec in specs})


EMBEDDER_SPECS: Mapping[str, EmbedderSpec] = _embedders(
    EmbedderSpec(
        name="BAAI/bge-small-en-v1.5",
        repo="BAAI/bge-small-en-v1.5",
        revision="5c38ec7c405ec4b44b94cc5a9bb96e735b38267a",
        max_length=_BGE_MAX_LENGTH,
        pooling="cls",
        normalize=True,
        dim=384,
        weight_mb=133.5,
    ),
    EmbedderSpec(
        name="BAAI/bge-base-en-v1.5",
        repo="BAAI/bge-base-en-v1.5",
        revision="a5beb1e3e68b9ab74eb54cfd186867f64f240e1a",
        max_length=_BGE_MAX_LENGTH,
        pooling="cls",
        normalize=True,
        dim=768,
        weight_mb=438.0,
    ),
    EmbedderSpec(
        name="BAAI/bge-large-en-v1.5",
        repo="BAAI/bge-large-en-v1.5",
        revision="d4aa6901d3a41ba39fb536a557fa166f842b0e09",
        max_length=_BGE_MAX_LENGTH,
        pooling="cls",
        normalize=True,
        dim=1024,
        weight_mb=1340.6,
    ),
    EmbedderSpec(
        name="sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2",
        repo="sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2",
        revision="e8f8c211226b894fcb81acc59f3b34ba3efd5f42",
        max_length=_MULTILINGUAL_MINILM_MAX_LENGTH,
        pooling="mean",
        normalize=False,
        dim=384,
        weight_mb=470.6,
    ),
    EmbedderSpec(
        name="sentence-transformers/paraphrase-multilingual-mpnet-base-v2",
        repo="sentence-transformers/paraphrase-multilingual-mpnet-base-v2",
        revision="4328cf26390c98c5e3c738b4460a05b95f4911f5",
        max_length=_MPNET_MAX_LENGTH,
        pooling="mean",
        normalize=False,
        dim=768,
        weight_mb=1112.2,
    ),
    EmbedderSpec(
        name="intfloat/multilingual-e5-large",
        repo="intfloat/multilingual-e5-large",
        revision="3d7cfbdacd47fdda877c5cd8a79fbcc4f2a574f3",
        max_length=_E5_MAX_LENGTH,
        pooling="mean",
        normalize=False,
        dim=1024,
        weight_mb=2239.6,
    ),
    EmbedderSpec(
        name="BAAI/bge-base-en",
        repo="BAAI/bge-base-en",
        revision="b737bf5dcc6ee8bdc530531266b4804a5d77b5d8",
        max_length=_BGE_MAX_LENGTH,
        pooling="cls",
        normalize=True,
        dim=768,
        weight_mb=438.0,
    ),
    EmbedderSpec(
        name="BAAI/bge-small-en",
        repo="BAAI/bge-small-en",
        revision="2275a7bdee235e9b4f01fa73aa60d3311983cfea",
        max_length=_BGE_MAX_LENGTH,
        pooling="cls",
        normalize=True,
        dim=384,
        weight_mb=133.5,
    ),
    EmbedderSpec(
        name="BAAI/bge-small-zh-v1.5",
        repo="BAAI/bge-small-zh-v1.5",
        revision="7999e1d3359715c523056ef9478215996d62a620",
        max_length=_BGE_MAX_LENGTH,
        pooling="cls",
        normalize=True,
        dim=512,
        weight_mb=95.8,
    ),
    EmbedderSpec(
        name="mixedbread-ai/mxbai-embed-large-v1",
        repo="mixedbread-ai/mxbai-embed-large-v1",
        revision="b33106f585b9ce46904ad7443a3b52b7a63e231c",
        max_length=_MXBAI_MAX_LENGTH,
        pooling="cls",
        normalize=True,
        dim=1024,
        weight_mb=670.3,
    ),
    EmbedderSpec(
        name="snowflake/snowflake-arctic-embed-xs",
        repo="Snowflake/snowflake-arctic-embed-xs",
        revision="d8c86521100d3556476a063fc2342036d45c106f",
        max_length=_ARCTIC_MAX_LENGTH,
        pooling="cls",
        normalize=True,
        dim=384,
        weight_mb=90.3,
    ),
    EmbedderSpec(
        name="snowflake/snowflake-arctic-embed-s",
        repo="Snowflake/snowflake-arctic-embed-s",
        revision="e596f507467533e48a2e17c007f0e1dacc837b33",
        max_length=_ARCTIC_MAX_LENGTH,
        pooling="cls",
        normalize=True,
        dim=384,
        weight_mb=132.9,
    ),
    EmbedderSpec(
        name="snowflake/snowflake-arctic-embed-m",
        repo="Snowflake/snowflake-arctic-embed-m",
        revision="fc74610d18462d218e312aa986ec5c8a75a98152",
        max_length=_ARCTIC_MAX_LENGTH,
        pooling="cls",
        normalize=True,
        dim=768,
        weight_mb=435.6,
    ),
    EmbedderSpec(
        name="snowflake/snowflake-arctic-embed-l",
        repo="Snowflake/snowflake-arctic-embed-l",
        revision="d8fb21ca8d905d2832ee8b96c894d3298964346b",
        max_length=_ARCTIC_MAX_LENGTH,
        pooling="cls",
        normalize=True,
        dim=1024,
        weight_mb=1336.4,
    ),
    EmbedderSpec(
        name="sentence-transformers/all-MiniLM-L6-v2",
        repo="sentence-transformers/all-MiniLM-L6-v2",
        revision="1110a243fdf4706b3f48f1d95db1a4f5529b4d41",
        max_length=_MINILM_L6_MAX_LENGTH,
        pooling="mean",
        normalize=True,
        dim=384,
        weight_mb=90.9,
    ),
    EmbedderSpec(
        name="thenlper/gte-base",
        repo="thenlper/gte-base",
        revision="c078288308d8dee004ab72c6191778064285ec0c",
        max_length=_GTE_MAX_LENGTH,
        pooling="mean",
        normalize=True,
        dim=768,
        weight_mb=219.0,
    ),
    EmbedderSpec(
        name="thenlper/gte-large",
        repo="thenlper/gte-large",
        revision="4bef63f39fcc5e2d6b0aae83089f307af4970164",
        max_length=_GTE_MAX_LENGTH,
        pooling="mean",
        normalize=True,
        dim=1024,
        weight_mb=670.3,
    ),
    EmbedderSpec(
        name="ibm-granite/granite-embedding-small-english-r2",
        repo="ibm-granite/granite-embedding-small-english-r2",
        revision="2ab6fa8ea2d674564defd37171ae19079b864b33",
        max_length=_GRANITE_MAX_LENGTH,
        pooling="cls",
        normalize=False,
        dim=384,
        weight_mb=95.3,
    ),
    EmbedderSpec(
        name="Qwen/Qwen3-Embedding-0.6B",
        repo="Qwen/Qwen3-Embedding-0.6B",
        revision="97b0c614be4d77ee51c0cef4e5f07c00f9eb65b3",
        max_length=_QWEN3_MAX_LENGTH,
        pooling="last_token",
        normalize=True,
        dim=1024,
        weight_mb=1191.6,
    ),
)

RERANKER_SPECS: Mapping[str, RerankerSpec] = _rerankers(
    RerankerSpec(
        name="Xenova/ms-marco-MiniLM-L-6-v2",
        repo="cross-encoder/ms-marco-MiniLM-L-6-v2",
        revision="233902d25c440f23af6f7d6e94d2946bac0bee0a",
        max_length=_RERANKER_MAX_LENGTH,
        architecture="bert",
        uses_token_type_ids=True,
        weight_mb=90.9,
    ),
    RerankerSpec(
        name="Xenova/ms-marco-MiniLM-L-12-v2",
        repo="cross-encoder/ms-marco-MiniLM-L-12-v2",
        revision="7b0235231ca2674cb8ca8f022859a6eba2b1c968",
        max_length=_RERANKER_MAX_LENGTH,
        architecture="bert",
        uses_token_type_ids=True,
        weight_mb=133.5,
    ),
    RerankerSpec(
        name="BAAI/bge-reranker-base",
        repo="BAAI/bge-reranker-base",
        revision="2cfc18c9415c912f9d8155881c133215df768a70",
        max_length=_RERANKER_MAX_LENGTH,
        architecture="xlm_roberta",
        uses_token_type_ids=False,
        weight_mb=1112.2,
    ),
    RerankerSpec(
        name="jinaai/jina-reranker-v2-base-multilingual",
        repo="jinaai/jina-reranker-v2-base-multilingual",
        revision="9cfeff2df7d40d1b78e75e5e9cebec92a99813c9",
        max_length=_JINA_RERANKER_MAX_LENGTH,
        architecture="jina_xlm_roberta",
        uses_token_type_ids=False,
        weight_mb=556.9,
    ),
    RerankerSpec(
        name="BAAI/bge-reranker-v2-m3",
        repo="BAAI/bge-reranker-v2-m3",
        revision="953dc6f6f85a1b2dbfca4c34a2796e7dde08d41e",
        max_length=_RERANKER_V2_M3_MAX_LENGTH,
        architecture="xlm_roberta",
        uses_token_type_ids=False,
        weight_mb=2271.1,
    ),
)


def _lookup[SpecT](registry: Mapping[str, SpecT], name: str) -> SpecT:
    try:
        return registry[name]
    except KeyError:
        raise UnsupportedModelError(name, tuple(sorted(registry))) from None


def get_embedder_spec(name: str) -> EmbedderSpec:
    """Return the spec of embedder *name*; raise ``UnsupportedModelError`` if unknown."""
    return _lookup(EMBEDDER_SPECS, name)


def get_reranker_spec(name: str) -> RerankerSpec:
    """Return the spec of reranker *name*; raise ``UnsupportedModelError`` if unknown."""
    return _lookup(RERANKER_SPECS, name)
