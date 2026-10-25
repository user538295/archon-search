"""Unit tests for the cross-encoder loaders: stock architectures and the Jina fused-QKV conversion.

Everything runs on tiny synthetic checkpoints (no weights, no network).
"""
from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path
from typing import Any
from unittest.mock import patch

import _tiny_encoder as tiny_encoder
import pytest
import torch
from safetensors import safe_open
from safetensors.torch import save_file

from archon_search.model_specs import RerankerSpec
from archon_search.reranker_loading import load_cross_encoder, load_jina_reranker

pytestmark = pytest.mark.real_torch_backends

_TOLERANCE = 1e-5

_HIDDEN = 16
_HEADS = 2
_LAYERS = 2
_INTERMEDIATE = 32
_VOCAB = 30
_MAX_POSITIONS = 40
_EPS = 1e-5
_PAD_ID = 1
_LAYER_PREFIX = "roberta.encoder.layers"
_NATIVE_LAYER_PREFIX = "roberta.encoder.layer"

#: Jina checkpoint suffix -> stock XLM-R suffix, for every non-fused per-layer tensor.
_LAYER_RENAMES = {
    "mixer.out_proj": "attention.output.dense",
    "norm1": "attention.output.LayerNorm",
    "norm2": "output.LayerNorm",
    "mlp.fc1": "intermediate.dense",
    "mlp.fc2": "output.dense",
}
_TOP_RENAMES = {
    "roberta.emb_ln.weight": "roberta.embeddings.LayerNorm.weight",
    "roberta.emb_ln.bias": "roberta.embeddings.LayerNorm.bias",
}


def _jina_spec() -> RerankerSpec:
    return tiny_encoder.tiny_reranker_spec("jina_xlm_roberta", uses_token_type_ids=False)


def _random(generator: torch.Generator, *shape: int) -> torch.Tensor:
    """A random tensor exactly representable in bf16 (the checkpoint dtype), returned as float32."""
    return torch.randn(*shape, generator=generator).to(torch.bfloat16).float()


def _fused_tensors(layers: int = _LAYERS) -> dict[str, torch.Tensor]:
    """A random checkpoint in the Jina key layout, float32 values that bf16 holds exactly."""
    generator = torch.Generator().manual_seed(7)

    def linear(name: str, out: int, inp: int) -> dict[str, torch.Tensor]:
        return {f"{name}.weight": _random(generator, out, inp) * 0.2, f"{name}.bias": _random(generator, out) * 0.1}

    def norm(name: str) -> dict[str, torch.Tensor]:
        return {f"{name}.weight": 1 + _random(generator, _HIDDEN) * 0.1, f"{name}.bias": _random(generator, _HIDDEN) * 0.1}

    tensors: dict[str, torch.Tensor] = {
        "roberta.embeddings.word_embeddings.weight": _random(generator, _VOCAB, _HIDDEN),
        "roberta.embeddings.position_embeddings.weight": _random(generator, _MAX_POSITIONS, _HIDDEN),
        "roberta.embeddings.token_type_embeddings.weight": _random(generator, 1, _HIDDEN),
        **norm("roberta.emb_ln"),
        **linear("classifier.dense", _HIDDEN, _HIDDEN),
        **linear("classifier.out_proj", 1, _HIDDEN),
    }
    for layer in range(layers):
        base = f"{_LAYER_PREFIX}.{layer}"
        tensors |= linear(f"{base}.mixer.Wqkv", 3 * _HIDDEN, _HIDDEN)
        tensors |= linear(f"{base}.mixer.out_proj", _HIDDEN, _HIDDEN)
        tensors |= norm(f"{base}.norm1") | norm(f"{base}.norm2")
        tensors |= linear(f"{base}.mlp.fc1", _INTERMEDIATE, _HIDDEN)
        tensors |= linear(f"{base}.mlp.fc2", _HIDDEN, _INTERMEDIATE)
    return {name: value.to(torch.bfloat16).float() for name, value in tensors.items()}


