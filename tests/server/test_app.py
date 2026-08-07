"""Tests for archon_search.server.app startup guards."""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from archon_search.config import SearchConfig
from archon_search.server.app import _check_multilingual_deps


# ---------------------------------------------------------------------------
# _check_multilingual_deps
# ---------------------------------------------------------------------------


def _make_config(*, multilingual: bool) -> SearchConfig:
    cfg = SearchConfig()
    cfg.multilingual = multilingual
    return cfg


class TestCheckMultilingualDeps:
    def test_check_multilingual_deps_disabled(self):
        """When multilingual=False, _import_fasttext is never called and function returns normally."""
        cfg = _make_config(multilingual=False)
        with patch("archon_search.server.app._import_fasttext") as mock_import:
            _check_multilingual_deps(cfg)
        mock_import.assert_not_called()

    def test_check_multilingual_deps_package_missing(self, tmp_path):
        """When multilingual=True and fasttext-wheel is not installed, RuntimeError with 'fasttext-wheel' in message."""
        cfg = _make_config(multilingual=True)
        model_path = tmp_path / "models" / "lid.176.ftz"
        model_path.parent.mkdir(parents=True, exist_ok=True)
        model_path.touch()

        # Simulate fasttext not installed by making import raise ImportError
        with patch("archon_search.server.app._import_fasttext", side_effect=ImportError("no module named fasttext")):
            with pytest.raises(RuntimeError) as exc_info:
                _check_multilingual_deps(cfg)
        assert "fasttext-wheel" in str(exc_info.value)

    def test_check_multilingual_deps_model_missing(self, tmp_path):
        """When multilingual=True, import succeeds but model file is absent: RuntimeError with 'lid.176.ftz'."""
        cfg = _make_config(multilingual=True)

        mock_fasttext = MagicMock()
        missing_path = tmp_path / "missing" / "lid.176.ftz"
        with patch("archon_search.server.app._import_fasttext", return_value=mock_fasttext):
            with patch(
                "archon_search.server.app._multilingual_model_path",
                return_value=missing_path,
            ):
                with pytest.raises(RuntimeError) as exc_info:
                    _check_multilingual_deps(cfg)
        assert "lid.176.ftz" in str(exc_info.value)

    def test_check_multilingual_deps_all_present(self, tmp_path):
        """When multilingual=True, import succeeds, and model file exists: returns without error."""
        cfg = _make_config(multilingual=True)
        model_path = tmp_path / "models" / "lid.176.ftz"
        model_path.parent.mkdir(parents=True, exist_ok=True)
        model_path.touch()

        mock_fasttext = MagicMock()
        with patch("archon_search.server.app._import_fasttext", return_value=mock_fasttext):
            with patch(
                "archon_search.server.app._multilingual_model_path",
                return_value=model_path,
            ):
                _check_multilingual_deps(cfg)  # no exception


# ---------------------------------------------------------------------------
# create_app production path — LanguageDetector wired when multilingual=True
# ---------------------------------------------------------------------------


class TestCreateAppLanguageDetectorWiring:
    def test_create_app_wires_language_detector_when_multilingual(self, tmp_path):
        """create_app() passes a LanguageDetector to SearchPipeline when multilingual=True."""
        from archon_search.jobs import JobStore
        from archon_search.server.app import create_app
        from archon_search.language_detector import LanguageDetector

        cfg = SearchConfig()
        cfg.multilingual = True
        cfg.db_path = str(tmp_path / "test.db")

        job_store = JobStore()

        with (
            patch("archon_search.server.app._check_multilingual_deps"),
            patch("archon_search.server.app._import_fasttext"),
            patch.object(LanguageDetector, "__init__", return_value=None),
            patch("archon_search.server.app.ModelEmbedder"),
            patch("archon_search.server.app.SearchStore"),
            patch("archon_search.server.app.IndexingStateStore"),
        ):
            app = create_app(cfg, job_store)

        pipeline = app.state.pipeline
        assert pipeline._language_detector is not None
        assert isinstance(pipeline._language_detector, LanguageDetector)
        assert pipeline._language_detection_confidence_threshold == cfg.language_detection_confidence_threshold

    def test_create_app_no_language_detector_when_not_multilingual(self, tmp_path):
        """create_app() does NOT pass a LanguageDetector to SearchPipeline when multilingual=False."""
        from archon_search.jobs import JobStore
        from archon_search.server.app import create_app
        from archon_search.language_detector import LanguageDetector

        cfg = SearchConfig()
        cfg.multilingual = False
        cfg.db_path = str(tmp_path / "test.db")

        job_store = JobStore()

        with (
            patch("archon_search.server.app._check_multilingual_deps"),
            patch.object(LanguageDetector, "__init__", side_effect=AssertionError("should not be called")),
            patch("archon_search.server.app.ModelEmbedder"),
            patch("archon_search.server.app.SearchStore"),
            patch("archon_search.server.app.IndexingStateStore"),
        ):
            app = create_app(cfg, job_store)

        pipeline = app.state.pipeline
        assert pipeline._language_detector is None


