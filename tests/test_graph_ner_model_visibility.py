"""Startup visibility for the graph prose-NER engine (2026-08-19-030).

A missing graph engine dependency used to be discoverable only by reading
per-file ingest logs. It now surfaces through
``ModelValidationResult.provider_warnings`` and therefore ``GET /status``.

Rewired off spaCy in cycle-2 (C2-A-01/C2-B-1/C1-B-1): the shipped prose
extraction engine since BE-11 is gliner
(``paths.GRAPH_NER_MODEL_NAME`` = "knowledgator/gliner-relex-multi-v1.0"),
never spaCy — the probe below now checks gliner import-ability, mirroring
``ensure_graph_engine_importable``'s construction-time guard. The engine is
also multilingual, so there is no English-only disclosure to make (C2-A-02/
C2-B-2) — ``notes`` is always empty here.
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from archon_search.config import SearchConfig
from archon_search.jobs import JobStore
from archon_search.model_validation import graph_ner_status, validate_models_async
from archon_search.server.app import create_app


def _config_with_graph(tmp_path: Path, *, enabled: bool, multilingual: bool) -> SearchConfig:
    config = SearchConfig()
    config.db_path = str(tmp_path / "db")
    config.graph.enabled = enabled
    config.multilingual = multilingual
    return config


def _gliner_absent() -> "patch.dict[str, object | None]":
    """Bind ``sys.modules['gliner']`` to ``None`` — the suite's absent-module
    idiom (mirrors ``tests/test_graph_deps_construction_guard.py``'s
    ``_gliner_absent`` helper; `find_spec` raises `ValueError` on a bare
    `None` sentinel entry only when nothing binds `__spec__`, and both
    ``ensure_graph_engine_importable`` and ``graph_ner_status`` treat that
    as "found", never "absent" — see graph_extractor.py). Scoped to a `with`
    block, not the whole test: `create_app` also runs the SAME
    construction-time guard, so leaving gliner "absent" for the rest of the
    test would fail app creation, not just the probe under test."""
    return patch.dict(sys.modules, {"gliner": None})  # type: ignore[dict-item]


def test_graph_disabled_produces_no_warnings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No graph, no probe: gliner's absence is irrelevant."""
    monkeypatch.setenv("ARCHON_SEARCH_DATA_DIR", str(tmp_path))
    config = _config_with_graph(tmp_path, enabled=False, multilingual=True)

    with _gliner_absent():
        assert graph_ner_status(config) == ([], [])


def test_gliner_absent_warns_at_startup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """[graph] enabled with gliner not importable -> an actionable startup warning."""
    monkeypatch.setenv("ARCHON_SEARCH_DATA_DIR", str(tmp_path))
    config = _config_with_graph(tmp_path, enabled=True, multilingual=False)

    with _gliner_absent():
        warnings, notes = graph_ner_status(config)

    assert notes == [], "gliner is multilingual — nothing to disclose"
    assert len(warnings) == 1, warnings
    assert "gliner" in warnings[0]
    assert "pip install" in warnings[0], "the warning must name the fix, not just the symptom"


def test_gliner_present_is_silent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Nothing to disclose when gliner is importable, multilingual or not —
    the shipped engine is multilingual so there is no English-only case."""
    monkeypatch.setenv("ARCHON_SEARCH_DATA_DIR", str(tmp_path))

    for multilingual in (False, True):
        config = _config_with_graph(tmp_path, enabled=True, multilingual=multilingual)
        assert graph_ner_status(config) == ([], []), f"multilingual={multilingual}"


@pytest.mark.asyncio
async def test_status_surfaces_missing_gliner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The ``/status`` serialiser surfaces the warning.

    ``app.state.model_validation`` is assigned directly rather than driven
    through the lifespan: this pins the serialiser contract, not the wiring
    (the lifespan's own spawn is covered by the app tests).
    """
    monkeypatch.setenv("ARCHON_SEARCH_DATA_DIR", str(tmp_path))
    config = _config_with_graph(tmp_path, enabled=True, multilingual=False)

    with _gliner_absent(), patch(
        "archon_search.model_validation.validate_providers_shared",
        return_value=(True, True, []),
    ):
        result = await validate_models_async(config)

    job_store = JobStore(path=tmp_path / "jobs.json")
    app = create_app(config, job_store)
    mock_store = MagicMock()
    mock_store.get_all_collections_meta = AsyncMock(return_value=[])
    mock_store.migrate_namespace = AsyncMock()
    mock_store.connect = AsyncMock()
    mock_store.disconnect = AsyncMock()
    mock_store.ping = AsyncMock(return_value=True)
    mock_store.pending_migrations = AsyncMock(return_value=[])
    app.state.search_store = mock_store
    app.state.model_validation = result

    # Explicit, not read from the ambient environment: an absent env var
    # turned the assertion below into a confusing 401 (C2-B-21).
    key = app.state.api_key
    client = TestClient(app, headers={"Authorization": f"Bearer {key}"})
    response = client.get("/status")

    assert response.status_code == 200
    warnings = response.json()["model_validation"]["provider_warnings"]
    assert any("gliner" in w for w in warnings), (
        f"the missing graph engine must surface on /status; got {warnings}"
    )


