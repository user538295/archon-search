"""S279: ``/ready`` must not answer 200 when startup warm-up *failed*.

The sibling repro (``test_s279_max_profile_cold_reranker.py``) closed the
*pending* hole: ``/ready`` now stays 503 while the warm-up task is running.
It left the *failed* hole open. ``routes_ready.py`` computes::

    ready_flag = storage_ok and not warmup_pending and not sync_pending

``WarmupResult.FAILED`` is not ``PENDING``, so a warm-up that blew its
``_EAGER_WARMUP_TIMEOUT_SECONDS`` budget (``server/app.py:95``, 600 s) flips
``/ready`` straight from 503 to 200 with both ONNX models still cold — exactly
the state the gate exists to hide.

That budget is realistic to exceed under the ``max`` profile precisely because
it is the heavy one: ``BAAI/bge-large-en-v1.5`` (~1.3 GB) is downloaded and
built by ``embedder_cache.preload``, which alone can consume the whole window.
The original ordering ran that preload *before* ``pipeline.warmup_models()``, so
the cross-encoder was never warmed at all; the fix warms the cross-encoder
first, leaving only an embedder cold when the budget blows.

Without that ordering the first ``POST /search`` pays the cold build on the request path at
``server/routes_search.py:362`` — ``await pipeline.warmup_models(embedder)``,
which sits *outside* the route's ``_SEARCH_TIMEOUT_SECONDS`` (30 s) budget and
is bounded only by ``Reranker._WARMUP_TIMEOUT_SECONDS`` (300 s,
``reranker.py:16``). The measured cold build of ``BAAI/bge-reranker-base`` is
91 s, so the client gets no response at all: ``status=0 body=None``.

Both slow loads are simulated here so the test stays deterministic and
weight-free; the mechanism — same code path, same ordering — is the point.
"""
from __future__ import annotations

import asyncio
import sys
import time

import pytest

from archon_search.embedder_cache import EmbedderCache
from archon_search.profiles import ENGLISH_PROFILES
from archon_search.server import app as app_module
from archon_search.server.schemas import WarmupResult
from tests.integration.conftest import ingest_file_via_path, make_real_app

pytestmark = [pytest.mark.integration, pytest.mark.xdist_group("s279")]

_MAX_PROFILE = ENGLISH_PROFILES["max"]

#: Stand-in for the cold download+build of ``BAAI/bge-large-en-v1.5`` — long
#: enough to exhaust the warm-up budget below, as the real ~1.3 GB fetch does.
_SLOW_EMBEDDER_PRELOAD_SECONDS = 5.0

#: Stand-in for ``_EAGER_WARMUP_TIMEOUT_SECONDS`` (600 s in production).
_WARMUP_BUDGET_SECONDS = 0.5

#: Stand-in for the measured 91.2 s cold download+build of ``BAAI/bge-reranker-base``.
_COLD_CROSS_ENCODER_LOAD_SECONDS = 4.0

#: Budget a normal HTTP client allows a single search. Overrunning it is what the
#: reporter saw as ``status=0 body=None`` — a client-side read timeout, no response.
_CLIENT_SEARCH_TIMEOUT_SECONDS = 2.0

#: Ceiling for waiting out the backgrounded warm-up task before the first search.
_WARMUP_SETTLE_TIMEOUT_SECONDS = 30.0

_MAX_PROFILE_TOML = f"""
[database]
embedding_model = "{_MAX_PROFILE.embedder}"
reranker_model = "{_MAX_PROFILE.reranker}"
chunk_size = {_MAX_PROFILE.chunk_size}
profile = "max"
multilingual = false
"""