# ---------------------------------------------------------------------------
# create_app production path — fan-out config wiring (S443)
# ---------------------------------------------------------------------------


def _create_app_with_stubbed_deps(cfg, job_store):
    """Call create_app with the expensive constructors patched out.

    Mirrors the patch set used by TestCreateAppLanguageDetectorWiring so the
    wiring assertions cost microseconds instead of a real LanceDB open.
    """
    from archon_search.server.app import create_app

    with (
        patch("archon_search.server.app.ModelEmbedder"),
        patch("archon_search.server.app.SearchStore"),
        patch("archon_search.server.app.IndexingStateStore"),
    ):
        return create_app(cfg, job_store)


class TestCreateAppPipelineWiring:
    def test_create_app_forwards_every_search_pipeline_param(self, tmp_path):
        """S443 structural guard: create_app must forward EVERY SearchPipeline param.

        S443 happened because ``create_app`` hand-maintains a ~20-argument
        ``SearchPipeline(...)`` call that silently drifted from the constructor
        signature — three fan-out kwargs were simply never passed, so operator
        TOML values were replaced by the ``__init__`` defaults.  The pre-existing
        ``test_create_pipeline_passes_fanout_config`` stayed green throughout,
        because it guards ``pipeline.create_pipeline`` — a factory the server
        never calls.

        This test closes that gap for the site where the bug actually happened:
        it fails the moment a new ``SearchPipeline.__init__`` parameter is added
        without a matching keyword in ``create_app``.
        """
        import inspect

        from archon_search.jobs import JobStore
        from archon_search.pipeline import SearchPipeline

        cfg = SearchConfig()
        cfg.db_path = str(tmp_path / "test.db")
        job_store = JobStore()

        with patch("archon_search.server.app.SearchPipeline") as mock_pipeline:
            _create_app_with_stubbed_deps(cfg, job_store)

        assert mock_pipeline.call_count == 1, (
            f"expected create_app to build exactly one SearchPipeline, "
            f"got {mock_pipeline.call_count}"
        )
        forwarded = set(mock_pipeline.call_args.kwargs)
        expected = set(inspect.signature(SearchPipeline.__init__).parameters) - {"self"}
        missing = expected - forwarded
        assert not missing, (
            "create_app does not forward these SearchPipeline.__init__ params: "
            f"{sorted(missing)}. Add them to the SearchPipeline(...) call in "
            "archon_search/server/app.py — a forgotten param silently falls back "
            "to the constructor default and the operator's config is ignored (S443)."
        )

    def test_create_app_wires_fanout_config_into_pipeline(self, tmp_path):
        """S443 regression: [search] fan-out knobs must reach the SearchPipeline.

        ``create_app`` built ``SearchPipeline`` without passing ``max_fanout`` /
        ``fanout_leg_trim`` / ``fanout_timeout_seconds``, so every configured
        value was silently replaced by the ``SearchPipeline.__init__`` defaults
        (8 / 40 / 30.0).  The routes read ``config.max_fanout`` for the
        collection-count guard, but the actual ``asyncio.timeout`` in
        ``_fanout_merge_acl`` used the stale 30.0 s default, so an
        operator-configured timeout never fired.

        Goes through the real ``load_config`` TOML path so it also proves the
        ``[search]`` keys map onto the ``SearchConfig`` attributes ``create_app``
        reads.

        Note: ``_max_fanout`` is currently write-only on ``SearchPipeline`` — the
        fan-out breadth cap is enforced in the route/MCP handlers from
        ``config.max_fanout`` directly.  It is asserted here for wiring parity
        with ``create_pipeline``, not as a behavioral guarantee.
        """
        from archon_search.config import load_config
        from archon_search.jobs import JobStore

        toml_file = tmp_path / "archon-search.toml"
        toml_file.write_text(
            "[search]\nfanout_timeout_seconds = 0.75\nmax_fanout = 3\nfanout_leg_trim = 7\n",
            encoding="utf-8",
        )
        cfg = load_config(toml_file)
        cfg.db_path = str(tmp_path / "test.db")

        assert cfg.fanout_timeout_seconds == 0.75, (
            f"config did not load fanout_timeout_seconds; got {cfg.fanout_timeout_seconds!r}"
        )

        app = _create_app_with_stubbed_deps(cfg, JobStore())

        pipeline = app.state.pipeline
        assert pipeline._fanout_timeout_seconds == 0.75, (
            "create_app did not pass fanout_timeout_seconds to SearchPipeline; "
            f"pipeline uses {pipeline._fanout_timeout_seconds!r} instead of 0.75"
        )
        assert pipeline._max_fanout == 3, (
            "create_app did not pass max_fanout to SearchPipeline; "
            f"pipeline uses {pipeline._max_fanout!r} instead of 3"
        )
        assert pipeline._fanout_leg_trim == 7, (
            "create_app did not pass fanout_leg_trim to SearchPipeline; "
            f"pipeline uses {pipeline._fanout_leg_trim!r} instead of 7"
        )
