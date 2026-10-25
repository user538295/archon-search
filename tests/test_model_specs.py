"""Tests for archon_search.model_specs."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import numpy as np
import pytest

from archon_search.model_specs import (
    EMBEDDER_SPECS,
    RERANKER_SPECS,
    UnsupportedModelError,
    get_embedder_spec,
    get_reranker_spec,
)
from archon_search.profiles import ENGLISH_PROFILES, MULTILINGUAL_PROFILES

_FIXTURE_DIR = Path(__file__).parent / "fixtures" / "runtime_parity"
_FIXTURE = _FIXTURE_DIR / "model_specs.json"
_HEAVY_MODULES = ("torch", "transformers", "tokenizers")
_CORPUS_TEXTS = 27
_CORPUS_PAIRS = 14

_EXPECTED_ARCHITECTURE = {
    "Xenova/ms-marco-MiniLM-L-6-v2": ("bert", True),
    "Xenova/ms-marco-MiniLM-L-12-v2": ("bert", True),
    "BAAI/bge-reranker-base": ("xlm_roberta", False),
    "jinaai/jina-reranker-v2-base-multilingual": ("jina_xlm_roberta", False),
    "BAAI/bge-reranker-v2-m3": ("xlm_roberta", False),
}
#: Graphs fastembed ships with FastGelu (tanh GELU): the whole reason their cosine is below 1.
_TANH_GELU_MODELS = {
    "BAAI/bge-small-en-v1.5",
    "BAAI/bge-base-en-v1.5",
    "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2",
    "BAAI/bge-base-en",
    "BAAI/bge-small-en",
    "BAAI/bge-small-zh-v1.5",
}


@pytest.fixture(scope="module")
def fixture_json() -> dict:
    return json.loads(_FIXTURE.read_text())


def test_every_profile_model_has_a_spec() -> None:
    profiles = [*ENGLISH_PROFILES.values(), *MULTILINGUAL_PROFILES.values()]
    for profile in profiles:
        assert get_embedder_spec(profile.embedder).name == profile.embedder
        if profile.reranker is not None:
            assert get_reranker_spec(profile.reranker).name == profile.reranker


def test_registries_hold_exactly_the_fixture_models(fixture_json: dict) -> None:
    assert set(EMBEDDER_SPECS) == set(fixture_json["embedders"])
    assert set(RERANKER_SPECS) == set(fixture_json["rerankers"])
    assert len(EMBEDDER_SPECS) + len(RERANKER_SPECS) == 24


def test_embedder_specs_match_the_frozen_fixture_json(fixture_json: dict) -> None:
    for name, frozen in fixture_json["embedders"].items():
        spec = EMBEDDER_SPECS[name]
        assert spec.name == name
        assert spec.repo == frozen["torch_repo"]
        assert spec.revision == frozen["revision"]
        assert spec.max_length == frozen["max_length"]
        assert spec.pooling == frozen["pooling"]
        assert spec.normalize is frozen["normalize"]
        assert spec.dim == frozen["output_dim"]
        assert spec.weight_mb == frozen["torch_weight_MB"]


def test_pooling_variants_and_unnormalized_models_are_the_documented_ones() -> None:
    assert {spec.pooling for spec in EMBEDDER_SPECS.values()} == {"cls", "mean", "last_token"}
    assert {name for name, spec in EMBEDDER_SPECS.items() if not spec.normalize} == {
        "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2",
        "sentence-transformers/paraphrase-multilingual-mpnet-base-v2",
        "intfloat/multilingual-e5-large",
        "ibm-granite/granite-embedding-small-english-r2",
    }


def test_the_fixture_records_which_graphs_use_tanh_gelu_and_claims_no_quantization(fixture_json: dict) -> None:
    flagged = {name for name, entry in fixture_json["embedders"].items() if entry["fastembed_graph_uses_tanh_gelu"]}

    assert flagged == _TANH_GELU_MODELS
    assert not any("fastembed_onnx_is_quantized" in entry for entry in fixture_json["embedders"].values())


def test_snowflake_registry_names_are_lower_case_but_the_hub_repo_is_not() -> None:
    for size in ("xs", "s", "m", "l"):
        spec = get_embedder_spec(f"snowflake/snowflake-arctic-embed-{size}")
        assert spec.repo == f"Snowflake/snowflake-arctic-embed-{size}"


def test_long_context_models_keep_their_limits() -> None:
    assert get_embedder_spec("ibm-granite/granite-embedding-small-english-r2").max_length == 8192
    assert get_embedder_spec("Qwen/Qwen3-Embedding-0.6B").max_length == 32768
    assert get_embedder_spec("sentence-transformers/all-MiniLM-L6-v2").max_length == 256
    assert get_reranker_spec("BAAI/bge-reranker-v2-m3").max_length == 8192


def test_qwen3_pools_the_last_token() -> None:
    spec = get_embedder_spec("Qwen/Qwen3-Embedding-0.6B")

    assert (spec.pooling, spec.normalize, spec.dim) == ("last_token", True, 1024)


def test_granite_pools_cls_without_normalizing() -> None:
    spec = get_embedder_spec("ibm-granite/granite-embedding-small-english-r2")

    assert (spec.pooling, spec.normalize, spec.dim) == ("cls", False, 384)


def test_every_embedder_spec_has_a_golden_array_of_its_dimension() -> None:
    with np.load(_FIXTURE_DIR / "embedder_golden.npz") as golden:
        assert set(golden.files) == {name.replace("/", "__") for name in EMBEDDER_SPECS}
        for name, spec in EMBEDDER_SPECS.items():
            assert golden[name.replace("/", "__")].shape == (_CORPUS_TEXTS, spec.dim), name


def test_every_reranker_spec_has_golden_scores_for_every_pair() -> None:
    golden = json.loads((_FIXTURE_DIR / "reranker_golden.json").read_text())

    assert set(golden) == set(RERANKER_SPECS)
    assert all(len(scores) == _CORPUS_PAIRS for scores in golden.values())


def test_reranker_specs_match_the_frozen_fixture_json(fixture_json: dict) -> None:
    for name, frozen in fixture_json["rerankers"].items():
        spec = RERANKER_SPECS[name]
        assert spec.name == name
        assert spec.repo == frozen["torch_repo"]
        assert spec.revision == frozen.get("revision", frozen.get("torch_revision_sha"))
        assert spec.max_length == frozen["max_length"]
        assert spec.weight_mb == frozen["torch_weight_MB"]
        assert (spec.architecture, spec.uses_token_type_ids) == _EXPECTED_ARCHITECTURE[name]
        if "architecture" in frozen:
            assert (frozen["architecture"], frozen["uses_token_type_ids"]) == _EXPECTED_ARCHITECTURE[name]


def test_multilingual_models_are_not_normalized_and_keep_their_truncation() -> None:
    expected = {
        "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2": 128,
        "sentence-transformers/paraphrase-multilingual-mpnet-base-v2": 512,
        "intfloat/multilingual-e5-large": 512,
    }
    for name, max_length in expected.items():
        spec = get_embedder_spec(name)
        assert spec.normalize is False
        assert spec.pooling == "mean"
        assert spec.max_length == max_length


def test_jina_reranker_truncates_at_1024() -> None:
    assert get_reranker_spec("jinaai/jina-reranker-v2-base-multilingual").max_length == 1024


@pytest.mark.parametrize("getter", [get_embedder_spec, get_reranker_spec])
def test_unknown_model_raises_with_supported_list(getter) -> None:
    with pytest.raises(UnsupportedModelError) as excinfo:
        getter("nope/not-a-model")

    registry = EMBEDDER_SPECS if getter is get_embedder_spec else RERANKER_SPECS
    assert excinfo.value.name == "nope/not-a-model"
    assert excinfo.value.supported == tuple(sorted(registry))
    assert excinfo.value.args[0] == (
        f"unsupported model 'nope/not-a-model'; supported: {', '.join(sorted(registry))}"
    )
    assert isinstance(excinfo.value, ValueError)


def test_registries_are_immutable() -> None:
    with pytest.raises(TypeError):
        EMBEDDER_SPECS["x"] = EMBEDDER_SPECS["BAAI/bge-small-en-v1.5"]  # type: ignore[index]
    with pytest.raises(TypeError):
        RERANKER_SPECS["x"] = next(iter(RERANKER_SPECS.values()))  # type: ignore[index]


def test_importing_specs_does_not_import_torch() -> None:
    code = (
        "import sys; import archon_search.model_specs; "
        f"print([m for m in {_HEAVY_MODULES!r} if m in sys.modules])"
    )
    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, check=True
    )
    assert result.stdout.strip() == "[]"
