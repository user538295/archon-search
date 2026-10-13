"""S635 regression: a configured ONNX provider this host lacks falls back instead of failing.

With ``[database].providers = ["CUDAExecutionProvider"]`` (alone or followed by
``"CPUExecutionProvider"``) on a host whose onnxruntime has no CUDA build, both models must
still load on the providers the host does have, and model validation must report them as
loaded together with the warning ``configured ONNX providers not available:
CUDAExecutionProvider``. fastembed itself raises ``ValueError("Provider … is not
available")`` for any requested provider onnxruntime lacks, even when CPU is listed too, so
the fallback has to happen in archon before the providers reach fastembed. Each case also
runs with the first provider ``onnxruntime.get_all_providers()`` names that this host's
build lacks, so the coverage holds on a CUDA host too.

The default suite stubs fastembed (``tests/_search_stubs.py``), so every load runs in a clean
child process through archon's real ``ModelEmbedder``/``ModelReranker``/
``validate_providers_shared`` with the real fastembed and onnxruntime, offline, against tiny
synthetic models laid out under ``get_models_dir()`` the way huggingface_hub caches them —
the same approach as ``tests/test_embedder_external_data.py``.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from archon_search.config import SearchConfig
from tests.test_embedder_external_data import _last_line_json, _offline_child_env, _run_child

# onnx, onnxruntime and tokenizers are imported inside the functions that use them: a
# module-level import would load them into every xdist worker at collection.

_CPU_PROVIDER = "CPUExecutionProvider"
_CUDA_PROVIDER = "CUDAExecutionProvider"
# Stands for the first provider onnxruntime knows but this host's build lacks, so the
# fallback is covered on every host — CUDA hosts included, where the CUDA cases skip.
_HOST_UNAVAILABLE = "host-unavailable"
# (provider, followed by CPU?) — resolved by `_unavailable_provider_list` at run time, so
# onnxruntime stays out of collection.
_UNAVAILABLE_PROVIDER_CASES = [
    pytest.param(_CUDA_PROVIDER, False, id="cuda-only"),
    pytest.param(_CUDA_PROVIDER, True, id="cuda-then-cpu"),
    pytest.param(_HOST_UNAVAILABLE, False, id="host-unavailable-only"),
    pytest.param(_HOST_UNAVAILABLE, True, id="host-unavailable-then-cpu"),
]

# The shipped defaults, so the test covers the models a default install loads.
_EMBEDDING_MODEL = SearchConfig.embedding_model
_RERANKER_MODEL = SearchConfig.reranker_model
_REVISION = "6" * 40

_PAD_TOKEN = "[PAD]"
_UNK_TOKEN = "[UNK]"
_QUERY_TOKEN = "s635"
_VOCAB = {_PAD_TOKEN: 0, _UNK_TOKEN: 1, _QUERY_TOKEN: 2}
_EMBEDDING_DIM = 4
_QUERY_TOKEN_SCORE = 0.25
_MAX_CONTEXT = 512
_ONNX_OPSET = 17
# IR version of opset 17 (onnx 1.12). Pinned: `make_model` defaults to the newest IR the
# installed onnx knows, which can be ahead of what the installed onnxruntime reads.
_ONNX_IR_VERSION = 8
_ONE_HOT_TOLERANCE = 1e-6

# Run in a fresh interpreter: the parent's fastembed is a stub.
# The installed fastembed's description of each model, through its public API.
_DESCRIPTOR_SCRIPT = """
import json, sys
from fastembed import TextEmbedding
from fastembed.rerank.cross_encoder import TextCrossEncoder
def describe(cls, name):
    (match,) = [m for m in cls.list_supported_models() if m["model"] == name]
    return {"repo": match["sources"]["hf"], "model_file": match["model_file"]}
print(json.dumps([describe(TextEmbedding, sys.argv[1]), describe(TextCrossEncoder, sys.argv[2])]))
"""
# The runtime and validation entry points a server calls with `[database].providers`.
_LOADER_SCRIPT = """
import json, sys
from archon_search.embedder import ModelEmbedder
from archon_search.model_validation import validate_providers_shared
from archon_search.reranker import ModelReranker
step, embedding_model, reranker_model, query, *providers = sys.argv[1:]
if step == "embed":
    result = ModelEmbedder(embedding_model, providers=providers).encode([query])