def _write_checkpoint(directory: Path, tensors: dict[str, torch.Tensor], *, layers: int = _LAYERS) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    config = {
        "architectures": ["XLMRobertaForSequenceClassification"],
        "auto_map": {"AutoModelForSequenceClassification": "modeling_xlm_roberta.XLMRobertaForSequenceClassification"},
        "hidden_size": _HIDDEN,
        "num_hidden_layers": layers,
        "num_attention_heads": _HEADS,
        "intermediate_size": _INTERMEDIATE,
        "vocab_size": _VOCAB,
        "max_position_embeddings": _MAX_POSITIONS,
        "type_vocab_size": 1,
        "layer_norm_eps": _EPS,
    }
    (directory / "config.json").write_text(json.dumps(config))
    save_file({name: value.to(torch.bfloat16).contiguous() for name, value in tensors.items()}, str(directory / "model.safetensors"))
    return directory


@pytest.fixture
def fused() -> dict[str, torch.Tensor]:
    return _fused_tensors()


@pytest.fixture
def fused_checkpoint(tmp_path: Path, fused: dict[str, torch.Tensor]) -> Path:
    return _write_checkpoint(tmp_path / "jina", fused)


def _fused_logits(tensors: dict[str, torch.Tensor], token_ids: list[int]) -> torch.Tensor:
    """Independent forward pass of the fused checkpoint, written with plain torch ops."""
    ids = torch.tensor(token_ids)
    # RoBERTa position ids: running count of non-pad tokens, offset by the pad id.
    positions = torch.arange(len(token_ids)) + 1 + _PAD_ID
    layer_norm = torch.nn.functional.layer_norm
    x = (
        tensors["roberta.embeddings.word_embeddings.weight"][ids]
        + tensors["roberta.embeddings.position_embeddings.weight"][positions]
        + tensors["roberta.embeddings.token_type_embeddings.weight"][0]
    )
    x = layer_norm(x, (_HIDDEN,), tensors["roberta.emb_ln.weight"], tensors["roberta.emb_ln.bias"], _EPS)
    head_size = _HIDDEN // _HEADS
    for layer in range(_LAYERS):
        p = f"{_LAYER_PREFIX}.{layer}."
        qkv = x @ tensors[p + "mixer.Wqkv.weight"].T + tensors[p + "mixer.Wqkv.bias"]
        qkv = qkv.reshape(len(token_ids), 3, _HEADS, head_size)  # layout (three h d)
        q, k, v = (qkv[:, i].transpose(0, 1) for i in range(3))  # (heads, tokens, head_size)
        attention = torch.softmax(q @ k.transpose(1, 2) / head_size**0.5, dim=-1) @ v
        attention = attention.transpose(0, 1).reshape(len(token_ids), _HIDDEN)
        attention = attention @ tensors[p + "mixer.out_proj.weight"].T + tensors[p + "mixer.out_proj.bias"]
        x = layer_norm(x + attention, (_HIDDEN,), tensors[p + "norm1.weight"], tensors[p + "norm1.bias"], _EPS)
        hidden = torch.nn.functional.gelu(x @ tensors[p + "mlp.fc1.weight"].T + tensors[p + "mlp.fc1.bias"])
        hidden = hidden @ tensors[p + "mlp.fc2.weight"].T + tensors[p + "mlp.fc2.bias"]
        x = layer_norm(x + hidden, (_HIDDEN,), tensors[p + "norm2.weight"], tensors[p + "norm2.bias"], _EPS)
    pooled = torch.tanh(x[0] @ tensors["classifier.dense.weight"].T + tensors["classifier.dense.bias"])
    return pooled @ tensors["classifier.out_proj.weight"].T + tensors["classifier.out_proj.bias"]


