"""Unit tests for the consolidated graph-engine stub factory and the structural
meta-guard that keeps the removed engine out of the test tree (BE-16, S42)."""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

import archon_search.graph_extractor as graph_extractor
from tests._graph_engine_stub import install_graph_engine_stub

_REPO_ROOT = Path(__file__).resolve().parent.parent


async def _run_stub(texts: list[str]):
    """Build the patched ProseExtractionBackend and run one inference batch."""
    backend = graph_extractor.ProseExtractionBackend()
    return await backend.inference(texts, ner_confidence=0.5, relation_confidence=0.5)


async def test_factory_injects_distinct_entity_and_relation_payloads(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One factory expresses the discriminating behavioural shapes: an injected
    entity payload and a *separately* injected relation payload both reach the
    result, and the entity/relation type are set directly."""
    install_graph_engine_stub(
        monkeypatch,
        entities=[("Acme", "system")],
        relations=[("Acme", "Beta", "uses")],
    )
    batch = await _run_stub(["Acme integrates with Beta."])
    (chunk,) = batch.chunks

    assert [(e.text, e.label) for e in chunk.entities] == [("Acme", "system")]
    assert [(r.head, r.tail, r.label) for r in chunk.relations] == [("Acme", "Beta", "uses")]
    # Distinct payloads: the relation's tail was injected independently — it is
    # not sourced from the entity list, which never contained "Beta".
    entity_texts = {e.text for e in chunk.entities}
    assert chunk.relations[0].tail not in entity_texts


async def test_factory_shapes_stay_discriminating(monkeypatch: pytest.MonkeyPatch) -> None:
    """content_aware vs unconditional vs callable payloads produce different
    results for the same text — a single fixed stub would erase this."""
    # Unconditional: entity tagged even though it is absent from the text.
    install_graph_engine_stub(monkeypatch, entities=[("Ghost", "concept")], content_aware=False)
    (unconditional,) = (await _run_stub(["nothing here"])).chunks
    assert [e.text for e in unconditional.entities] == ["Ghost"], "unconditional shape"

    # Content-aware: only tagged where the surface text occurs.
    install_graph_engine_stub(monkeypatch, entities=[("Ghost", "concept")], content_aware=True)
    (absent,) = (await _run_stub(["nothing here"])).chunks
    (present,) = (await _run_stub(["Ghost walks"])).chunks
    assert absent.entities == [], "content-aware shape: absent surface text yields no span"
    assert [e.text for e in present.entities] == ["Ghost"], "content-aware shape: present surface text tagged"

    # Callable payload: per-chunk routing that need not match the text literally.
    install_graph_engine_stub(monkeypatch, entities=lambda t: [("X", "system")] if "trigger" in t else [])
    (routed,) = (await _run_stub(["trigger"])).chunks
    assert [e.text for e in routed.entities] == ["X"], "callable shape: per-chunk routing"


async def test_factory_empty_mode_returns_no_spans(monkeypatch: pytest.MonkeyPatch) -> None:
    """The explicit empty mode returns zero entities and relations for any chunk."""
    install_graph_engine_stub(monkeypatch, empty=True)
    batch = await _run_stub(["Alice met Bob at Google."])
    (chunk,) = batch.chunks
    assert chunk.entities == []
    assert chunk.relations == []


def test_no_test_file_names_the_removed_engine() -> None:
    """Structural meta-guard (S42): no git-tracked file under ``tests/`` names the
    removed engine or its deleted ``_LABEL_TO_ENTITY_TYPE`` mapping table, in its
    content or its filename. The scan covers ALL tracked *text* files under
    ``tests/`` — not just ``.py`` — so prose docs (e.g. ``tests/eval/README.md``)
    are guarded too.

    Two scoping devices, both pinned literals (not a ``__file__`` self-skip, so a
    second offending file cannot hide behind one):

    * ``_ALLOWLIST`` — files that may legitimately name the engine: this guard
      itself (it must contain the search terms), and the captured spaCy
      throughput baseline (``_graph_ner_throughput_baseline.py``), a frozen
      historical measurement record whose whole purpose is to record what the
      pre-removal engine produced — its facts must not be rewritten to pass a
      lint.
    * ``_DATA_PREFIX`` — the Wikipedia-derived eval *corpus* is retrieval DATA,
      not test code; a source document that legitimately mentions spaCy must not
      trip a code-naming guard. BE-21's repo-wide scan owns corpus coverage.

    Binary fixtures are read with ``errors="ignore"`` so a future non-text
    fixture under ``tests/`` cannot crash the scan with ``UnicodeDecodeError``."""
    _ALLOWLIST = {
        "tests/test_graph_engine_stub.py",
        "tests/eval/_graph_ner_throughput_baseline.py",
    }
    _DATA_PREFIX = "tests/eval/corpus/"
    pattern = re.compile(r"spacy|en_core_web_sm|_LABEL_TO_ENTITY_TYPE", re.IGNORECASE)

    tracked = subprocess.run(
        ["git", "ls-files", "tests"],
        cwd=_REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.splitlines()
    files = [
        f for f in tracked if f not in _ALLOWLIST and not f.startswith(_DATA_PREFIX)
    ]

    offenders: list[str] = []
    for rel in files:
        if pattern.search(rel):
            offenders.append(f"{rel} (filename)")
        content = (_REPO_ROOT / rel).read_text(encoding="utf-8", errors="ignore")
        if pattern.search(content):
            offenders.append(f"{rel} (content)")

    assert not offenders, f"test files still name the removed engine: {offenders}"


def test_removed_engine_absent_from_dependency_manifests() -> None:
    """Interim guard until BE-21's repo-wide scan lands: the removed engine must
    not reappear in the dependency manifests. Restores the coverage the deleted
    ``test_pyproject`` spaCy-absence asserts gave up. This guard file is itself
    self-allowlisted in the meta-guard above, so naming the engine here is safe."""
    pattern = re.compile(r"spacy|en_core_web_sm|en-core-web-sm", re.IGNORECASE)
    for manifest in ("pyproject.toml", "uv.lock"):
        text = (_REPO_ROOT / manifest).read_text(encoding="utf-8")
        assert not pattern.search(text), f"{manifest} still names the removed engine"
