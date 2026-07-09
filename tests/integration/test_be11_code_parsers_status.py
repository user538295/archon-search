"""E2g BE-11 — e2e: server starts with graph.enabled=True and [code] extras missing.

Scenario: S9 — Given graph.enabled=true but [code] extras (tree-sitter parsers) are
missing, When the server starts, Then it starts successfully, prose graphing works,
code graphing is skipped, and a one-time WARNING plus a health/status field name
the fix.

Also hosts T-1 (`test_e2e_gracefulDegradation_missingCodeParsers`) — the tester-role
e2e re-verification of S9 that additionally exercises a prose graph *query*
(POST /search with graph_mode="naive") through to a verified graph expansion,
not just entity extraction at ingest time.

This is a TestClient-based e2e test exercising the full application stack:
- graph enabled in config
- real LanceDB store + GraphStore
- stubbed spaCy returning a named entity for any text (prose graphing)
- tree-sitter's Python grammar simulated absent (code graphing degrades softly)
- ingest of both a code file and a prose file
- GET /status reflects the degraded `code_parsers` field
"""
from __future__ import annotations

import sys
import types
from pathlib import Path

import pytest

from tests.integration.conftest import ingest_file_via_path, make_real_app

pytestmark = pytest.mark.integration


def _auth(api_key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {api_key}"}


def _install_spacy_stub(monkeypatch: pytest.MonkeyPatch) -> None:
    """Install a fake spaCy package returning one named entity for any text.

    Must be installed before make_real_app because create_app calls
    _check_graph_deps which does ``import spacy``.
    """

    class _FakeEnt:
        def __init__(self, text: str, label: str) -> None:
            self.text = text
            self.label_ = label

    class _FakeDoc:
        def __init__(self) -> None:
            self.ents = [_FakeEnt("Alice", "PERSON")]

    class _FakeNLP:
        def __call__(self, text: str) -> _FakeDoc:
            return _FakeDoc()

    nlp_instance = _FakeNLP()

    fake_util = types.ModuleType("spacy.util")
    fake_util.get_installed_models = lambda: ["en_core_web_sm"]  # type: ignore[attr-defined]

    fake_cli = types.ModuleType("spacy.cli")
    fake_cli.download = lambda model: None  # type: ignore[attr-defined]

    fake_spacy = types.ModuleType("spacy")
    fake_spacy.load = lambda model: nlp_instance  # type: ignore[attr-defined]
    fake_spacy.util = fake_util  # type: ignore[attr-defined]
    fake_spacy.cli = fake_cli  # type: ignore[attr-defined]

    monkeypatch.setitem(sys.modules, "spacy", fake_spacy)
    monkeypatch.setitem(sys.modules, "spacy.util", fake_util)
    monkeypatch.setitem(sys.modules, "spacy.cli", fake_cli)


def test_serverStarts_whenCodeParsersMissing_graphEnabled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Server starts, /status reports the degraded code_parsers field, and prose
    graphing still works — even with graph.enabled=True and tree-sitter's Python
    grammar unavailable.
    """
    import archon_search.code_enricher as ce

    # Isolate this test's grammar-registry state from any other test/process
    # activity on the same xdist worker — both are process-global module dicts.
    monkeypatch.setattr(ce, "_GRAMMAR_CACHE", {})
    monkeypatch.setattr(ce, "_GRAMMAR_LOGGED", set())

    # Simulate tree-sitter's Python grammar package being absent (established
    # stub pattern — see test_grammar_warning_logged_once in test_code_enricher.py).
    monkeypatch.setitem(sys.modules, "tree_sitter_python", None)

    _install_spacy_stub(monkeypatch)

    col = "be11-code-parsers-missing"
    code_doc = tmp_path / "sample.py"
    code_doc.write_text(
        "def foo():\n    return 1\n\n\nclass Bar:\n    def baz(self):\n        pass\n",
        encoding="utf-8",
    )
    prose_doc = tmp_path / "prose.txt"
    prose_doc.write_text(
        "Alice is a software engineer.\n" * 10,
        encoding="utf-8",
    )

    with make_real_app(tmp_path, monkeypatch, graph_enabled=True) as (client, _cfg, api_key):
        # Server started; /health confirms it before any ingest.
        health = client.get("/health")
        assert health.status_code == 200, f"/health failed: {health.status_code} {health.text}"

        # Ingest the code file — this trips CodeEnricher.prepare(".py") →
        # _get_grammar(".py") → grammar missing → WARNING logged, ingest still
        # completes (code graphing is skipped, not a hard failure).
        ingest_file_via_path(client, col, str(code_doc), api_key=api_key)

        # Ingest a plain prose file — proves prose graphing still works via the
        # spaCy stub, independent of the code-parser degradation.
        ingest_file_via_path(client, col, str(prose_doc), api_key=api_key)

        resp = client.get("/status", headers=_auth(api_key))
        assert resp.status_code == 200, f"GET /status failed: {resp.status_code} {resp.text}"
        data = resp.json()

        assert "code_parsers" in data, (
            f"'code_parsers' key missing from status response: {list(data.keys())}"
        )
        code_parsers = data["code_parsers"]
        assert code_parsers is not None, (
            "code_parsers sub-object is None even though graph.enabled=True"
        )
        assert code_parsers["degraded"] is True
        assert ".py" in code_parsers["missing_extensions"]
        assert code_parsers["message"], "message should name the fix when degraded"

        # Prose graphing still works: the ingested collection has a graph entry
        # with node_count > 0 from the spaCy-stub-extracted entity.
        assert "graph" in data
        graph = data["graph"]
        assert graph is not None
        col_entries = [c for c in graph["collections"] if c["collection"] == col]
        assert len(col_entries) == 1, (
            f"Expected 1 entry for collection {col!r} in graph.collections, "
            f"got: {graph['collections']}"
        )
        assert col_entries[0]["node_count"] > 0, (
            "Expected node_count > 0 from prose graphing (spaCy stub entity), "
            f"got: {col_entries[0]['node_count']}"
        )


def _install_spacy_stub_multi_entity(monkeypatch: pytest.MonkeyPatch) -> None:
    """Install a fake spaCy recognizing "Alice" and "Google" (content-dependent).

    Unlike ``_install_spacy_stub`` (one hardcoded entity for ANY text — which
    would also tag the ingested `.py` code file with a bogus "Alice" entity,
    and can never form a co-occurrence edge since it never emits two entities
    together), this stub only tags entity names that literally appear in the
    chunk. Two co-occurring entities are required so `graph_extractor.py`'s
    `itertools.combinations` pairwise-edge builder actually produces an edge —
    without an edge, `GraphExpander.expand`'s neighbour lookup is always empty
    and `graph_mode="naive"` degrades to an unverifiable no-op (see
    test_e1a_t3_graph_error_paths_e2e.py's `_install_spacy_stub_with_entities`
    for the same pattern applied to graph_mode roundtrip tests).
    """

    class _FakeEnt:
        def __init__(self, text: str, label: str) -> None:
            self.text = text
            self.label_ = label

    class _FakeDoc:
        def __init__(self, ents: list) -> None:
            self.ents = ents

    _ENTITY_MAP = [("Alice", "PERSON"), ("Google", "ORG")]

    class _FakeNLP:
        def __call__(self, text: str) -> _FakeDoc:
            ents = [
                _FakeEnt(name, label) for name, label in _ENTITY_MAP if name in text
            ]
            return _FakeDoc(ents)

    nlp_instance = _FakeNLP()

    fake_util = types.ModuleType("spacy.util")
    fake_util.get_installed_models = lambda: ["en_core_web_sm"]  # type: ignore[attr-defined]

    fake_cli = types.ModuleType("spacy.cli")
    fake_cli.download = lambda model: None  # type: ignore[attr-defined]

    fake_spacy = types.ModuleType("spacy")
    fake_spacy.load = lambda model: nlp_instance  # type: ignore[attr-defined]
    fake_spacy.util = fake_util  # type: ignore[attr-defined]
    fake_spacy.cli = fake_cli  # type: ignore[attr-defined]

    monkeypatch.setitem(sys.modules, "spacy", fake_spacy)
    monkeypatch.setitem(sys.modules, "spacy.util", fake_util)
    monkeypatch.setitem(sys.modules, "spacy.cli", fake_cli)


def test_e2e_gracefulDegradation_missingCodeParsers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """T-1: server starts and prose graphing works when code parsers are missing;
    health/status names the fix.

    Extends test_serverStarts_whenCodeParsersMissing_graphEnabled by actually
    executing a prose graph *query* (POST /search with graph_mode="naive") and
    asserting expansion fired (``graph_expansion_applied=True`` and a non-empty
    result set) — not just that entity extraction produced graph nodes at
    ingest time, and not just that the endpoint returns 200 (which it would
    even if expansion silently no-opped).
    """
    import archon_search.code_enricher as ce

    # Isolate this test's grammar-registry state from any other test/process
    # activity on the same xdist worker — both are process-global module dicts.
    monkeypatch.setattr(ce, "_GRAMMAR_CACHE", {})
    monkeypatch.setattr(ce, "_GRAMMAR_LOGGED", set())

    # Simulate tree-sitter's Python grammar package being absent.
    monkeypatch.setitem(sys.modules, "tree_sitter_python", None)

    # Two co-occurring entities (Alice, Google) are required so the graph gets
    # a real edge — a single-entity stub (as the sibling test uses) can never
    # produce a co-occurrence edge, making graph_mode="naive" an unverifiable
    # no-op. See _install_spacy_stub_multi_entity's docstring.
    _install_spacy_stub_multi_entity(monkeypatch)

    col = "e2g-t1-graceful-degradation"
    code_doc = tmp_path / "sample.py"
    code_doc.write_text(
        "def foo():\n    return 1\n\n\nclass Bar:\n    def baz(self):\n        pass\n",
        encoding="utf-8",
    )
    prose_doc = tmp_path / "prose.txt"
    prose_doc.write_text(
        "Alice is a senior engineer at Google.\n" * 10,
        encoding="utf-8",
    )

    with make_real_app(tmp_path, monkeypatch, graph_enabled=True) as (client, _cfg, api_key):
        # Server started (200 startup).
        health = client.get("/health")
        assert health.status_code == 200, f"/health failed: {health.status_code} {health.text}"

        # Ingest a code file — trips the missing-grammar soft-degrade path.
        # The stub is content-dependent (only matches "Alice"/"Google" literally
        # appearing in the text), so this code file contributes no bogus entities.
        ingest_file_via_path(client, col, str(code_doc), api_key=api_key)

        # Ingest a prose file — populates the graph with co-occurring "Alice" and
        # "Google" entities (and a co-occurrence edge between them) for the query
        # below to expand against.
        ingest_file_via_path(client, col, str(prose_doc), api_key=api_key)

        # Prose graphing works: node_count > 0 proves entity extraction actually
        # populated the graph (mirrors test_serverStarts_whenCodeParsersMissing_graphEnabled).
        status_before = client.get("/status", headers=_auth(api_key))
        assert status_before.status_code == 200
        graph_before = status_before.json()["graph"]
        assert graph_before is not None
        col_entries = [c for c in graph_before["collections"] if c["collection"] == col]
        assert len(col_entries) == 1, (
            f"Expected 1 entry for collection {col!r} in graph.collections, "
            f"got: {graph_before['collections']}"
        )
        assert col_entries[0]["node_count"] > 0, (
            "Expected node_count > 0 from prose graphing (spaCy stub entities), "
            f"got: {col_entries[0]['node_count']}"
        )

        # Prose graph query succeeds AND actually exercises graph-based query
        # expansion: graph_expansion_applied=True proves the "Alice" query token
        # matched the ingested entity and its "Google" neighbour was found —
        # not just that the endpoint returned 200 (which a silent no-op would too).
        search_resp = client.post(
            "/search",
            json={"collection": col, "query": "Alice", "graph_mode": "naive"},
            headers=_auth(api_key),
        )
        assert search_resp.status_code == 200, (
            f"POST /search graph_mode='naive' failed despite missing code parsers: "
            f"{search_resp.status_code} {search_resp.text}"
        )
        search_data = search_resp.json()
        assert "results" in search_data, (
            f"search response missing 'results' key: {list(search_data.keys())}"
        )
        assert len(search_data["results"]) > 0, (
            f"Expected non-empty results for 'Alice' query, got: {search_data['results']}"
        )
        assert "graph_expansion_applied" in search_data, (
            f"'graph_expansion_applied' key missing from response: {list(search_data.keys())}"
        )
        assert search_data["graph_expansion_applied"] is True, (
            "Expected graph_expansion_applied=True — the 'Alice' query token should "
            "match the ingested entity and expand via its 'Google' co-occurrence "
            f"neighbour, even with code parsers missing. Full response: {search_data}"
        )

        # /status names the fix: degraded code_parsers with an actionable message.
        resp = client.get("/status", headers=_auth(api_key))
        assert resp.status_code == 200, f"GET /status failed: {resp.status_code} {resp.text}"
        data = resp.json()

        assert "code_parsers" in data, (
            f"'code_parsers' key missing from status response: {list(data.keys())}"
        )
        code_parsers = data["code_parsers"]
        assert code_parsers is not None, (
            "code_parsers sub-object is None even though graph.enabled=True"
        )
        assert code_parsers["degraded"] is True, (
            f"Expected degraded=True with tree-sitter's Python grammar missing, "
            f"got: {code_parsers}"
        )
        assert ".py" in code_parsers["missing_extensions"], (
            f"Expected '.py' in missing_extensions, got: {code_parsers['missing_extensions']}"
        )
        assert "archon-search[code]" in code_parsers["message"], (
            f"message should name the concrete fix (the [code] extras install), "
            f"got: {code_parsers['message']!r}"
        )


def test_codeParsersStatus_isNull_whenGraphDisabled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """graph.enabled=False → /status.code_parsers is null, even with a missing grammar (C1-T-1).

    The code_parsers soft-degrade check is only meaningful alongside code
    graphing; it must not report anything when graph is off, regardless of
    whether tree-sitter grammars are actually missing.
    """
    import archon_search.code_enricher as ce

    # Isolate this test's grammar-registry state from other tests/process activity
    # on the same xdist worker — both are process-global module dicts.
    monkeypatch.setattr(ce, "_GRAMMAR_CACHE", {})
    monkeypatch.setattr(ce, "_GRAMMAR_LOGGED", set())

    # Simulate tree-sitter's Python grammar package being absent.
    monkeypatch.setitem(sys.modules, "tree_sitter_python", None)

    col = "be11-graph-disabled"
    code_doc = tmp_path / "sample.py"
    code_doc.write_text("def foo():\n    return 1\n", encoding="utf-8")

    with make_real_app(tmp_path, monkeypatch, graph_enabled=False) as (client, _cfg, api_key):
        ingest_file_via_path(client, col, str(code_doc), api_key=api_key)

        # Trip the missing-grammar branch directly to prove even a "dirty" registry
        # state does not leak into the response when graph is disabled.
        ce._get_grammar(".py")
        assert ce.has_missing_code_parsers() is True

        resp = client.get("/status", headers=_auth(api_key))
        assert resp.status_code == 200, f"GET /status failed: {resp.status_code} {resp.text}"
        data = resp.json()

        assert "code_parsers" in data, (
            f"'code_parsers' key missing from status response: {list(data.keys())}"
        )
        assert data["code_parsers"] is None, (
            f"code_parsers should be null when graph.enabled=False, got: {data['code_parsers']}"
        )


def test_codeParsersStatus_healthyPath_notDegraded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """graph.enabled=True and no missing-grammar event → /status reports a healthy code_parsers (C1-T-2a).

    Also directly asserts has_missing_code_parsers() is False on fresh/isolated
    state, matching the isolation pattern used in
    tests/test_code_enricher.py's TestGrammarRegistry fixture.
    """
    import archon_search.code_enricher as ce

    monkeypatch.setattr(ce, "_GRAMMAR_CACHE", {})
    monkeypatch.setattr(ce, "_GRAMMAR_LOGGED", set())

    assert ce.has_missing_code_parsers() is False

    _install_spacy_stub(monkeypatch)

    col = "be11-healthy"
    prose_doc = tmp_path / "prose.txt"
    prose_doc.write_text("Alice is a software engineer.\n" * 10, encoding="utf-8")

    with make_real_app(tmp_path, monkeypatch, graph_enabled=True) as (client, _cfg, api_key):
        ingest_file_via_path(client, col, str(prose_doc), api_key=api_key)

        resp = client.get("/status", headers=_auth(api_key))
        assert resp.status_code == 200, f"GET /status failed: {resp.status_code} {resp.text}"
        data = resp.json()

        assert "code_parsers" in data, (
            f"'code_parsers' key missing from status response: {list(data.keys())}"
        )
        code_parsers = data["code_parsers"]
        assert code_parsers is not None, (
            "code_parsers sub-object is None even though graph.enabled=True"
        )
        assert code_parsers["degraded"] is False
        assert code_parsers["missing_extensions"] == []
        assert not code_parsers["message"]


def test_codeParsersStatus_wizardSuccessCase_notDegraded_withRealCodeParser(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Wizard's intended end-state (both extras present) doesn't hit the degraded path (C1-A-5).

    Proves the missing half of the happy-path story: `test_codeParsersStatus_healthyPath_notDegraded`
    only ingests prose and never exercises the tree-sitter grammar at all, so it
    can't distinguish "healthy because untested" from "healthy because the grammar
    genuinely parses". This test ingests a REAL code file with tree-sitter's
    Python grammar genuinely available (not stubbed absent) — the state a guided
    wizard install (BE-11 `graph.enabled=true` + `[code]`+`[graph]` extras both
    installed) is meant to reach — and asserts `/status`'s `code_parsers` stays
    healthy (`degraded=False`).

    Skips gracefully when tree-sitter-python isn't installed locally (optional
    `[code]` extra; CI's bare `uv sync --dev` never installs it — see
    tests/integration/test_http_enrichment_metadata.py for the same guard).
    """
    try:
        import tree_sitter_python  # noqa: F401  # type: ignore[import-untyped]
    except ImportError:
        pytest.skip("tree-sitter-python not installed — code enrichment skipped")

    import archon_search.code_enricher as ce

    # Isolate this test's grammar-registry state from other tests/process activity
    # on the same xdist worker — both are process-global module dicts.
    monkeypatch.setattr(ce, "_GRAMMAR_CACHE", {})
    monkeypatch.setattr(ce, "_GRAMMAR_LOGGED", set())

    _install_spacy_stub(monkeypatch)

    col = "be11-wizard-success"
    code_doc = tmp_path / "sample.py"
    code_doc.write_text(
        "def foo():\n    return 1\n\n\nclass Bar:\n    def baz(self):\n        pass\n",
        encoding="utf-8",
    )

    with make_real_app(tmp_path, monkeypatch, graph_enabled=True) as (client, _cfg, api_key):
        # Real tree-sitter grammar in use — no absent-package simulation.
        ingest_file_via_path(client, col, str(code_doc), api_key=api_key)

        resp = client.get("/status", headers=_auth(api_key))
        assert resp.status_code == 200, f"GET /status failed: {resp.status_code} {resp.text}"
        data = resp.json()

        assert "code_parsers" in data, (
            f"'code_parsers' key missing from status response: {list(data.keys())}"
        )
        code_parsers = data["code_parsers"]
        assert code_parsers is not None, (
            "code_parsers sub-object is None even though graph.enabled=True"
        )
        assert code_parsers["degraded"] is False, (
            "wizard success state (both extras present, graph enabled) must not "
            f"report a degraded code_parsers status, got: {code_parsers}"
        )
        assert code_parsers["missing_extensions"] == []
