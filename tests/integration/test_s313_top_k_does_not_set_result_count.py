"""S313: an explicit in-range ``top_k`` is accepted and never sets the result count.

Bug report: ``POST /search`` with an explicit ``top_k`` inside ``1..top_k_max``
must answer 200, and the number of results must be the collection's configured
``[database].top_k_return`` (default 5) regardless of the requested value —
``top_k=3`` still returns 5, ``top_k=50`` also returns 5, never 50. The
documented contract is ``Documentation/UserManual/60_searching.md`` principle 3
("Result count is server-configured. Per-request ``top_k`` is validated
(``1..[database].top_k_max``) but does not set the returned count") and the
``top_k`` row of the request-field table; the default lives in
``Documentation/UserManual/30_configuration.md`` (``top_k_return`` = 5).

The reporter recorded ``got 0: None`` / ``assert 0 == 200`` — a client-side read
timeout, the same symptom family as S278/S290 — so the test pins both halves of
the contract: a real HTTP 200, and an exact result count equal to
``top_k_return``.
"""
from __future__ import annotations

import pytest

from tests.integration.conftest import ingest_doc, make_real_app

pytestmark = [pytest.mark.integration, pytest.mark.xdist_group("s313")]

_COLLECTION = "s313docs"

#: More chunks than ``top_k_return`` so "3", "5" and "50" are distinguishable.
_DOC_COUNT = 12

#: Below the configured ``top_k_return``.
_BELOW_RETURN_TOP_K = 3

#: Above ``top_k_return``, below the default ``top_k_max`` of 100.
_ABOVE_RETURN_TOP_K = 50

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
def test_s313_explicit_top_k_returns_configured_top_k_return(
    tmp_path, monkeypatch, requested_top_k: int
) -> None:
    """Any in-range ``top_k`` → 200 and exactly ``top_k_return`` results."""
    with make_real_app(tmp_path, monkeypatch) as (client, cfg, api_key):
        headers = {"Authorization": f"Bearer {api_key}"}
        _ingest_corpus(client, api_key)

        assert cfg.top_k_return < _DOC_COUNT, (
            f"corpus of {_DOC_COUNT} chunks must exceed top_k_return={cfg.top_k_return}, "
            "otherwise the count assertion below cannot distinguish the two contracts"
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
        assert len(results) == cfg.top_k_return, (
            f"POST /search with top_k={requested_top_k} returned {len(results)} results; the "
            f"per-request top_k must not set the returned count — the pipeline must return "
            f"[database].top_k_return={cfg.top_k_return} results "
            "(Documentation/UserManual/60_searching.md, principle 3)."
        )