def test_converted_tiny_model_matches_fused_reference(fused_checkpoint: Path, fused: dict[str, torch.Tensor]) -> None:
    token_ids = [0, 5, 9, 12, 7, 20, 2]
    model = load_jina_reranker(fused_checkpoint).eval()

    with torch.inference_mode():
        converted = model(input_ids=torch.tensor([token_ids])).logits[0]

    expected = _fused_logits(fused, token_ids)
    assert expected.abs().min() > 1e-3  # a degenerate near-zero logit would make the comparison vacuous
    torch.testing.assert_close(converted, expected, atol=_TOLERANCE, rtol=0)


def test_wqkv_split_gives_equal_thirds_in_query_key_value_order(tmp_path: Path, fused: dict[str, torch.Tensor]) -> None:
    for layer in range(_LAYERS):
        base = f"{_LAYER_PREFIX}.{layer}.mixer.Wqkv"
        fused[f"{base}.weight"] = torch.cat([torch.full((_HIDDEN, _HIDDEN), float(part)) for part in (1, 2, 4)])
        fused[f"{base}.bias"] = torch.cat([torch.full((_HIDDEN,), float(part)) for part in (8, 16, 32)])
    model = load_jina_reranker(_write_checkpoint(tmp_path / "wqkv", fused))

    for layer in model.roberta.encoder.layer:
        attention = layer.attention.self
        for projection, part in ((attention.query, 1), (attention.key, 2), (attention.value, 4)):
            assert projection.weight.shape == (_HIDDEN, _HIDDEN)
            assert torch.equal(projection.weight, torch.full((_HIDDEN, _HIDDEN), float(part)))
            assert torch.equal(projection.bias, torch.full((_HIDDEN,), float(part * 8)))


def test_every_source_key_is_consumed_exactly_once(fused_checkpoint: Path, fused: dict[str, torch.Tensor]) -> None:
    state = load_jina_reranker(fused_checkpoint).state_dict()

    expected: dict[str, torch.Tensor] = {}
    for name, tensor in fused.items():
        if ".mixer.Wqkv." in name:
            continue
        native = _TOP_RENAMES.get(name, name)
        if name.startswith(_LAYER_PREFIX):
            index, _, remainder = name.removeprefix(f"{_LAYER_PREFIX}.").partition(".")
            module, _, kind = remainder.rpartition(".")
            native = f"{_NATIVE_LAYER_PREFIX}.{index}.{_LAYER_RENAMES[module]}.{kind}"
        expected[native] = tensor
    for layer in range(_LAYERS):
        for part, projection in enumerate(("query", "key", "value")):
            for kind in ("weight", "bias"):
                fused_rows = fused[f"{_LAYER_PREFIX}.{layer}.mixer.Wqkv.{kind}"].chunk(3)[part]
                expected[f"{_NATIVE_LAYER_PREFIX}.{layer}.attention.self.{projection}.{kind}"] = fused_rows

    assert set(state) == set(expected)
    for name, tensor in expected.items():
        assert torch.equal(state[name], tensor), name


def test_output_dtype_is_float32_although_the_checkpoint_is_bf16(fused_checkpoint: Path) -> None:
    with safe_open(str(fused_checkpoint / "model.safetensors"), "pt") as stored:
        assert {stored.get_tensor(key).dtype for key in list(stored.keys())} == {torch.bfloat16}

    model = load_jina_reranker(fused_checkpoint)

    assert {parameter.dtype for parameter in model.parameters()} == {torch.float32}


def test_model_shape_follows_the_config_not_constants(tmp_path: Path) -> None:
    model = load_jina_reranker(_write_checkpoint(tmp_path / "one-layer", _fused_tensors(layers=1), layers=1))

    assert len(model.roberta.encoder.layer) == 1
    assert model.config.hidden_size == _HIDDEN
    assert model.config.num_labels == 1
    assert model.config.pad_token_id == _PAD_ID