elif step == "rerank":
    result = ModelReranker(reranker_model, providers=providers).predict([(query, query)])
else:
    result = list(validate_providers_shared(providers, embedding_model, reranker_model))
print(json.dumps(result))
"""


@dataclass(frozen=True)
class _ModelsCache:
    """Environment a child needs to load the synthetic models offline."""

    env: dict[str, str]


def _unavailable_provider_list(provider: str, then_cpu: bool) -> list[str]:
    """A provider list whose first entry this host's onnxruntime lacks, for one
    ``_UNAVAILABLE_PROVIDER_CASES`` case; skips when the named provider is available."""
    import onnxruntime  # noqa: PLC0415

    available = onnxruntime.get_available_providers()
    if provider == _HOST_UNAVAILABLE:
        missing = [p for p in onnxruntime.get_all_providers() if p not in available]
        if not missing:
            pytest.skip("this onnxruntime build exposes every provider it knows, so none is unavailable")
        provider = missing[0]
    elif provider in available:
        pytest.skip(
            f"{provider} is available on this host, so it cannot stand in for a configured"
            " provider the host lacks"
        )
    return [provider, _CPU_PROVIDER] if then_cpu else [provider]


def _gather_model(table: np.ndarray, output_name: str, output_shape: list[Any]) -> bytes:
    """A single Gather of ``input_ids`` over *table*: each token reads its own row."""
    from onnx import TensorProto, helper, numpy_helper  # noqa: PLC0415

    weights = "weights"
    graph = helper.make_graph(
        [helper.make_node("Gather", [weights, "input_ids"], [output_name])],
        "s635",
        inputs=[helper.make_tensor_value_info("input_ids", TensorProto.INT64, ["batch", "seq"])],
        outputs=[helper.make_tensor_value_info(output_name, TensorProto.FLOAT, output_shape)],
        initializer=[numpy_helper.from_array(table, name=weights)],
    )
    model = helper.make_model(
        graph,
        opset_imports=[helper.make_opsetid("", _ONNX_OPSET)],
        ir_version=_ONNX_IR_VERSION,
    )
    return model.SerializeToString()


def _embedding_table() -> np.ndarray:
    """One-hot row per vocabulary token, so the output names the row that was read."""
    return np.eye(len(_VOCAB), _EMBEDDING_DIM, dtype=np.float32)


def _score_table() -> np.ndarray:
    """One relevance score per token; the cross-encoder reads the first token's."""
    scores = np.zeros(len(_VOCAB), dtype=np.float32)
    scores[_VOCAB[_QUERY_TOKEN]] = _QUERY_TOKEN_SCORE
    return scores


def _tokenizer_files() -> dict[str, bytes]:
    from tokenizers import Tokenizer, models, pre_tokenizers  # noqa: PLC0415

    tokenizer = Tokenizer(models.WordLevel(_VOCAB, unk_token=_UNK_TOKEN))
    tokenizer.pre_tokenizer = pre_tokenizers.Whitespace()
    config = {"model_max_length": _MAX_CONTEXT, "pad_token": _PAD_TOKEN}
    return {
        "tokenizer.json": tokenizer.to_str().encode(),
        "tokenizer_config.json": json.dumps(config).encode(),
    }


def _write_snapshot(cache_dir: Path, repo: str, files: dict[str, bytes]) -> None:
    """Lay *files* out as a huggingface_hub snapshot of *repo* at ``_REVISION``."""
    from huggingface_hub.file_download import repo_folder_name  # noqa: PLC0415

    repo_dir = cache_dir / repo_folder_name(repo_id=repo, repo_type="model")
    (repo_dir / "refs").mkdir(parents=True)
    (repo_dir / "refs" / "main").write_text(_REVISION)
    snapshot_dir = repo_dir / "snapshots" / _REVISION
    for name, payload in files.items():
        path = snapshot_dir / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(payload)


def _write_default_models(models_dir: Path) -> None:
    """Synthetic default embedder + reranker as huggingface_hub snapshots in *models_dir*."""
    child = _run_child(_DESCRIPTOR_SCRIPT, _EMBEDDING_MODEL, _RERANKER_MODEL)
    assert child.returncode == 0, child.stderr
    embedder, reranker = _last_line_json(child.stdout)

    tokenizer = _tokenizer_files()
    _write_snapshot(
        models_dir,
        embedder["repo"],
        {
            embedder["model_file"]: _gather_model(
                _embedding_table(), "last_hidden_state", ["batch", "seq", _EMBEDDING_DIM]
            ),
            **tokenizer,
        },
    )
    _write_snapshot(
        models_dir,
        reranker["repo"],
        {
            reranker["model_file"]: _gather_model(_score_table(), "logits", ["batch", "seq"]),
            **tokenizer,
        },
    )


