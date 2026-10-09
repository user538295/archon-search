"""S671 guard: wizard pre-warm loads the model with the real fastembed, not only downloads it.

``_prewarm_models`` splits each model into a download phase (``lazy_load=True``) and a
load phase (the runtime ``ModelEmbedder``). That split holds only while fastembed's
``lazy_load=True`` downloads without building the ONNX session. The default suite stubs
fastembed, so this runs pre-warm in a clean child interpreter with the real fastembed and
onnxruntime, offline, against the tiny synthetic model ``test_embedder_external_data``
builds: a valid model must pre-warm, and the same cache with a model file that is not
ONNX must fail the load phase — the S671 case the download-only pre-warm let through.

S635: with ``[database].providers`` naming a provider this host's onnxruntime lacks (an
NVIDIA host without onnxruntime-gpu), pre-warm must load both the embedder and the
reranker on CPU and the wizard's console must say why exactly once — the child configures
no logging, exactly like the wizard. Runs on the synthetic default models
``test_onnx_provider_fallback`` builds.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from archon_search.config import SearchConfig
from tests.test_embedder_external_data import (
    _DATA_XET_HASH,
    _MODEL_NAME,
    _MODEL_XET_HASH,
    _build_model,
    _ModelFiles,
    _fastembed_model_files,
    _last_line_json,
    _run_child,
    _tokenizer_files,
    _write_shared_blob_cache,
)
from tests.test_onnx_provider_fallback import (
    _UNAVAILABLE_PROVIDER_CASES,
    _unavailable_provider_list,
    _write_default_models,
)

_PREWARM_TIMEOUT_S = "60"
_NOT_ONNX = b"not an onnx model"
_LOAD_FAILURE = "was downloaded but cannot be loaded"
# Pre-warm in a fresh interpreter: the parent's fastembed is a stub.
_PREWARM_SCRIPT = """
import json, sys
from archon_search.config import SearchConfig
from archon_search.install.errors import InstallError
from archon_search.install.prewarm import _prewarm_models
model, reranker, timeout, *providers = sys.argv[1:]
try:
    _prewarm_models(
        SearchConfig(embedding_model=model, reranker_model=reranker, providers=providers),
        float(timeout),
    )
except InstallError as exc:
    print(json.dumps(str(exc)))
else:
    print(json.dumps(None))
"""


def _offline_env(tmp_path: Path, name: str) -> tuple[Path, dict[str, str]]:
    """An isolated data dir and the child env that loads models only from it, offline."""
    data_dir = tmp_path / name
    # Empty, so a model can only come from the cache pre-warm passes as cache_dir.
    fallback_cache = tmp_path / f"{name}-fastembed_cache"
    fallback_cache.mkdir()
    env = {
        **os.environ,
        "ARCHON_SEARCH_DATA_DIR": str(data_dir),
        "FASTEMBED_CACHE_PATH": str(fallback_cache),
        "HF_HUB_OFFLINE": "1",
    }
    return data_dir, env


def _run_prewarm(
    env: dict[str, str], model: str, reranker: str, providers: list[str]
) -> tuple[str | None, str]:
    """Pre-warm in a child; return the InstallError message (None on success) and stderr."""
    child = _run_child(_PREWARM_SCRIPT, model, reranker, _PREWARM_TIMEOUT_S, *providers, env=env)
    assert child.returncode == 0, child.stderr
    return _last_line_json(child.stdout), child.stderr


def _prewarm_offline(
    tmp_path: Path, name: str, files: _ModelFiles, model_bytes: bytes, data_bytes: bytes
) -> str | None:
    """Pre-warm ``_MODEL_NAME`` from an offline cache holding *model_bytes*; return the
    InstallError message, or None on success."""
    data_dir, env = _offline_env(tmp_path, name)
    _write_shared_blob_cache(
        data_dir / "models",
        files.repo_dir,
        xet_files={
            files.model_file: (model_bytes, _MODEL_XET_HASH),
            files.data_file: (data_bytes, _DATA_XET_HASH),
        },
        git_files=_tokenizer_files(),
    )
    error, _stderr = _run_prewarm(env, _MODEL_NAME, "", [])
    return error


def test_prewarm_loads_a_valid_model_and_rejects_one_that_cannot_load(tmp_path: Path) -> None:
    files = _fastembed_model_files()
    model_bytes, data_bytes = _build_model(tmp_path / "build", files)

    error = _prewarm_offline(tmp_path, "valid", files, model_bytes, data_bytes)
    assert error is None

    error = _prewarm_offline(tmp_path, "corrupt", files, _NOT_ONNX, data_bytes)
    assert error is not None and _LOAD_FAILURE in error, error
    assert repr(_MODEL_NAME) in error


@pytest.mark.parametrize(("provider", "then_cpu"), _UNAVAILABLE_PROVIDER_CASES)
def test_prewarm_falls_back_to_cpu_and_tells_the_wizard_user(
    tmp_path: Path, provider: str, then_cpu: bool
) -> None:
    providers = _unavailable_provider_list(provider, then_cpu)
    data_dir, env = _offline_env(tmp_path, "unavailable-provider")
    _write_default_models(data_dir / "models")

    error, stderr = _run_prewarm(
        env, SearchConfig.embedding_model, SearchConfig.reranker_model, providers
    )

    assert error is None
    # The reranker failing only warns (S202), so its load is proven by the absence of that.
    assert _LOAD_FAILURE not in stderr, stderr
    assert f"configured ONNX providers not available: {providers[0]}" in stderr
    assert "loading models on CPUExecutionProvider" in stderr
    # Embedder and reranker both load under the same list: one warning, not one per model.
    assert stderr.count("configured ONNX providers not available") == 1, stderr