def test_missing_tensor_raises_value_error_naming_it(tmp_path: Path, fused: dict[str, torch.Tensor]) -> None:
    del fused[f"{_LAYER_PREFIX}.1.mlp.fc1.bias"]
    checkpoint = _write_checkpoint(tmp_path / "missing", fused)

    with pytest.raises(ValueError, match=r"layer\.1\.intermediate\.dense\.bias"):
        load_jina_reranker(checkpoint)


def test_missing_fused_qkv_raises_value_error_naming_the_projections(tmp_path: Path, fused: dict[str, torch.Tensor]) -> None:
    del fused[f"{_LAYER_PREFIX}.0.mixer.Wqkv.weight"]
    checkpoint = _write_checkpoint(tmp_path / "no-qkv", fused)

    with pytest.raises(ValueError, match=r"layer\.0\.attention\.self\.query\.weight"):
        load_jina_reranker(checkpoint)


def test_unexpected_tensor_raises_value_error_naming_it(tmp_path: Path, fused: dict[str, torch.Tensor]) -> None:
    fused[f"{_LAYER_PREFIX}.0.mixer.rotary_emb.inv_freq"] = torch.zeros(4)
    checkpoint = _write_checkpoint(tmp_path / "extra", fused)

    with pytest.raises(ValueError, match=r"mixer\.rotary_emb\.inv_freq"):
        load_jina_reranker(checkpoint)


def test_extra_layer_in_the_checkpoint_raises_value_error_naming_it(tmp_path: Path) -> None:
    checkpoint = _write_checkpoint(tmp_path / "extra-layer", _fused_tensors(layers=_LAYERS), layers=_LAYERS - 1)

    with pytest.raises(ValueError, match=rf"{_LAYER_PREFIX}\.{_LAYERS - 1}\."):
        load_jina_reranker(checkpoint)