@pytest.fixture(scope="module")
def models_cache(tmp_path_factory: pytest.TempPathFactory) -> _ModelsCache:
    """Synthetic default embedder + reranker under an isolated ``get_models_dir()``."""
    root = tmp_path_factory.mktemp("s635")
    data_dir = root / "data"
    _write_default_models(data_dir / "models")
    # BE-26: with FASTEMBED_CACHE_PATH unset, archon loads from `<data>/models`; nothing
    # but the synthetic snapshots can load.
    return _ModelsCache(env=_offline_child_env(data_dir, root / "tmpdir"))


def _load(models_cache: _ModelsCache, step: str, providers: list[str]) -> Any:
    child = _run_child(
        _LOADER_SCRIPT,
        step,
        _EMBEDDING_MODEL,
        _RERANKER_MODEL,
        _QUERY_TOKEN,
        *providers,
        env=models_cache.env,
    )
    assert child.returncode == 0, f"--- stderr ---\n{child.stderr}"
    return _last_line_json(child.stdout)


@pytest.mark.parametrize(("provider", "then_cpu"), _UNAVAILABLE_PROVIDER_CASES)
def test_embedder_falls_back_from_unavailable_provider(
    models_cache: _ModelsCache, provider: str, then_cpu: bool
) -> None:
    providers = _unavailable_provider_list(provider, then_cpu)

    vectors = _load(models_cache, "embed", providers)

    # One query of one token: CLS pooling returns that token's one-hot row, unit length.
    expected = _embedding_table()[_VOCAB[_QUERY_TOKEN]]
    assert vectors == [pytest.approx(expected.tolist(), abs=_ONE_HOT_TOLERANCE)]


@pytest.mark.parametrize(("provider", "then_cpu"), _UNAVAILABLE_PROVIDER_CASES)
def test_reranker_falls_back_from_unavailable_provider(
    models_cache: _ModelsCache, provider: str, then_cpu: bool
) -> None:
    providers = _unavailable_provider_list(provider, then_cpu)

    scores = _load(models_cache, "rerank", providers)

    assert scores == [pytest.approx(_QUERY_TOKEN_SCORE)]


@pytest.mark.parametrize(("provider", "then_cpu"), _UNAVAILABLE_PROVIDER_CASES)
def test_validation_reports_models_loaded_with_missing_provider_warning(
    models_cache: _ModelsCache, provider: str, then_cpu: bool
) -> None:
    providers = _unavailable_provider_list(provider, then_cpu)
    unavailable = providers[0]

    embedder_ok, reranker_ok, warnings = _load(models_cache, "validate", providers)

    assert (embedder_ok, reranker_ok) == (True, True), warnings
    assert warnings == [f"configured ONNX providers not available: {unavailable}"], warnings


def test_s493_reranker_loads_from_fastembed_cache_path_outside_the_data_dir(
    tmp_path: Path,
) -> None:
    """S493: the real fastembed cross-encoder reads its weights from ``FASTEMBED_CACHE_PATH``.

    The synthetic reranker snapshot exists ONLY there, outside ``ARCHON_SEARCH_DATA_DIR``;
    ``<data>/models`` and fastembed's TMPDIR fallback are empty and the child is offline,
    so a ``ModelReranker`` that ignored the env var would fail with "from any source".
    """
    data_dir = tmp_path / "archon-data"
    (data_dir / "models").mkdir(parents=True)
    fastembed_cache = tmp_path / "fastembed-cache"
    _write_default_models(fastembed_cache)
    env = _offline_child_env(data_dir, tmp_path / "tmpdir", fastembed_cache=fastembed_cache)

    scores = _load(_ModelsCache(env=env), "rerank", [_CPU_PROVIDER])

    assert scores == [pytest.approx(_QUERY_TOKEN_SCORE)]
    assert not any((data_dir / "models").iterdir()), "nothing may land in <data>/models"
