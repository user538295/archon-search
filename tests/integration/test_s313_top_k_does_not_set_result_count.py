"""S313, rewritten by BE-26: an explicit in-range ``top_k`` is accepted and sets the result count.

The bug report behind this file: ``POST /search`` with an explicit ``top_k`` inside ``1..top_k_max`` must answer 200
(the reporter recorded ``got 0: None`` / ``assert 0 == 200``, a client-side read timeout, the same symptom family as
S278/S290). Until BE-26 the pipeline took no ``top_k`` and the count was always ``[database].top_k_return``; since
BE-26 the request's ``top_k`` is the number of results (``top_k=3`` returns 3, ``top_k=9`` returns 9); a request without
one still returns ``top_k_return`` (default 5), which ``test_be26_top_k_cutover.py`` pins. This file pins a real HTTP 200
and the exact count.
"""
from __future__ import annotations

import pytest

from tests.integration.conftest import ingest_doc, make_real_app

pytestmark = [pytest.mark.integration, pytest.mark.xdist_group("s313")]

_COLLECTION = "s313docs"

#: More chunks than any requested ``top_k`` below, so each request has enough candidates to return exactly N.
_DOC_COUNT = 12

#: Below the configured ``top_k_return``.
_BELOW_RETURN_TOP_K = 3

#: Above ``top_k_return``, still within the corpus.
_ABOVE_RETURN_TOP_K = 9

_QUERY = "aurora"


def _ingest_corpus(client, api_key: str) -> None:
    for i in range(_DOC_COUNT):
        ingest_doc(
            client,
            _COLLECTION,
            f"Aurora borealis observation number {i}: the aurora glowed over the northern sky.",
            f"/corpus/aurora_{i}.txt",
            api_key=api_key,
        )


@pytest.mark.parametrize("requested_top_k", [_BELOW_RETURN_TOP_K, _ABOVE_RETURN_TOP_K])
def test_s313_explicit_top_k_sets_the_result_count(
    tmp_path, monkeypatch, requested_top_k: int
) -> None:
    """Any in-range ``top_k`` → 200 and exactly that many results."""
    with make_real_app(tmp_path, monkeypatch) as (client, cfg, api_key):
        headers = {"Authorization": f"Bearer {api_key}"}
        _ingest_corpus(client, api_key)

        assert _BELOW_RETURN_TOP_K < cfg.top_k_return < _ABOVE_RETURN_TOP_K < _DOC_COUNT, (
            "the requested counts must straddle top_k_return and fit the corpus, "
            "otherwise the count assertion below cannot tell the request from the default"
        )

        resp = client.post(
            "/search",
            json={"collection": _COLLECTION, "query": _QUERY, "top_k": requested_top_k},
            headers=headers,
        )

        assert resp.status_code == 200, (
            f"Expected 200 for top_k={requested_top_k} inside 1..top_k_max="
            f"{cfg.top_k_max}, got {resp.status_code}: {resp.text}"
        )
        results = resp.json()["results"]
        assert len(results) == requested_top_k, (
            f"POST /search with top_k={requested_top_k} returned {len(results)} results; the per-request "
            "top_k must set the returned count (the configured top_k_return only applies when it is omitted)."
        )
