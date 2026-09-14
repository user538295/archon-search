"""S290: a restarted server must answer a within-ceiling ``/search`` , not park the connection.

Bug (``Documentation/Backlog/S290-serve_accepts_top_k_within_new_ceiling.md``).
After ``archon-search config set search.top_k_max 10`` and a restart of the
isolated ``archon-search serve`` process, ``POST /search {"top_k": 50}`` is
correctly rejected with 422 — the restarted server did pick the new ceiling up —
but the very next ``POST /search {"top_k": 5}``, which is *within* that ceiling,
produces no HTTP response at all inside a normal client read budget. The reporter
recorded it as ``status=0`` / ``body=None``
(``AssertionError: Expected 200 for top_k=5 at/below the new ceiling of 10, got
0: None`` / ``assert 0 == 200``) — the same symptom family as
S278/S279/S281/S283/S286/S288.

The asymmetry between the two requests is the fingerprint: the ``top_k`` ceiling
check in ``server/routes_search.py`` ``search()`` runs *before* any model is
touched, so the over-ceiling request answers instantly, while the within-ceiling
one goes on to ``warmup_for_search`` and blocks.

Root cause. A restart leaves every ONNX model cold in the new process.
``_search_budget.warmup_for_search`` bounds only the *reranker* leg
(``RERANKER_WARMUP_WAIT_SECONDS``); ``SearchPipeline.warmup_models`` awaits
``target_embedder.warmup()`` with no timeout at all (``pipeline.py``, "The
embedder leg is deliberately unbounded"), and that await sits *outside*
``run_within_budget`` — so nothing caps it. A cold embedder build therefore holds
the connection open for as long as the build takes, past any client read timeout,
and the client records status 0 with no body. S286/S288 closed exactly this hole
for the cross-encoder on the single-collection and fan-out branches; the embedder
leg on the same branches is still open, and a restart is what makes it cold.

Fixed looks like: a search that cannot get a query vector in time ends inside the
per-request budget with a real HTTP response (the 503/504 the error taxonomy
already defines for an unavailable backend) instead of parking the connection —
so a client sees a status code, never a read timeout.

The build is *simulated* (``_ColdLoadTextEmbedding``) so the test stays
deterministic and weight-free, as in
``test_s288_multi_collection_search_cold_reranker.py``. The ratio is the point,
not the absolute numbers: a real cold embedder download+ONNX build on a
restarted server outruns a client's read budget exactly the way
``_COLD_EMBEDDER_LOAD_SECONDS`` outruns ``_CLIENT_SEARCH_TIMEOUT_SECONDS`` here.
"""
from __future__ import annotations

import sys
import time

import pytest
from click.testing import CliRunner

from archon_search.cli.config_cmd import config as config_group
from tests.integration.conftest import ingest_file_via_path, make_real_app

pytestmark = [pytest.mark.integration, pytest.mark.xdist_group("s290")]

#: The operator-configured ceiling the scenario writes via ``config set``.
_NEW_TOP_K_CEILING = 10

#: Over the new ceiling (but under the 100 default), so a 422 here proves the
#: restarted process actually re-read the TOML rather than keeping the default.
_OVER_CEILING_TOP_K = 50

#: At/below the new ceiling — the request the reporter never got a response to.
_WITHIN_CEILING_TOP_K = 5

#: Stand-in for the cold download+ONNX build of the query embedder in a freshly
#: restarted process, compressed. Must outlive the client budget below.
_COLD_EMBEDDER_LOAD_SECONDS = 6.0

#: Budget a normal HTTP client allows a single search. Overrunning it is what the
#: reporter saw as ``status=0 body=None`` — a client-side read timeout, no response.
_CLIENT_SEARCH_TIMEOUT_SECONDS = 2.0

_COLLECTION = "s290col"

_FILE_TEXT = (
    "Archon Search is a hybrid retrieval server that combines dense vector "
    "search with full-text search over a document collection."
)