def test_conversion_needs_neither_einops_nor_remote_code(
    fused_checkpoint: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setitem(sys.modules, "einops", None)  # any ``import einops`` now raises ImportError
    monkeypatch.setattr(
        "transformers.dynamic_module_utils.get_class_from_dynamic_module",
        lambda *args, **kwargs: pytest.fail("remote code was requested"),
    )

    model = load_jina_reranker(fused_checkpoint)  # config.json carries ``auto_map``, as the real checkpoint does

    assert type(model).__module__.startswith("transformers.models.xlm_roberta")


# ---------------------------------------------------------------------------
# load_cross_encoder
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def bert_snapshot(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return tiny_encoder.build_cross_encoder_snapshot(tmp_path_factory.mktemp("tiny-bert-ce"), "bert")


@pytest.fixture(scope="module")
def xlm_snapshot(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return tiny_encoder.build_cross_encoder_snapshot(tmp_path_factory.mktemp("tiny-xlmr-ce"), "xlm_roberta")


def test_loads_a_tiny_bert_cross_encoder_in_fp32_eval_mode(bert_snapshot: Path) -> None:
    spec = tiny_encoder.tiny_reranker_spec("bert")

    _, model = load_cross_encoder(spec, bert_snapshot, "cpu")

    assert type(model).__name__ == "BertForSequenceClassification"
    assert model.config.num_labels == 1
    assert not model.training
    assert {parameter.dtype for parameter in model.parameters()} == {torch.float32}
    assert {parameter.device.type for parameter in model.parameters()} == {"cpu"}


def test_loads_a_tiny_xlm_roberta_cross_encoder(xlm_snapshot: Path) -> None:
    spec = tiny_encoder.tiny_reranker_spec("xlm_roberta", uses_token_type_ids=False)

    _, model = load_cross_encoder(spec, xlm_snapshot, "cpu")

    assert type(model).__name__ == "XLMRobertaForSequenceClassification"
    assert not model.training


def test_tokenizer_has_truncation_from_the_spec_and_no_padding(bert_snapshot: Path) -> None:
    spec = tiny_encoder.tiny_reranker_spec("bert", max_length=9)

    tokenizer, _ = load_cross_encoder(spec, bert_snapshot, "cpu")
    encoding = tokenizer.encode("w1 w2 w3 w4 w5 w6", "w7 w8 w9 w10 w11 w12")

    assert tokenizer.padding is None
    assert tokenizer.truncation is not None
    assert tokenizer.truncation["max_length"] == 9
    assert len(encoding.ids) == 9


def test_cross_encoder_loader_never_trusts_remote_code(
    bert_snapshot: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from transformers import AutoModelForSequenceClassification

    real = AutoModelForSequenceClassification.from_pretrained

    def refuse_remote_code(*args: Any, **kwargs: Any) -> Any:
        if "trust_remote_code" in kwargs:
            pytest.fail(f"trust_remote_code was passed: {kwargs['trust_remote_code']!r}")
        return real(*args, **kwargs)

    monkeypatch.setattr(AutoModelForSequenceClassification, "from_pretrained", refuse_remote_code)

    load_cross_encoder(tiny_encoder.tiny_reranker_spec("bert"), bert_snapshot, "cpu")


def test_jina_architecture_goes_through_the_fused_conversion(
    tmp_path: Path, bert_snapshot: Path, fused: dict[str, torch.Tensor]
) -> None:
    snapshot = _write_checkpoint(tmp_path / "jina-snapshot", fused)
    shutil.copy(bert_snapshot / "tokenizer.json", snapshot / "tokenizer.json")

    tokenizer, model = load_cross_encoder(_jina_spec(), snapshot, "cpu")

    token_ids = tokenizer.encode("w1 w2", "w3").ids
    with torch.inference_mode():
        logits = model(input_ids=torch.tensor([token_ids])).logits[0]
    torch.testing.assert_close(logits, _fused_logits(fused, token_ids), atol=_TOLERANCE, rtol=0)
    assert not model.training


def test_importing_the_loading_module_does_not_import_torch() -> None:
    import subprocess

    code = (
        "import sys, archon_search.reranker_loading;"
        "heavy = {'torch', 'transformers', 'tokenizers', 'safetensors'} & set(sys.modules);"
        "sys.exit(f'imported: {sorted(heavy)}' if heavy else 0)"
    )

    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=False)

    assert result.returncode == 0, result.stderr


_MODELS_BUILT_AT_ONCE = 4


def test_concurrent_cross_encoder_loads_build_one_model_at_a_time(bert_snapshot: Path) -> None:
    """``from_pretrained`` swaps torch globals while it builds a model (``transformers`` 4.x), so two
    threads building at once leave meta tensors behind ('Cannot copy out of meta tensor'). Every
    device serializes the build; ``accelerator_lock`` alone does that for ``mps`` only."""
    from transformers import AutoModelForSequenceClassification

    meter = tiny_encoder.OverlapMeter()
    with patch.object(AutoModelForSequenceClassification, "from_pretrained", new=meter):
        tiny_encoder.run_concurrently(
            lambda: load_cross_encoder(tiny_encoder.tiny_reranker_spec("bert"), bert_snapshot, "cpu"),
            _MODELS_BUILT_AT_ONCE,
        )

    assert meter.calls == _MODELS_BUILT_AT_ONCE
    assert meter.peak == 1


def test_concurrent_jina_conversions_build_one_model_at_a_time(fused_checkpoint: Path) -> None:
    """The Jina route builds its model by hand, not through ``from_pretrained``; that build must not
    overlap another thread's ``from_pretrained`` either, so it takes the same lock."""
    import transformers

    meter = tiny_encoder.OverlapMeter(build=transformers.XLMRobertaForSequenceClassification)
    with patch.object(transformers, "XLMRobertaForSequenceClassification", new=meter):
        tiny_encoder.run_concurrently(lambda: load_jina_reranker(fused_checkpoint), _MODELS_BUILT_AT_ONCE)

    assert meter.calls == _MODELS_BUILT_AT_ONCE
    assert meter.peak == 1
