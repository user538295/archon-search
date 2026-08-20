"""Startup visibility for the graph prose-NER model (2026-08-19-030).

A missing ``en_core_web_sm`` used to be discoverable only by reading per-file
ingest logs. It now surfaces through ``ModelValidationResult.provider_warnings``
and therefore ``GET /status``, alongside the honest English-only disclosure for
multilingual deployments.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from archon_search.config import SearchConfig
from archon_search.jobs import JobStore
from archon_search.model_validation import graph_ner_warnings, validate_models_async
from archon_search.server.app import create_app

# The shared spaCy stub factory (2026-08-19-030 review: three independently
# hand-rolled spaCy stubs across this change set was the exact mechanism that
# produced a fabricated-fixture Critical elsewhere — consolidated instead of
# repeated here). `tests/` is a proper package (`tests/__init__.py`), so this
# cross-module import is safe under pytest's rootdir import mode.
from tests.test_graph_extractor import _make_spacy_stub as _spacy_stub


def _config_with_graph(tmp_path: Path, *, enabled: bool, multilingual: bool) -> SearchConfig:
    config = SearchConfig()
    config.db_path = str(tmp_path / "db")
    config.graph.enabled = enabled
    config.multilingual = multilingual
    return config


def test_graph_disabled_produces_no_warnings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No graph, no probe: the model's absence is irrelevant."""
    monkeypatch.setenv("ARCHON_SEARCH_DATA_DIR", str(tmp_path))
    config = _config_with_graph(tmp_path, enabled=False, multilingual=True)

    with patch.dict(sys.modules, _spacy_stub(installed_models=[])):
        assert graph_ner_warnings(config) == []


def test_missing_model_warns_at_startup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """[graph] enabled with no model anywhere → an actionable startup warning."""
    monkeypatch.setenv("ARCHON_SEARCH_DATA_DIR", str(tmp_path))
    config = _config_with_graph(tmp_path, enabled=True, multilingual=False)

    with patch.dict(sys.modules, _spacy_stub(installed_models=[])):
        warnings = graph_ner_warnings(config)

    assert len(warnings) == 1, warnings
    assert "en_core_web_sm" in warnings[0]
    assert "wizard" in warnings[0], "the warning must name the fix, not just the symptom"


def test_present_model_and_multilingual_discloses_english_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Honest disclosure: the model works, but only for English prose."""
    monkeypatch.setenv("ARCHON_SEARCH_DATA_DIR", str(tmp_path))
    config = _config_with_graph(tmp_path, enabled=True, multilingual=True)

    with patch.dict(sys.modules, _spacy_stub(installed_models=["en_core_web_sm"])):
        warnings = graph_ner_warnings(config)

    assert len(warnings) == 1, warnings
    assert "English-only" in warnings[0]
    assert "en_core_web_sm" in warnings[0]


def test_present_model_english_deployment_is_silent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Nothing to disclose when the model is present and the corpus is English."""
    monkeypatch.setenv("ARCHON_SEARCH_DATA_DIR", str(tmp_path))
    config = _config_with_graph(tmp_path, enabled=True, multilingual=False)

    with patch.dict(sys.modules, _spacy_stub(installed_models=["en_core_web_sm"])):
        assert graph_ner_warnings(config) == []