def _install_slow_embedder_preload(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make the lifespan's embedder preload outlast the warm-up budget.

    ``_run_model_warmup`` awaits ``pipeline.warmup_models()`` first and
    ``embedder_cache.preload`` second, both inside one ``asyncio.timeout``
    (``server/app.py``) — so an over-budget preload lands ``warmup_result`` on
    ``FAILED`` with the cross-encoder already warm. Reverse those two awaits and
    the reranker is never warmed, which is exactly what this test detects.
    """

    async def _slow_preload(self: EmbedderCache, model_names: list[str]) -> None:
        await asyncio.sleep(_SLOW_EMBEDDER_PRELOAD_SECONDS)

    monkeypatch.setattr(EmbedderCache, "preload", _slow_preload)
    monkeypatch.setattr(app_module, "_EAGER_WARMUP_TIMEOUT_SECONDS", _WARMUP_BUDGET_SECONDS)


def _install_cold_cross_encoder(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make cross-encoder construction cost ``_COLD_CROSS_ENCODER_LOAD_SECONDS``.

    ``ModelReranker.predict`` imports ``TextCrossEncoder`` from
    ``fastembed.rerank.cross_encoder`` on its first call (``reranker.py``), so
    patching the class there puts the cost exactly where the real ONNX build
    sits: inside the first search request.
    """
    module = sys.modules["fastembed.rerank.cross_encoder"]
    base = module.TextCrossEncoder

    class _ColdLoadTextCrossEncoder(base):  # type: ignore[misc, valid-type]
        def __init__(self, *args: object, **kwargs: object) -> None:
            time.sleep(_COLD_CROSS_ENCODER_LOAD_SECONDS)
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(module, "TextCrossEncoder", _ColdLoadTextCrossEncoder)


def test_s279_ready_200_after_failed_warmup_still_hides_a_cold_reranker(tmp_path, monkeypatch):
    """A 200 from /ready must mean the next /search is not a cold model build."""
    doc = tmp_path / "doc.md"
    doc.write_text("# max_test\nThe quick brown fox jumps over the lazy dog.\n")
    data_dir = tmp_path / "app"
    data_dir.mkdir()

    _install_slow_embedder_preload(monkeypatch)

    with make_real_app(data_dir, monkeypatch, toml_content=_MAX_PROFILE_TOML) as (
        client,
        cfg,
        api_key,
    ):
        assert cfg.chunk_size == _MAX_PROFILE.chunk_size
        assert cfg.reranker_model == _MAX_PROFILE.reranker
        headers = {"Authorization": f"Bearer {api_key}"}

        ingest_file_via_path(client, "max_test", str(doc), api_key=api_key)

        # Wait for the warm-up task to blow its budget, i.e. reach a terminal
        # state. This is the production sequence compressed in time, not a
        # shortcut: an operator polling /ready sees the same transition.
        deadline = time.monotonic() + _WARMUP_SETTLE_TIMEOUT_SECONDS
        while (
            getattr(client.app.state, "warmup_result", None) == WarmupResult.PENDING
            and time.monotonic() < deadline
        ):
            client.get("/health")
            time.sleep(0.05)
        assert client.app.state.warmup_result == WarmupResult.FAILED

        assert client.get("/health").status_code == 200

        ready = client.get("/ready")
        assert ready.status_code == 200, ready.text
        assert ready.json()["ready"] is True

        _install_cold_cross_encoder(monkeypatch)

        started = time.monotonic()
        resp = client.post(
            "/search",
            json={"collection": "max_test", "query": "fox"},
            headers=headers,
        )
        elapsed = time.monotonic() - started

        assert resp.status_code == 200, resp.text
        results = resp.json()["results"]
        assert results, "expected at least one result"
        assert results[0]["reranker_score"] is not None

        assert elapsed < _CLIENT_SEARCH_TIMEOUT_SECONDS, (
            f"first POST /search took {elapsed:.1f}s after GET /ready reported ready=true "
            f"on a FAILED warm-up — the cold cross-encoder build ran on the request path "
            f"(routes_search.py:362, outside the 30s search budget). A real client times "
            f"out and records status=0 body=None (S279). /ready must not report ready=true "
            f"while warm-up has failed and the models are provably cold."
        )
