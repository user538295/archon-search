"""S288 — export → import into a fresh server → search must return the source's results.

Bug: an import job reports ``DONE`` without ever building the embedder that serves
the imported collection. ``_import_task`` (``archon_search/server/routes_export.py``)
only calls ``embedder_cache.get_or_load`` — which returns a *lazy* embedder whose
ONNX model is built on the first ``encode`` (``archon_search/embedder.py``
``ModelEmbedder``) — and ``recompute_collection_meta``, which embeds nothing unless
the collection has a description. Ingest, by contrast, embeds every chunk with that
same cached embedder and then awaits the model warm-up before ``DONE`` (S281).

On a freshly started target server the lifespan warm-up is still building the models
(the real build is seconds to minutes), so the first search after ``import --wait``
finds the embedder cold, ``warmup_for_search`` gives up after
``EMBEDDER_WARMUP_WAIT_SECONDS`` and the search silently runs ``fts_only``. Every
chunk that shares no term with the query — here ``# Gamma`` — vanishes from the
results, although the source server returns it via the vector leg.

The cold ONNX build is modelled faithfully: the fake ``TextEmbedding`` constructor
(the point where ``ModelEmbedder.encode`` builds the real ONNX session) takes
``_COLD_BUILD_SECONDS``, i.e. "the build takes longer than the import". The
scheduler tick is shortened (as in S291's test) so the import starts at once rather
than up to 5 s later, which keeps the whole import well inside the cold window.
Nothing else in the stubbed stack changes.

Accepted timing risk: without the fix the test is red only while the import finishes
inside the cold window, so an extremely stalled host could let the build complete
first and turn it falsely green. There is no deterministic gate to close that: holding
the ONNX build until the import reports ``DONE`` would deadlock the fixed path, whose
``DONE`` waits on exactly that build. With the fix the result is deterministic.

Run with:
    uv run pytest tests/integration/test_s288_export_import_roundtrip_search.py --no-cov
"""
from __future__ import annotations

import shutil
import sys
import time
from pathlib import Path

import pytest

import archon_search.jobs.scheduler as _scheduler_module
from tests.integration.conftest import ingest_doc, make_real_app, search, wait_for_job_done

pytestmark = [pytest.mark.integration, pytest.mark.xdist_group("s288")]

_COLLECTION = "s288_corpus"
_QUERY = "semantic search embeddings"
_DOCS: dict[str, str] = {
    "/s288/alpha.md": "# Alpha\nArchon search is a semantic search tool for developers.\n",
    "/s288/beta.md": "# Beta\nVector embeddings enable semantic retrieval of documents.\n",
    "/s288/gamma.md": "# Gamma\nReranking reorders candidate chunks by relevance to the query.\n",
}
_GAMMA_MARKER = "# Gamma"
_SCHEDULER_TICK_SECONDS: float = 0.1
# Per-embedder cold ONNX build time. Startup builds the global embedder, then the
# cached per-collection one (server/warmup.py), so the latter stays cold for about
# twice this — far longer than a 3-doc import takes — yet the build still finishes.
_COLD_BUILD_SECONDS: float = 3.0
_STUB_TEXT_EMBEDDING = "_FakeTextEmbedding"


def _auth(api_key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {api_key}"}


def _install_slow_onnx_build(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make every embedder ONNX build take ``_COLD_BUILD_SECONDS``.

    ``ModelEmbedder.encode`` resolves ``fastembed.TextEmbedding`` at call time, so
    patching the module attribute affects every embedder built from here on.
    """
    fastembed = sys.modules["fastembed"]
    instant_text_embedding = fastembed.TextEmbedding
    # The delay models a real build only on top of the instant test stub (tests/_search_stubs.py).
    assert instant_text_embedding.__name__ == _STUB_TEXT_EMBEDDING, instant_text_embedding

    def _slow_build(model_name: str, **kwargs: object) -> object:
        time.sleep(_COLD_BUILD_SECONDS)
        return instant_text_embedding(model_name, **kwargs)

    monkeypatch.setattr(fastembed, "TextEmbedding", _slow_build)


def test_s288_imported_collection_search_matches_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source_dir = tmp_path / "source"
    target_dir = tmp_path / "target"
    source_dir.mkdir()
    target_dir.mkdir()
    monkeypatch.setattr(_scheduler_module, "_SCHEDULER_TICK_SECONDS", _SCHEDULER_TICK_SECONDS)

    # --- source: ingest, search, export ---
    with make_real_app(source_dir, monkeypatch) as (client, _cfg, api_key):
        for source_path, text in _DOCS.items():
            ingest_doc(client, _COLLECTION, text, source_path, api_key=api_key)
        source_texts = sorted(r["text"] for r in search(client, _COLLECTION, _QUERY, api_key=api_key))
        # Presence anchor: Gamma shares no term with the query, so only the vector leg finds it.
        assert len(source_texts) == len(_DOCS), f"source search is broken: {source_texts}"
        assert any(t.startswith(_GAMMA_MARKER) for t in source_texts), source_texts

        export = client.post(f"/collections/{_COLLECTION}/export", json={}, headers=_auth(api_key))
        assert export.status_code == 202, f"{export.status_code}: {export.text}"
        archive = Path(wait_for_job_done(client, export.json()["job_id"], what="export").result["archive_path"])

    # --- copy the archive into the isolated target's data dir, like the report ---
    target_exports = target_dir / "exports"
    target_exports.mkdir()
    target_archive = target_exports / archive.name
    shutil.copy(archive, target_archive)

    # --- target: a fresh server whose model build is still running ---
    _install_slow_onnx_build(monkeypatch)
    with make_real_app(target_dir, monkeypatch) as (client, cfg, api_key):
        # Presence anchor: the scenario needs the collection's embedder still cold here.
        embedder_cache = client.app.state.embedder_cache
        assert embedder_cache.cold_models([cfg.embedding_model]) == [cfg.embedding_model], (
            "the per-collection embedder was already warm before the import — this run "
            "cannot tell a warming import from a non-warming one"
        )
        imported = client.post(
            f"/collections/{_COLLECTION}/import",
            json={"path": str(target_archive)},
            headers=_auth(api_key),
        )
        assert imported.status_code == 202, f"{imported.status_code}: {imported.text}"
        result = wait_for_job_done(client, imported.json()["job_id"], what="import").result
        assert result["imported"] == result["total_in_archive"] == len(_DOCS), result
        # The cause, checked before its symptom so a failure names it.
        assert embedder_cache.cold_models([cfg.embedding_model]) == [], (
            "S288: the import reached DONE while the collection's embedder was still cold"
        )

        target_texts = sorted(r["text"] for r in search(client, _COLLECTION, _QUERY, api_key=api_key))

    assert target_texts == source_texts, (
        "S288: round-trip did not preserve the search results — the target search ran "
        "without its vector leg because the import never warmed the collection's embedder.\n"
        f"source: {source_texts}\ntarget: {target_texts}"
    )