def test_gliner_absent_message_names_the_extra(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """T6: the extractor's own "[graph] extra missing" wording is reused
    verbatim so the two surfaces (construction-time guard, startup probe)
    agree."""
    from archon_search.graph_extractor import GLINER_NOT_INSTALLED_MESSAGE

    monkeypatch.setenv("ARCHON_SEARCH_DATA_DIR", str(tmp_path))
    config = _config_with_graph(tmp_path, enabled=True, multilingual=False)

    with _gliner_absent():
        warnings, _notes = graph_ner_status(config)

    assert len(warnings) == 1, warnings
    assert GLINER_NOT_INSTALLED_MESSAGE in warnings[0]


# ---------------------------------------------------------------------------
# T7: `graph_ner_status` never raises — pin the docstring's claim directly.
# ---------------------------------------------------------------------------


def test_graph_ner_status_never_raises_on_unexpected_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Any unexpected failure in the probe reads as "cannot determine" rather
    than propagating — `validate_models_async` calls this UNGUARDED before
    its own `try`, so the docstring's "never raises" claim must actually
    hold."""
    monkeypatch.setenv("ARCHON_SEARCH_DATA_DIR", str(tmp_path))
    config = _config_with_graph(tmp_path, enabled=True, multilingual=False)

    with patch(
        "importlib.util.find_spec",
        side_effect=RuntimeError("boom: unexpected probe failure"),
    ):
        warnings, notes = graph_ner_status(config)

    assert warnings == ["graph NER model presence could not be determined"]
    assert notes == []


# ---------------------------------------------------------------------------
# T4: `graph_warnings` are PREPENDED on ALL FOUR `validate_models_async`
# return paths — success, timeout, unexpected failure, split-provider
# failure — not just the success path that was the only one tested before.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "scenario", ["success", "timeout", "unexpected_failure", "split_provider_failure"]
)
async def test_graph_warnings_prepended_on_every_return_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, scenario: str
) -> None:
    """Asserted by INDEX because "prepended" is the contract, not just "present"."""
    monkeypatch.setenv("ARCHON_SEARCH_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(
        "archon_search.model_validation.graph_ner_status",
        lambda config: (["graph test warning"], []),
    )

    config = SearchConfig()
    config.db_path = str(tmp_path / "db")

    if scenario == "success":
        # A non-empty provider warning is what makes this case discriminate:
        # with an empty list, prepend and append produce the same
        # single-element result and the assertion proves nothing (C2-T-2).
        with patch(
            "archon_search.model_validation.validate_providers_shared",
            return_value=(True, True, ["provider test warning"]),
        ):
            result = await validate_models_async(config)
    elif scenario == "timeout":
        def _slow(*args, **kwargs):
            import time as _time

            _time.sleep(0.2)
            return True, True, []

        with patch(
            "archon_search.model_validation.validate_providers_shared", side_effect=_slow
        ):
            result = await validate_models_async(config, timeout_seconds=0.01)
    elif scenario == "unexpected_failure":
        with patch(
            "archon_search.model_validation.validate_providers_shared",
            side_effect=RuntimeError("boom"),
        ):
            result = await validate_models_async(config)
    else:  # split_provider_failure
        config.reranker_providers = ["CPUExecutionProvider"]
        config.reranker_model = "some-reranker"
        call_count = {"n": 0}

        def _split(*args, **kwargs):
            call_count["n"] += 1
            if call_count["n"] == 1:
                return True, True, []
            raise RuntimeError("split boom")

        with patch(
            "archon_search.model_validation.validate_providers_shared", side_effect=_split
        ):
            result = await validate_models_async(config)

    assert result.provider_warnings[0] == "graph test warning", (
        f"scenario={scenario!r}: graph_warnings must be PREPENDED; got {result.provider_warnings!r}"
    )


def test_wizard_summary_no_english_only_note_when_graph_disabled() -> None:
    """Install-wizard summary text (own, independent wording — the install
    package must not import the graph layer): still exercised here as the
    negative case, unaffected by the runtime status-probe rewire above."""
    from archon_search.install import WizardFeatures, _render_summary
    from archon_search.profiles import get_profile

    profile = get_profile("balanced", multilingual=True)
    features = WizardFeatures(install_graph_extra=False)
    output = _render_summary("balanced", profile, multilingual=True, providers=[], features=features)

    assert "English-only" not in output, (
        "multilingual=True with graph disabled must not carry the graph-specific "
        f"English-only disclosure; got:\n{output}"
    )


def test_status_surfaces_notes_separately_from_warnings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """C1-I-7 on the wire: ``provider_notes`` is serialised as its own field,
    so a permanent disclosure never lands in the one ``routes_ready`` grades.

    Driven from a hand-built result rather than a real probe run: the
    warnings/notes split itself is pinned by the unit tests above.
    """
    from archon_search.model_validation import ModelValidationResult

    monkeypatch.setenv("ARCHON_SEARCH_DATA_DIR", str(tmp_path))
    config = _config_with_graph(tmp_path, enabled=True, multilingual=False)

    job_store = JobStore(path=tmp_path / "jobs.json")
    app = create_app(config, job_store)
    mock_store = MagicMock()
    mock_store.get_all_collections_meta = AsyncMock(return_value=[])
    mock_store.migrate_namespace = AsyncMock()
    mock_store.connect = AsyncMock()
    mock_store.disconnect = AsyncMock()
    mock_store.ping = AsyncMock(return_value=True)
    mock_store.pending_migrations = AsyncMock(return_value=[])
    app.state.search_store = mock_store
    app.state.model_validation = ModelValidationResult(
        embedder_ok=True,
        reranker_ok=True,
        provider_warnings=[],
        provider_notes=["some permanent, unactionable note"],
    )

    client = TestClient(app, headers={"Authorization": f"Bearer {app.state.api_key}"})
    body = client.get("/status").json()["model_validation"]

    assert any("permanent" in n for n in body["provider_notes"]), body
    assert body["provider_warnings"] == [], (
        "a permanent disclosure in provider_warnings pins checks.models to WARN "
        f"with no action that clears it; got {body['provider_warnings']!r}"
    )
