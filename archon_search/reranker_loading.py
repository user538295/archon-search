"""Loading cross-encoder rerankers: stock architectures and the Jina fused-QKV conversion.

``torch`` and friends are imported lazily inside the functions so importing this module
stays cheap. ``trust_remote_code`` is never passed anywhere: the Jina checkpoint is
converted in memory into the stock ``XLMRobertaForSequenceClassification`` layout instead
of running the vendor's modeling code.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import TYPE_CHECKING

from archon_search.torch_runtime import accelerator_lock, disable_weight_progress_bar, load_tokenizer

if TYPE_CHECKING:
    import torch
    from safetensors import safe_open
    from tokenizers import Tokenizer

    from archon_search.model_specs import RerankerSpec

CONFIG_FILE = "config.json"
WEIGHTS_FILE = "model.safetensors"
JINA_ARCHITECTURE = "jina_xlm_roberta"
_XLM_ROBERTA_PAD_TOKEN_ID = 1
_XLM_ROBERTA_BOS_TOKEN_ID = 0
_XLM_ROBERTA_EOS_TOKEN_ID = 2
_FUSED_PROJECTIONS = ("query", "key", "value")

_FUSED_QKV = re.compile(r"^(roberta\.encoder\.)layers(\.\d+\.)mixer\.Wqkv\.(weight|bias)$")
#: Jina checkpoint key pattern -> stock XLM-R key template; keys matching none are kept as they are.
_RENAMES: tuple[tuple[re.Pattern[str], str], ...] = tuple(
    (re.compile(pattern), template)
    for pattern, template in (
        (r"^roberta\.emb_ln\.(.+)$", r"roberta.embeddings.LayerNorm.\1"),
        (r"^roberta\.encoder\.layers\.(\d+)\.mixer\.out_proj\.(.+)$", r"roberta.encoder.layer.\1.attention.output.dense.\2"),
        (r"^roberta\.encoder\.layers\.(\d+)\.norm1\.(.+)$", r"roberta.encoder.layer.\1.attention.output.LayerNorm.\2"),
        (r"^roberta\.encoder\.layers\.(\d+)\.norm2\.(.+)$", r"roberta.encoder.layer.\1.output.LayerNorm.\2"),
        (r"^roberta\.encoder\.layers\.(\d+)\.mlp\.fc1\.(.+)$", r"roberta.encoder.layer.\1.intermediate.dense.\2"),
        (r"^roberta\.encoder\.layers\.(\d+)\.mlp\.fc2\.(.+)$", r"roberta.encoder.layer.\1.output.dense.\2"),
    )
)


def load_cross_encoder(spec: RerankerSpec, path: Path, device: str) -> tuple[Tokenizer, torch.nn.Module]:
    """Load the tokenizer and the fp32 cross-encoder of *spec* from the snapshot at *path*.

    The tokenizer is the raw ``tokenizer.json`` with padding off and ``longest_first``
    truncation at ``spec.max_length`` (callers pad per batch). *device* is an
    already-resolved torch device; the model is returned in eval mode on it and maps a
    token batch to one raw logit per row (``logits[:, 0]``).
    """
    import torch  # noqa: PLC0415 — lazy: keeps module import free of PyTorch
    from transformers import AutoModelForSequenceClassification  # noqa: PLC0415

    disable_weight_progress_bar()
    tokenizer = load_tokenizer(path, spec.max_length)
    with accelerator_lock(device):
        if spec.architecture == JINA_ARCHITECTURE:
            model = load_jina_reranker(path)
        else:
            model = AutoModelForSequenceClassification.from_pretrained(path, dtype=torch.float32)
        model = model.to(device).eval()
    return tokenizer, model


def load_jina_reranker(path: Path) -> torch.nn.Module:
    """Build a stock fp32 ``XLMRobertaForSequenceClassification`` from a Jina reranker checkpoint.

    Reads ``config.json`` and ``model.safetensors`` under *path*. The fused ``mixer.Wqkv``
    tensors (layout ``(three h d)``: rows ``[0:H)`` query, ``[H:2H)`` key, ``[2H:3H)``
    value) are split, the remaining tensors are renamed to the stock layout, and every
    tensor is cast to float32 (exact for the bf16 checkpoint). Layer count and sizes come
    from the config.

    Raises:
        ValueError: if a checkpoint tensor has no place in the model, or a model tensor
            has no source in the checkpoint; the message names the offending keys.
    """
    from safetensors import safe_open  # noqa: PLC0415 — lazy: keeps module import free of PyTorch

    config = json.loads((path / CONFIG_FILE).read_text())
    model = _stock_model(config)
    with safe_open(str(path / WEIGHTS_FILE), "pt") as checkpoint:
        converted, sources = _convert(checkpoint)
    expected = set(model.state_dict())
    if unexpected := sorted(sources[name] for name in converted.keys() - expected):
        raise ValueError(f"checkpoint tensors with no place in the model: {', '.join(unexpected)}")
    if missing := sorted(expected - converted.keys()):
        raise ValueError(f"model tensors missing from the checkpoint: {', '.join(missing)}")
    model.load_state_dict(converted)
    return model


def _stock_model(config: dict[str, object]) -> torch.nn.Module:
    """An untrained single-label stock XLM-R classifier shaped by the Jina *config*."""
    from transformers import XLMRobertaConfig, XLMRobertaForSequenceClassification  # noqa: PLC0415

    return XLMRobertaForSequenceClassification(
        XLMRobertaConfig(
            vocab_size=config["vocab_size"],
            hidden_size=config["hidden_size"],
            num_hidden_layers=config["num_hidden_layers"],
            num_attention_heads=config["num_attention_heads"],
            intermediate_size=config["intermediate_size"],
            max_position_embeddings=config["max_position_embeddings"],
            type_vocab_size=config["type_vocab_size"],
            layer_norm_eps=config["layer_norm_eps"],
            pad_token_id=_XLM_ROBERTA_PAD_TOKEN_ID,
            bos_token_id=_XLM_ROBERTA_BOS_TOKEN_ID,
            eos_token_id=_XLM_ROBERTA_EOS_TOKEN_ID,
            num_labels=1,
        )
    ).float()


def _convert(checkpoint: safe_open) -> tuple[dict[str, torch.Tensor], dict[str, str]]:
    """Map every checkpoint tensor to float32 stock-layout tensors.

    Returns the converted tensors by stock key and, for each, the checkpoint key it came from.
    """
    converted: dict[str, torch.Tensor] = {}
    sources: dict[str, str] = {}
    for key in checkpoint.keys():
        tensor = checkpoint.get_tensor(key).float()
        for name, value in _stock_entries(key, tensor):
            if name in sources:
                raise ValueError(f"checkpoint tensors {sources[name]} and {key} both map to {name}")
            converted[name] = value
            sources[name] = key
    return converted, sources


def _stock_entries(key: str, tensor: torch.Tensor) -> list[tuple[str, torch.Tensor]]:
    """The ``(stock key, tensor)`` entries one checkpoint tensor becomes."""
    fused = _FUSED_QKV.match(key)
    if fused is None:
        return [(_stock_name(key), tensor)]
    if tensor.shape[0] % len(_FUSED_PROJECTIONS):
        raise ValueError(f"{key} has {tensor.shape[0]} rows, not a multiple of {len(_FUSED_PROJECTIONS)}")
    encoder, layer, kind = fused.groups()
    parts = tensor.chunk(len(_FUSED_PROJECTIONS), dim=0)
    return [
        (f"{encoder}layer{layer}attention.self.{projection}.{kind}", part)
        for projection, part in zip(_FUSED_PROJECTIONS, parts, strict=True)
    ]


def _stock_name(key: str) -> str:
    for pattern, template in _RENAMES:
        if pattern.match(key):
            return pattern.sub(template, key)
    return key
