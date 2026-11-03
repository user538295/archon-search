"""``SearchService.search`` over the real app's pipeline, store and embedder cache (no mocks)."""
from __future__ import annotations

from pathlib import Path

import pytest

from archon_search.constants import DEFAULT_NAMESPACE
from archon_search.filters import SearchFilters
from archon_search.services.authz import AuthIdentity
from archon_search.services.search_command import SearchCommand
from archon_search.services.search_service import SearchService
from tests.integration.conftest import ingest_file_via_path, make_real_app

pytestmark = pytest.mark.integration

_COLLECTION = "svc-col"
_QUERY = "quick brown fox"
_MATCHING_GLOB = "*fox.md"
_NO_MATCH_GLOB = "/no/such/dir/that/matches/nothing/*"
_DOC_TEXT = (
    "# Fox notes\n\n"
    "The quick brown fox jumps over the lazy dog.\n"
    "A quick brown fox is quick, and the brown fox is a fox.\n"
)


def test_single_path_derived_fields_and_acl_gate_against_a_real_pipeline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    doc = tmp_path / "corpus" / "fox.md"
    doc.parent.mkdir()
    doc.write_text(_DOC_TEXT)

    with make_real_app(tmp_path, monkeypatch) as (client, config, api_key):
        ingest_file_via_path(client, _COLLECTION, str(doc), api_key=api_key)
        state = client.app.state
        service = SearchService(state.pipeline, state.config, embedder_cache=state.embedder_cache)
        caller = AuthIdentity(namespace=DEFAULT_NAMESPACE, admin=True)

        def search(**command_fields: object):
            command = SearchCommand(query=_QUERY, collection=_COLLECTION, **command_fields)  # type: ignore[arg-type]
            return client.portal.call(service.search, command, caller)

        plain = search()
        gated = search(acl_context=True, filters=SearchFilters(source_path_glob=_MATCHING_GLOB))
        filtered_out = search(filters=SearchFilters(source_path_glob=_NO_MATCH_GLOB))

    assert plain.results, "the ingested document must be found"
    assert all(hit.acl_gate is None for hit in plain.results), "acl_gate only when acl_context was requested"
    assert plain.applied_filters is None
    assert plain.embedding_model == config.embedding_model
    assert plain.include_metadata is False
    assert gated.results
    assert all(hit.acl_gate is not None and hit.acl_gate.warnings == [] for hit in gated.results)
    assert gated.applied_filters is not None
    assert gated.applied_filters.source_path_glob == _MATCHING_GLOB
    assert filtered_out.results == (), "the filter must reach the pipeline"