@pytest.mark.asyncio
async def test_status_surfaces_missing_graph_model(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """End to end: the missing model reaches ``GET /status``, not only the logs."""
    monkeypatch.setenv("ARCHON_SEARCH_DATA_DIR", str(tmp_path))
    config = _config_with_graph(tmp_path, enabled=True, multilingual=False)

    with patch.dict(sys.modules, _spacy_stub(installed_models=[])):
        with patch(
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

    key = os.environ.get("ARCHON_SEARCH_API_KEY", "")
    client = TestClient(app, headers={"Authorization": f"Bearer {key}"})
    response = client.get("/status")

    assert response.status_code == 200
    warnings = response.json()["model_validation"]["provider_warnings"]
    assert any("en_core_web_sm" in w for w in warnings), (
        f"the missing graph model must surface on /status; got {warnings}"
    )


# ---------------------------------------------------------------------------
# T6: a missing spaCy PACKAGE (the `[graph]` extra itself) must not be
# reported as a missing MODEL — the two surfaces (extractor, startup probe)
# must agree on which one is absent.
# ---------------------------------------------------------------------------


def test_spacy_package_absent_blames_the_extra_not_the_model(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`[graph] enabled` with spaCy itself uninstalled must say so — not
    "model neither installed nor provisioned", which blames the wrong thing."""
    from archon_search.graph_extractor import SPACY_NOT_INSTALLED_MESSAGE

    monkeypatch.setenv("ARCHON_SEARCH_DATA_DIR", str(tmp_path))
    config = _config_with_graph(tmp_path, enabled=True, multilingual=False)

    with patch.dict(sys.modules, {"spacy": None}):  # type: ignore[dict-item]
        warnings = graph_ner_warnings(config)

    assert len(warnings) == 1, warnings
    assert SPACY_NOT_INSTALLED_MESSAGE in warnings[0], (
        f"expected the extractor's own '[graph] extra missing' wording (agreement, T6); "
        f"got {warnings[0]!r}"
    )
    assert "neither installed nor provisioned" not in warnings[0], (
        "must not blame the MODEL when spaCy itself is the thing that's missing"
    )
    assert "pip install" in warnings[0]


# ---------------------------------------------------------------------------
# T7: `graph_ner_warnings` never raises — pin the docstring's claim directly.
# ---------------------------------------------------------------------------


def testgraph_ner_warnings_never_raises_on_unexpected_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Any unexpected failure in the resolution path reads as "cannot
    determine" rather than propagating — `validate_models_async` calls this
    UNGUARDED before its own `try`, so the docstring's "never raises" claim
    must actually hold."""
    monkeypatch.setenv("ARCHON_SEARCH_DATA_DIR", str(tmp_path))
    config = _config_with_graph(tmp_path, enabled=True, multilingual=False)

    with (
        patch.dict(sys.modules, _spacy_stub(installed_models=["en_core_web_sm"])),
        patch(
            "archon_search.graph_extractor.resolve_spacy_model",
            side_effect=RuntimeError("boom: unexpected probe failure"),
        ),
    ):
        warnings = graph_ner_warnings(config)

    assert warnings == ["graph NER model presence could not be determined"]


# ---------------------------------------------------------------------------
# T16: multilingual AND missing/incompatible model — both notes are
# independently true and both must surface, not just one.
# ---------------------------------------------------------------------------


def test_missing_model_and_multilingual_shows_both_notes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A deployment that is both multilingual AND missing the model needs
    BOTH the "provision it" fix and the "English-only anyway" disclosure —
    dropping either one loses actionable information."""
    monkeypatch.setenv("ARCHON_SEARCH_DATA_DIR", str(tmp_path))
    config = _config_with_graph(tmp_path, enabled=True, multilingual=True)

    with patch.dict(sys.modules, _spacy_stub(installed_models=[])):
        warnings = graph_ner_warnings(config)

    assert len(warnings) == 2, warnings
    assert any("en_core_web_sm" in w and "wizard" in w for w in warnings), (
        f"missing-model note must still surface; got {warnings!r}"
    )
    assert any("English-only" in w for w in warnings), (
        f"English-only disclosure must still surface; got {warnings!r}"
    )


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
        "archon_search.model_validation.graph_ner_warnings",
        lambda config: ["graph test warning"],
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


# ---------------------------------------------------------------------------
# T19: get_spacy_models_dir() honors ARCHON_SEARCH_DATA_DIR.
# ---------------------------------------------------------------------------


def test_get_spacy_models_dir_honors_data_dir_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from archon_search.paths import get_spacy_models_dir

    monkeypatch.setenv("ARCHON_SEARCH_DATA_DIR", str(tmp_path))
    assert get_spacy_models_dir() == tmp_path / "models" / "spacy"


# ---------------------------------------------------------------------------
# T20: the wizard summary's English-only disclosure requires GRAPH enabled,
# not just multilingual — pin the negative case. render.py itself is owned
# by another agent this cycle; test only.
# ---------------------------------------------------------------------------


def test_wizard_summary_no_english_only_note_when_graph_disabled() -> None:
    from archon_search.install import WizardFeatures, _render_summary
    from archon_search.profiles import get_profile

    profile = get_profile("balanced", multilingual=True)
    features = WizardFeatures(install_graph_extra=False)
    output = _render_summary("balanced", profile, multilingual=True, providers=[], features=features)

    assert "English-only" not in output, (
        "multilingual=True with graph disabled must not carry the graph-specific "
        f"English-only disclosure; got:\n{output}"
    )
