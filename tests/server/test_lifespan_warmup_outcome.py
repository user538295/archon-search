"""S632: the lifespan's background model warm-up reports its real outcome.

Every model loader the warm-up drives logs and swallows a model that cannot load, so
the warm-up used to announce ``model warm-up complete`` and set ``warmup_result`` to
DONE over an invalid ``embedding_model`` / ``reranker_model`` — the operator never saw
``model warm-up failed: ...`` and ``/ready`` reported ``checks.models: "ok"``.

These tests boot the real lifespan against the suite-wide fastembed stub
(``tests/_search_stubs.py``, installed by ``tests/conftest.py`` before anything imports
fastembed): no ONNX weights are built, so the real ``SearchPipeline.warmup_models`` and
``EmbedderCache.preload`` finish in milliseconds. The stub's model classes are wrapped
so they reject an unknown name exactly the way real fastembed does (``ValueError`` from
the constructor) and, where a test asks for it, fail inference after a successful
build. Model validation is pinned to all-OK so ``checks.models`` can only reflect the
warm-up.
"""
from __future__ import annotations

import logging
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import fastembed
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from fastembed.rerank import cross_encoder

from archon_search.collection_meta import CollectionMeta
from archon_search.config import SearchConfig
from archon_search.embedder_cache import EmbedderCache
from archon_search.jobs.store import JobStore
from archon_search.model_validation import ModelValidationResult
from archon_search.server import app as app_module
from archon_search.server import warmup as warmup_module
from archon_search.server.app import create_app
from archon_search.server.schemas import WarmupResult
from archon_search.store import SearchStore
from tests.test_app import wait_for_warmup

_WARMUP_LOGGER = "archon_search.server.warmup"
_FAILED_PREFIX = "model warm-up failed: "

#: Names fastembed does not know — the S632 reproduction's invalid model names.
_UNSUPPORTED_EMBEDDER = "nonexistent-org/no-such-model-s632"
_UNSUPPORTED_RERANKER = "nonexistent-org/no-such-reranker-s632"
_UNSUPPORTED_COLLECTION_MODEL = "nonexistent-org/no-such-collection-model-s632"

#: Loadable per-collection models (the stub accepts any name it is not told to reject).
_COLLECTION_MODEL_A = "example-org/collection-model-a-s632"
_COLLECTION_MODEL_B = "example-org/collection-model-b-s632"


@dataclass(frozen=True)
class WarmupRun:
    """What an operator can observe once the warm-up task has finished."""

    app: FastAPI
    ready_status: int
    ready: dict[str, Any]


def _failing_inference(texts: list[str]) -> object:
    raise RuntimeError("inference failed after the model was built")


def _patched(
    model_class: Callable[..., object],
    unsupported: frozenset[str],
    broken_inference: frozenset[str] = frozenset(),
) -> Callable[..., object]:
    """Wrap a fastembed model class: reject *unsupported* names like real fastembed,
    and build *broken_inference* models whose ``embed()`` then raises."""

    def _construct(model_name: str, **kwargs: object) -> object:
        if model_name in unsupported:
            raise ValueError(f"Model {model_name} is not supported.")
        model = model_class(model_name, **kwargs)
        if model_name in broken_inference:
            model.embed = _failing_inference  # type: ignore[attr-defined]
        return model

    return _construct


def _collection(name: str, model: str) -> CollectionMeta:
    return CollectionMeta(name=name, active_embedding_model=model)


def _is_warmup_failure(record: logging.LogRecord) -> bool:
    return (
        record.name == _WARMUP_LOGGER
        and record.levelno == logging.WARNING
        and record.getMessage().startswith(_FAILED_PREFIX)
    )


