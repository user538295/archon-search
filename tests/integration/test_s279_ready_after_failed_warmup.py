"""S279: a *failed* startup warm-up must still leave the cross-encoder warm.

The sibling repro (``test_s279_max_profile_cold_reranker.py``) pins the
*pending* case: ``/ready`` stays 503 while the warm-up task is running.
A *failed* warm-up never gates readiness — ``routes_ready.py`` computes::

    ready_flag = storage_ok and models_status is not CheckStatus.PENDING and not sync_pending

and a failed warm-up maps ``models_status`` to ``FAIL`` before ``model_validation`` is
even consulted (S631) — so once warm-up lands on ``WarmupResult.FAILED`` (here: it blew its
``_WARMUP_TASK_TIMEOUT_SECONDS`` budget, 600 s in ``server/warmup.py``) ``/ready``
answers 200 with ``ready: true`` and reports the failure only through
``checks.models: "fail"`` and a ``model warm-up failed: timed out ...`` WARNING
naming the models still cold. This test pins what that 200 must still guarantee:
the next ``POST /search`` is not a cold cross-encoder build.

That budget is realistic to exceed under the ``max`` profile precisely because
it is the heavy one: ``BAAI/bge-large-en-v1.5`` (~1.3 GB) is downloaded and
built by ``embedder_cache.preload``, which alone can consume the whole window.
The original ordering ran that preload *before* ``pipeline.warmup_models()``, so
the cross-encoder was never warmed at all; the fix warms the cross-encoder
first, leaving only an embedder cold when the budget blows.

Without that ordering the first ``POST /search`` paid the cold cross-encoder
build on the request path, outside the route's ``_SEARCH_TIMEOUT_SECONDS``
(30 s) budget and bounded only by ``Reranker._WARMUP_TIMEOUT_SECONDS`` (300 s,
``reranker.py``). The measured cold build of ``BAAI/bge-reranker-base`` is
91 s, so the client got no response at all: ``status=0 body=None``.

Both slow loads are simulated here so the test stays deterministic and
weight-free; the mechanism — same code path, same ordering — is the point.
"""
from __future__ import annotations

import asyncio
import logging
import time

import pytest

from archon_search.embedder_cache import EmbedderCache
from archon_search.profiles import ENGLISH_PROFILES
from archon_search.server import warmup as warmup_module
from archon_search.server.schemas import WarmupResult
from tests._cold_model_builds import install_cold_reranker_build
from tests.integration.conftest import ingest_file_via_path, make_real_app

pytestmark = [pytest.mark.integration, pytest.mark.xdist_group("s279")]

_MAX_PROFILE = ENGLISH_PROFILES["max"]

#: Stand-in for the cold download+build of ``BAAI/bge-large-en-v1.5`` — long
#: enough to exhaust the warm-up budget below, as the real ~1.3 GB fetch does.
_SLOW_EMBEDDER_PRELOAD_SECONDS = 5.0

#: Stand-in for ``_WARMUP_TASK_TIMEOUT_SECONDS`` (600 s in production).
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

    ``run_model_warmup`` awaits ``pipeline.warmup_models()`` first and
    ``embedder_cache.preload`` second, both inside one ``asyncio.timeout``
    (``server/warmup.py``) — so an over-budget preload lands ``warmup_result`` on
    ``FAILED`` with the cross-encoder already warm. Reverse those two awaits and
    the reranker is never warmed, which is exactly what this test detects.
    """

    async def _slow_preload(self: EmbedderCache, model_names: list[str]) -> None:
        await asyncio.sleep(_SLOW_EMBEDDER_PRELOAD_SECONDS)

    monkeypatch.setattr(EmbedderCache, "preload", _slow_preload)
    monkeypatch.setattr(warmup_module, "_WARMUP_TASK_TIMEOUT_SECONDS", _WARMUP_BUDGET_SECONDS)


def test_s279_ready_200_after_failed_warmup_still_hides_a_cold_reranker(tmp_path, monkeypatch, caplog):
    """A 200 from /ready must mean the next /search is not a cold model build."""
    doc = tmp_path / "doc.md"
    doc.write_text("# max_test\nThe quick brown fox jumps over the lazy dog.\n")
    data_dir = tmp_path / "app"
    data_dir.mkdir()

    _install_slow_embedder_preload(monkeypatch)
    caplog.set_level(logging.WARNING, logger=warmup_module.__name__)

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
        # The timeout WARNING lists what it left cold (S632: never an empty reason).
        # The cross-encoder warmed before the budget blew, so it is never listed; the
        # embedder may or may not be — the ingest above can load it meanwhile, and with
        # nothing cold the "; still cold: ..." suffix is omitted.
        timeout_prefix = f"model warm-up failed: timed out after {_WARMUP_BUDGET_SECONDS}s"
        timeout_messages = [m for m in caplog.messages if m.startswith(timeout_prefix)]
        assert len(timeout_messages) == 1, caplog.messages
        assert _MAX_PROFILE.reranker not in timeout_messages[0]

        assert client.get("/health").status_code == 200

        ready = client.get("/ready")
        assert ready.status_code == 200, ready.text
        assert ready.json()["ready"] is True

        install_cold_reranker_build(monkeypatch, _COLD_CROSS_ENCODER_LOAD_SECONDS)

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
            f"on a FAILED warm-up — the cold cross-encoder build ran on the request path, "
            f"outside the 30s search budget. A real client times out and records "
            f"status=0 body=None (S279). A failed warm-up must still have warmed the "
            f"cross-encoder before the embedder preload blew the budget."
        )
