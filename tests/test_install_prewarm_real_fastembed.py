"""S671 guard: wizard pre-warm loads the model with the real fastembed, not only downloads it.

``_prewarm_models`` splits each model into a download phase (``lazy_load=True``) and a
load phase (the runtime ``ModelEmbedder``). That split holds only while fastembed's
``lazy_load=True`` downloads without building the ONNX session. The default suite stubs
fastembed, so this runs pre-warm in a clean child interpreter with the real fastembed and
onnxruntime, offline, against the tiny synthetic model ``test_embedder_external_data``
builds: a valid model must pre-warm, and the same cache with a model file that is not
ONNX must fail the load phase — the S671 case the download-only pre-warm let through.
"""
from __future__ import annotations

import os
from pathlib import Path

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

_PREWARM_TIMEOUT_S = "60"
_NOT_ONNX = b"not an onnx model"
_LOAD_FAILURE = "was downloaded but cannot be loaded"
# Pre-warm in a fresh interpreter: the parent's fastembed is a stub.
_PREWARM_SCRIPT = """
import json, sys
from archon_search.config import SearchConfig
from archon_search.install.errors import InstallError
from archon_search.install.prewarm import _prewarm_models
try:
    _prewarm_models(SearchConfig(embedding_model=sys.argv[1], reranker_model=""), float(sys.argv[2]))
except InstallError as exc:
    print(json.dumps(str(exc)))
else:
    print(json.dumps(None))
"""


def _prewarm_offline(
    tmp_path: Path, name: str, files: _ModelFiles, model_bytes: bytes, data_bytes: bytes
) -> str | None:
    """Pre-warm ``_MODEL_NAME`` from an offline cache holding *model_bytes*; return the
    InstallError message, or None on success."""
    data_dir = tmp_path / name
    _write_shared_blob_cache(
        data_dir / "models",
        files.repo_dir,
        xet_files={
            files.model_file: (model_bytes, _MODEL_XET_HASH),
            files.data_file: (data_bytes, _DATA_XET_HASH),
        },
        git_files=_tokenizer_files(),
    )
    # Empty, so the model can only come from the cache pre-warm passes as cache_dir.
    fallback_cache = tmp_path / f"{name}-fastembed_cache"
    fallback_cache.mkdir()
    env = {
        **os.environ,
        "ARCHON_SEARCH_DATA_DIR": str(data_dir),
        "FASTEMBED_CACHE_PATH": str(fallback_cache),
        "HF_HUB_OFFLINE": "1",
    }
    child = _run_child(_PREWARM_SCRIPT, _MODEL_NAME, _PREWARM_TIMEOUT_S, env=env)
    assert child.returncode == 0, child.stderr
    return _last_line_json(child.stdout)


def test_prewarm_loads_a_valid_model_and_rejects_one_that_cannot_load(tmp_path: Path) -> None:
    files = _fastembed_model_files()
    model_bytes, data_bytes = _build_model(tmp_path / "build", files)

    assert _prewarm_offline(tmp_path, "valid", files, model_bytes, data_bytes) is None

    error = _prewarm_offline(tmp_path, "corrupt", files, _NOT_ONNX, data_bytes)
    assert error is not None and _LOAD_FAILURE in error, error
    assert repr(_MODEL_NAME) in error
