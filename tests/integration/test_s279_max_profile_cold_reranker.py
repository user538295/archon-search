"""S279: the first POST /search on a max-profile server must not block on a cold reranker.

Reproduced against a real ``uv run archon-search serve`` (max profile:
``BAAI/bge-large-en-v1.5`` + ``BAAI/bge-reranker-base``, ``chunk_size = 1024``,
cold model cache):

===========================  ==========  ============
step                         wall clock  result
===========================  ==========  ============
GET /health                  +4 s        200
GET /ready                   +7 s        200 ``{"ready": true, "checks": {"models": "fail", ...}}``
POST /search (first call)    +91.2 s     200
POST /search (second call)   +0.03 s     200
===========================  ==========  ============

``/ready`` answers ``ready: true`` while both ONNX models are still un-built, so a
client that follows the documented sequence sends its first search into a 91-second
cold cross-encoder download+build that runs **on the request path**. Any HTTP client
with a normal read timeout gets no response at all — the ``status=0 body=None`` in the
ticket. The gate exists (``_warmup_pending`` in ``server/routes_ready.py``) but is armed
only by ``eager_load_embedders``, which defaults to ``False``
(``config.py:286``) and is never set by ``archon-search install --profile max``
(``install/config_writer.py:31``, which writes only embedder/reranker/chunk_size).

The cold load is simulated here (``_ColdLoadTextCrossEncoder``) so the test stays
deterministic and weight-free; the ratio, not the absolute number, is the point.
"""
from __future__ import annotations

import sys
import time

import pytest

from archon_search.profiles import ENGLISH_PROFILES
from tests.integration.conftest import ingest_file_via_path, make_real_app

pytestmark = [pytest.mark.integration, pytest.mark.xdist_group("s279")]

_MAX_PROFILE = ENGLISH_PROFILES["max"]

#: Stand-in for the measured 91.2 s cold download+build of ``BAAI/bge-reranker-base``.
_COLD_CROSS_ENCODER_LOAD_SECONDS = 4.0

#: Budget a normal HTTP client allows a single search. Overrunning it is what the
#: reporter saw as ``status=0 body=None`` — a client-side read timeout, no response.
_CLIENT_SEARCH_TIMEOUT_SECONDS = 2.0

#: Ceiling for waiting out the backgrounded startup warm-up before the first search.
#: Generous because the whole point of the fix is that this window exists at all.
_READY_POLL_TIMEOUT_SECONDS = 30.0

_MAX_PROFILE_TOML = f"""
[database]
embedding_model = "{_MAX_PROFILE.embedder}"
reranker_model = "{_MAX_PROFILE.reranker}"
chunk_size = {_MAX_PROFILE.chunk_size}
profile = "max"
multilingual = false
"""


def _install_cold_cross_encoder(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make cross-encoder construction cost ``_COLD_CROSS_ENCODER_LOAD_SECONDS``.

    ``ModelReranker.predict`` imports ``TextCrossEncoder`` from
    ``fastembed.rerank.cross_encoder`` on its first call (``reranker.py``), so
    patching the class there puts the cost exactly where the real ONNX build sits:
    inside the first search request.
    """
    module = sys.modules["fastembed.rerank.cross_encoder"]
    base = module.TextCrossEncoder

    class _ColdLoadTextCrossEncoder(base):  # type: ignore[misc, valid-type]
        def __init__(self, *args: object, **kwargs: object) -> None:
            time.sleep(_COLD_CROSS_ENCODER_LOAD_SECONDS)
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(module, "TextCrossEncoder", _ColdLoadTextCrossEncoder)


def test_s279_first_search_after_ready_does_not_pay_cold_reranker_load(tmp_path, monkeypatch):
    """/health 200 then /ready 200 must mean the next /search is not a cold model build."""
    doc = tmp_path / "doc.md"
    doc.write_text("# max_test\nThe quick brown fox jumps over the lazy dog.\n")
    data_dir = tmp_path / "app"
    data_dir.mkdir()

    with make_real_app(data_dir, monkeypatch, toml_content=_MAX_PROFILE_TOML) as (
        client,
        cfg,
        api_key,
    ):
        assert cfg.chunk_size == _MAX_PROFILE.chunk_size
        assert cfg.reranker_model == _MAX_PROFILE.reranker
        headers = {"Authorization": f"Bearer {api_key}"}

        ingest_file_via_path(client, "max_test", str(doc), api_key=api_key)

        assert client.get("/health").status_code == 200
        # Poll /ready exactly as the documented client sequence does: warm-up is a
        # background task (the lifespan may never await it), so a single call can
        # legitimately catch a 503 under parallel-xdist load. Polling cannot mask the
        # bug — before the fix /ready answered 200 on the first call and the timing
        # assertion below still failed.
        ready_deadline = time.monotonic() + _READY_POLL_TIMEOUT_SECONDS
        while True:
            ready = client.get("/ready")
            if ready.status_code == 200 or time.monotonic() >= ready_deadline:
                break
            time.sleep(0.05)
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
            f"first POST /search took {elapsed:.1f}s after GET /ready reported ready=true — "
            f"the cold cross-encoder build ran on the request path. A real client times out "
            f"and records status=0 body=None (S279). Either /ready must stay 503 until the "
            f"reranker is warm, or the max profile must warm it off the request path."
        )