def _warmup_failures(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [record.getMessage() for record in caplog.records if _is_warmup_failure(record)]


def _warmup_infos(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [
        record.getMessage()
        for record in caplog.records
        if record.name == _WARMUP_LOGGER and record.levelno == logging.INFO
    ]


@pytest.fixture
def config(tmp_path: Path) -> SearchConfig:
    cfg = SearchConfig()
    cfg.db_path = str(tmp_path / "search")
    return cfg


@pytest.fixture
def run_warmup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> Callable[..., WarmupRun]:
    """Factory: boot the real lifespan, wait out the warm-up task, return what it left.

    ``unsupported`` names the models fastembed rejects; ``broken_inference`` names
    embedders that build but whose ``embed()`` raises; ``collections`` is what the
    store reports, which ``eager_load_embedders`` turns into per-collection preloads.
    """
    validation_ok = ModelValidationResult(embedder_ok=True, reranker_ok=True)
    monkeypatch.setattr(app_module, "validate_models_async", AsyncMock(return_value=validation_ok))
    monkeypatch.setattr(SearchStore, "connect", AsyncMock())
    monkeypatch.setattr(SearchStore, "_run_startup_migrations", AsyncMock())
    monkeypatch.setattr(SearchStore, "disconnect", AsyncMock())
    monkeypatch.setattr(SearchStore, "ping", AsyncMock(return_value=True))
    caplog.set_level(logging.INFO, logger=_WARMUP_LOGGER)

    def _run(
        cfg: SearchConfig,
        *,
        unsupported: frozenset[str] = frozenset(),
        broken_inference: frozenset[str] = frozenset(),
        collections: Iterable[CollectionMeta] = (),
    ) -> WarmupRun:
        monkeypatch.setattr(
            fastembed, "TextEmbedding", _patched(fastembed.TextEmbedding, unsupported, broken_inference)
        )
        monkeypatch.setattr(cross_encoder, "TextCrossEncoder", _patched(cross_encoder.TextCrossEncoder, unsupported))
        monkeypatch.setattr(SearchStore, "get_all_collections_meta", AsyncMock(return_value=list(collections)))
        app = create_app(cfg, JobStore(path=tmp_path / "jobs.json"))
        with TestClient(app) as client:
            wait_for_warmup(app)
            ready = client.get("/ready")
        return WarmupRun(app=app, ready_status=ready.status_code, ready=ready.json())

    return _run


def test_warmup_with_every_model_warm_reports_complete(
    config: SearchConfig, run_warmup: Callable[..., WarmupRun], caplog: pytest.LogCaptureFixture
) -> None:
    """Healthy models: INFO ``complete`` naming the reranker, DONE, ``/ready`` models ok."""
    run = run_warmup(config)

    assert _warmup_infos(caplog) == ["model warm-up complete (1 embedding model + reranker)"]
    assert _warmup_failures(caplog) == []
    assert run.app.state.warmup_result == WarmupResult.DONE
    assert run.ready_status == 200
    assert run.ready["ready"] is True
    assert run.ready["checks"]["models"] == "ok"


@pytest.mark.parametrize(
    ("collections", "expected_label"),
    [
        ((), "1 embedding model"),
        ((_collection("a", _COLLECTION_MODEL_A),), "2 embedding models"),
    ],
)
def test_warmup_without_a_reranker_does_not_claim_one(
    config: SearchConfig,
    run_warmup: Callable[..., WarmupRun],
    caplog: pytest.LogCaptureFixture,
    collections: tuple[CollectionMeta, ...],
    expected_label: str,
) -> None:
    """No reranker configured: nothing to warm or report beyond the embedders."""
    config.reranker_model = ""
    config.eager_load_embedders = True

    run = run_warmup(config, collections=collections)

    assert _warmup_infos(caplog) == [f"model warm-up complete ({expected_label})"]
    assert run.app.state.warmup_result == WarmupResult.DONE


def test_eager_warmup_with_more_models_than_the_cache_holds_reports_complete(
    config: SearchConfig, run_warmup: Callable[..., WarmupRun], caplog: pytest.LogCaptureFixture
) -> None:
    """Every model loaded and warmed: DONE, even though the LRU evicted some of them.

    ``embedder_cache_size = 1`` with three distinct models means the cache pushes two
    just-warmed embedders out during preload. That is a capacity warning (logged by the
    lifespan), not a failed warm-up — every model's load and warm-up embed() succeeded.
    """
    config.eager_load_embedders = True
    config.embedder_cache_size = 1

    run = run_warmup(
        config,
        collections=[_collection("a", _COLLECTION_MODEL_A), _collection("b", _COLLECTION_MODEL_B)],
    )

    assert _warmup_failures(caplog) == []
    assert _warmup_infos(caplog) == ["model warm-up complete (3 embedding models + reranker)"]
    assert run.app.state.warmup_result == WarmupResult.DONE
    assert run.ready["checks"]["models"] == "ok"


@pytest.mark.parametrize(
    ("config_field", "unsupported_model"),
    [
        ("embedding_model", _UNSUPPORTED_EMBEDDER),
        ("reranker_model", _UNSUPPORTED_RERANKER),
    ],
)
def test_warmup_with_an_unloadable_model_reports_failed_naming_it_once(
    config: SearchConfig,
    run_warmup: Callable[..., WarmupRun],
    caplog: pytest.LogCaptureFixture,
    config_field: str,
    unsupported_model: str,
) -> None:
    """A model that cannot load ends warm-up FAILED, not "complete" (S632).

    The global embedder and its preload entry share ``embedding_model``, so an
    unloadable embedder fails twice over — the WARNING still names it once. The
    models that did warm are not named.
    """
    setattr(config, config_field, unsupported_model)

    run = run_warmup(config, unsupported=frozenset({unsupported_model}))

    assert _warmup_failures(caplog) == [f"{_FAILED_PREFIX}{unsupported_model}"]
    assert not any(message.startswith("model warm-up complete") for message in _warmup_infos(caplog))
    assert run.app.state.warmup_result == WarmupResult.FAILED
    assert run.ready_status == 200
    assert run.ready["ready"] is True
    assert run.ready["checks"]["models"] == "fail"


def test_eager_warmup_with_an_unloadable_collection_model_reports_only_that_model(
    config: SearchConfig, run_warmup: Callable[..., WarmupRun], caplog: pytest.LogCaptureFixture
) -> None:
    """eager_load_embedders: a per-collection model that cannot load fails the warm-up.

    The global embedder and the reranker both warm, so the WARNING names only the
    collection's model.
    """
    config.eager_load_embedders = True

    run = run_warmup(
        config,
        unsupported=frozenset({_UNSUPPORTED_COLLECTION_MODEL}),
        collections=[_collection("docs", _UNSUPPORTED_COLLECTION_MODEL)],
    )

    assert _warmup_failures(caplog) == [f"{_FAILED_PREFIX}{_UNSUPPORTED_COLLECTION_MODEL}"]
    assert run.app.state.warmup_result == WarmupResult.FAILED
    assert run.ready["checks"]["models"] == "fail"


def test_eager_warmup_with_a_model_whose_warmup_embed_fails_reports_it(
    config: SearchConfig, run_warmup: Callable[..., WarmupRun], caplog: pytest.LogCaptureFixture
) -> None:
    """A backend that builds but whose warm-up embed() raises is a failed warm-up.

    The embedder ends warm (its ONNX model was constructed) and stays cached, so the
    cache state alone looks healthy — only preload's outcome shows the warm-up failed.
    """
    config.eager_load_embedders = True

    run = run_warmup(
        config,
        broken_inference=frozenset({_COLLECTION_MODEL_A}),
        collections=[_collection("docs", _COLLECTION_MODEL_A)],
    )

    assert _warmup_failures(caplog) == [f"{_FAILED_PREFIX}{_COLLECTION_MODEL_A}"]
    assert not any(message.startswith("model warm-up complete") for message in _warmup_infos(caplog))
    assert run.app.state.warmup_result == WarmupResult.FAILED
    assert run.ready["checks"]["models"] == "fail"


def test_warmup_with_several_failed_models_names_each_once_in_a_fixed_order(
    config: SearchConfig, run_warmup: Callable[..., WarmupRun], caplog: pytest.LogCaptureFixture
) -> None:
    """One WARNING lists every failed model: the global embedder, then the preloaded
    models in preload order (sorted), then the reranker — each named once."""
    config.eager_load_embedders = True
    config.embedding_model = _UNSUPPORTED_EMBEDDER
    config.reranker_model = _UNSUPPORTED_RERANKER
    collection_model_z = "nonexistent-org/zz-collection-model-s632"
    collection_model_a = "nonexistent-org/aa-collection-model-s632"

    run = run_warmup(
        config,
        unsupported=frozenset(
            {_UNSUPPORTED_EMBEDDER, _UNSUPPORTED_RERANKER, collection_model_z, collection_model_a}
        ),
        collections=[_collection("z", collection_model_z), _collection("a", collection_model_a)],
    )

    expected = ", ".join([_UNSUPPORTED_EMBEDDER, collection_model_a, collection_model_z, _UNSUPPORTED_RERANKER])
    assert _warmup_failures(caplog) == [f"{_FAILED_PREFIX}{expected}"]
    assert run.app.state.warmup_result == WarmupResult.FAILED


def _install_preload_that_runs_out_of_budget(monkeypatch: pytest.MonkeyPatch, *, load_first: bool) -> None:
    """Make ``preload`` end the way an expired warm-up budget ends it: ``TimeoutError``.

    Raising from inside the ``asyncio.timeout`` block is exactly what the block's expiry
    raises, without racing a wall-clock budget against the stubbed
    ``pipeline.warmup_models()`` (which always finishes first: it is awaited before
    ``preload``). The real bound is pinned by ``tests/test_c1_bug_repros.py``
    (``test_eager_warmup_has_terminal_timeout``). With ``load_first`` the models are
    loaded and warmed before the budget runs out.
    """
    real_preload = EmbedderCache.preload

    async def _preload_out_of_budget(self: EmbedderCache, model_names: list[str]) -> list[str]:
        if load_first:
            await real_preload(self, model_names)
        raise TimeoutError

    monkeypatch.setattr(EmbedderCache, "preload", _preload_out_of_budget)


def test_warmup_timeout_names_the_models_still_cold(
    config: SearchConfig,
    run_warmup: Callable[..., WarmupRun],
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A warm-up that blows its budget names what it left cold — not an empty reason.

    ``asyncio.timeout`` raises a bare ``TimeoutError`` whose ``str()`` is empty, so the
    old generic handler logged ``model warm-up failed: `` with nothing after it. The
    pipeline warm-up finishes first, so only the embedder preload never warmed.
    """
    _install_preload_that_runs_out_of_budget(monkeypatch, load_first=False)

    run = run_warmup(config)

    assert _warmup_failures(caplog) == [
        f"{_FAILED_PREFIX}timed out after {warmup_module._WARMUP_TASK_TIMEOUT_SECONDS}s; "
        f"still cold: {config.embedding_model}"
    ]
    assert run.app.state.warmup_result == WarmupResult.FAILED
    assert run.ready["checks"]["models"] == "fail"


def test_warmup_timeout_with_nothing_left_cold_omits_the_still_cold_suffix(
    config: SearchConfig,
    run_warmup: Callable[..., WarmupRun],
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A timeout after every model warmed still fails — but never claims ``still cold: none``."""
    _install_preload_that_runs_out_of_budget(monkeypatch, load_first=True)

    run = run_warmup(config)

    assert _warmup_failures(caplog) == [
        f"{_FAILED_PREFIX}timed out after {warmup_module._WARMUP_TASK_TIMEOUT_SECONDS}s"
    ]
    assert run.app.state.warmup_result == WarmupResult.FAILED
    assert run.ready["checks"]["models"] == "fail"


def test_warmup_timeout_names_a_model_the_lru_evicted_after_it_warmed(
    config: SearchConfig,
    run_warmup: Callable[..., WarmupRun],
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The timeout's cold probe reads the cache, so a model that warmed and was then
    LRU-evicted is reported as still cold — the first search pays its build again.

    ``preload`` warms both models concurrently into a one-slot cache, so which one is
    evicted is a race; exactly one of them must be named.
    """
    config.eager_load_embedders = True
    config.embedder_cache_size = 1
    _install_preload_that_runs_out_of_budget(monkeypatch, load_first=True)

    run = run_warmup(config, collections=[_collection("a", _COLLECTION_MODEL_A)])

    [failure] = _warmup_failures(caplog)
    assert failure.startswith(f"{_FAILED_PREFIX}timed out after {warmup_module._WARMUP_TASK_TIMEOUT_SECONDS}s; ")
    still_cold = failure.split("; still cold: ", 1)[1].split(", ")
    assert len(still_cold) == 1
    assert still_cold[0] in {config.embedding_model, _COLLECTION_MODEL_A}
    assert run.app.state.warmup_result == WarmupResult.FAILED