def _install_cold_embedder(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make query-embedder construction cost ``_COLD_EMBEDDER_LOAD_SECONDS``.

    ``FastEmbedBackend.encode`` does ``from fastembed import TextEmbedding`` on its
    first call (``archon_search/embedder.py``), so patching the class on the
    ``fastembed`` module puts the cost exactly where the real ONNX build sits.
    """
    module = sys.modules["fastembed"]
    base = module.TextEmbedding

    class _ColdLoadTextEmbedding(base):  # type: ignore[misc, valid-type]
        def __init__(self, *args: object, **kwargs: object) -> None:
            time.sleep(_COLD_EMBEDDER_LOAD_SECONDS)
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(module, "TextEmbedding", _ColdLoadTextEmbedding)


def test_s290_restarted_server_answers_within_ceiling_top_k(tmp_path, monkeypatch) -> None:
    """After ``config set`` + restart: 422 over the ceiling, a *timely* 200 within it."""
    data_dir = tmp_path / "app"
    data_dir.mkdir()
    doc = tmp_path / "doc.txt"
    doc.write_text(_FILE_TEXT)

    # --- The run before the restart: a corpus exists on disk. -----------------
    with make_real_app(data_dir, monkeypatch) as (client, _cfg, api_key):
        ingest_file_via_path(client, _COLLECTION, str(doc), api_key=api_key)

    # --- Steps 1-2: the real CLI writes and reads back the new ceiling. -------
    config_path = data_dir / "archon-search.toml"
    runner = CliRunner()
    set_result = runner.invoke(
        config_group,
        ["set", "search.top_k_max", str(_NEW_TOP_K_CEILING), "--config", str(config_path)],
    )
    assert set_result.exit_code == 0, f"config set failed: {set_result.output}"
    get_result = runner.invoke(
        config_group, ["get", "search.top_k_max", "--config", str(config_path)]
    )
    assert get_result.exit_code == 0, f"config get failed: {get_result.output}"
    assert get_result.output.strip() == str(_NEW_TOP_K_CEILING), (
        f"config get did not read back the value just written: {get_result.output!r}"
    )

    # Every model is cold in the restarted process — install the simulated build
    # before the second app starts, so the cost lands where production puts it.
    _install_cold_embedder(monkeypatch)

    # --- Step 3: restart, re-reading the TOML the CLI just wrote. -------------
    toml_content = config_path.read_text(encoding="utf-8")
    with make_real_app(data_dir, monkeypatch, toml_content=toml_content) as (client, cfg, api_key):
        headers = {"Authorization": f"Bearer {api_key}"}
        assert cfg.top_k_max == _NEW_TOP_K_CEILING, (
            f"restarted server did not load the configured ceiling: {cfg.top_k_max}"
        )
        # Pin the precondition: a warm embedder would make the timing assertion
        # below pass vacuously, exercising nothing.
        assert not client.app.state.pipeline.embedder_is_warm, (
            "the query embedder warmed up before the search was issued, so this run would not "
            "exercise the cold-embedder path at all — raise _COLD_EMBEDDER_LOAD_SECONDS rather "
            "than letting the test pass vacuously."
        )

        # --- Step 4: over the ceiling -> 422, and immediately (no model touched).
        over = client.post(
            "/search",
            json={"collection": _COLLECTION, "query": "hybrid retrieval", "top_k": _OVER_CEILING_TOP_K},
            headers=headers,
        )
        assert over.status_code == 422, (
            f"Expected 422 for top_k={_OVER_CEILING_TOP_K} above the new ceiling of "
            f"{_NEW_TOP_K_CEILING}, got {over.status_code}: {over.text}"
        )

        # --- Step 5: within the ceiling -> a 200 inside a client read budget. --
        started = time.monotonic()
        within = client.post(
            "/search",
            json={"collection": _COLLECTION, "query": "hybrid retrieval", "top_k": _WITHIN_CEILING_TOP_K},
            headers=headers,
        )
        elapsed = time.monotonic() - started

        assert within.status_code == 200, (
            f"Expected 200 for top_k={_WITHIN_CEILING_TOP_K} at/below the new ceiling of "
            f"{_NEW_TOP_K_CEILING}, got {within.status_code}: {within.text}"
        )
        assert within.json()["results"], "expected at least one result"
        assert elapsed < _CLIENT_SEARCH_TIMEOUT_SECONDS, (
            f"POST /search with top_k={_WITHIN_CEILING_TOP_K} took {elapsed:.1f}s on a restarted "
            f"server whose embedder is still cold. warmup_for_search bounds only the reranker leg; "
            f"SearchPipeline.warmup_models awaits the embedder warm-up with no timeout, outside "
            f"run_within_budget, so the request blocks for the whole ONNX build. A real client "
            f"times out first and records status=0 body=None (S290). The request must end inside "
            f"the per-request search budget with an HTTP response instead of parking the connection."
        )
