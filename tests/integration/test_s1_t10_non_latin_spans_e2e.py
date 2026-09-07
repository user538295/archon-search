"""E2e test for T-10 (S1, default lane): the ingest → graph plumbing is language-blind.

The real-engine half of S1 lives in the ``graph_real_artifact`` lane
(``tests/test_graph_ner_real_artifact_lane.py`` — French, real GLiNER checkpoint),
because a stub-backed assertion certifies the fixture, not the engine. This test is
the OTHER half: with the engine stubbed to return spans whose surface text is
Hungarian, Japanese and Cyrillic, everything downstream of the engine — chunk text
storage, ``make_stable_entity_id``, the LanceDB graph tables, and the JSON on
``GET /graph/{collection}`` — must carry those spans through byte-exactly.

That is a real risk and not a formality: entity ids are hashed from the surface text,
node names round-trip through Arrow/LanceDB and then through JSON, and any of those
layers could normalise, mojibake or truncate a non-ASCII name. The stub is deliberate
here — it pins the plumbing while holding the engine's own multilingual capability
constant, which is what makes a failure attributable to the plumbing.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from tests._graph_engine_stub import install_graph_engine_stub
from tests.integration.conftest import ingest_file_via_path, make_real_app

pytestmark = pytest.mark.integration

# Three scripts, chosen so a plumbing bug cannot pass by handling only one of them:
# Hungarian exercises Latin-with-diacritics (NFC/NFD normalisation), Japanese exercises
# a non-Latin script with no ASCII fallback, Cyrillic exercises homoglyphs that look
# like ASCII but are not. Each is paired with the engine's own lowercase entity type.
_HUNGARIAN_ENTITY = "Vektoros keresőmotor"
_JAPANESE_ENTITY = "検索パイプライン"
_CYRILLIC_ENTITY = "Векторное хранилище"

_NON_LATIN_ENTITIES: list[tuple[str, str]] = [
    (_HUNGARIAN_ENTITY, "system"),
    (_JAPANESE_ENTITY, "concept"),
    (_CYRILLIC_ENTITY, "system"),
]

# An ASCII entity alongside them, so the assertions below have a PRESENCE anchor that
# would still pass if non-Latin handling were broken — without it, a total plumbing
# failure and a specifically non-Latin one look identical.
_ASCII_ENTITY = "LanceDB"

# The document body. Every entity's surface text occurs literally, which is what makes
# the stub's content-aware routing tag it at all — a name absent from the text is dropped
# rather than emitted at offset 0. (The offsets themselves are NOT asserted downstream:
# `graph_extractor.py` reads only `ExtractedEntity.text`, and `GraphMention` has no offset
# fields. The engine's own offsets are pinned in the real-artifact lane's French leg,
# which is the only place they are observable.)
_DOCUMENT = "\n\n".join(
    [
        f"A {_HUNGARIAN_ENTITY} a lekérdezéseket beágyazott vektorokká alakítja, "
        f"majd a {_ASCII_ENTITY} indexében keres rá a legközelebbi szomszédokra.",
        f"{_JAPANESE_ENTITY}は、文書を解析し、分割し、埋め込みベクトルに変換します。",
        f"{_CYRILLIC_ENTITY} {_ASCII_ENTITY} хранит векторы и полнотекстовый индекс.",
    ]
)


def _auth(api_key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {api_key}"}


def test_e2e_non_latin_spans_through_the_stubbed_engine(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Non-Latin entity spans survive ingest, persistence and the graph route intact.

    ``content_aware=True`` makes the stub tag each name only where it literally occurs
    (with that occurrence's real character offsets), so the per-entity ``chunk_count``
    below is a genuine mention count the plumbing derived, not a constant the stub
    dictated — a name absent from the text yields no node at all.
    """
    install_graph_engine_stub(
        monkeypatch,
        entities=[*_NON_LATIN_ENTITIES, (_ASCII_ENTITY, "system")],
        content_aware=True,
    )
    col = "t10-non-latin-spans"
    doc = tmp_path / "non_latin.md"
    doc.write_text(_DOCUMENT, encoding="utf-8")

    with make_real_app(tmp_path, monkeypatch, graph_enabled=True) as (client, _cfg, api_key):
        ingest_file_via_path(client, col, str(doc), api_key=api_key)

        resp = client.get(f"/graph/{col}", headers=_auth(api_key))
        assert resp.status_code == 200, (
            f"GET /graph/{col} failed: {resp.status_code} {resp.text}"
        )
        nodes = resp.json()["nodes"]

    by_name = {node["entity_name"]: node for node in nodes}

    # The ASCII anchor first: if this is missing, the ingest itself failed and the
    # non-Latin assertions below would be reporting the wrong defect.
    assert _ASCII_ENTITY in by_name, (
        f"the ASCII anchor {_ASCII_ENTITY!r} is absent from the graph — the ingest or the "
        f"stub wiring failed, so nothing below would be a statement about non-Latin text. "
        f"names returned: {sorted(by_name)}"
    )

    missing = [name for name, _type in _NON_LATIN_ENTITIES if name not in by_name]
    assert not missing, (
        f"non-Latin entity names did not survive the ingest → graph → JSON round trip: "
        f"{missing} absent while the ASCII anchor {_ASCII_ENTITY!r} came through. "
        f"names returned: {sorted(by_name)}"
    )

    # Byte-exact, not merely present: a normalising or transcoding layer would return a
    # name that compares unequal to the source spelling while still looking plausible.
    for name, entity_type in _NON_LATIN_ENTITIES:
        node = by_name[name]
        assert node["entity_name"] == name, (
            f"{name!r} came back as {node['entity_name']!r} — the plumbing rewrote the span"
        )
        assert node["entity_type"] == entity_type, (
            f"{name!r} came back typed {node['entity_type']!r}, expected {entity_type!r} — "
            "the entity type is not carried per-span"
        )
        # A mention row was written for this name: `chunk_count` is derived from the
        # mentions table, so 0 would mean the node persisted with no incidence record and
        # every salience/co-occurrence figure over it would be empty. Each non-Latin name
        # occurs exactly ONCE in the document, so 1 is the value regardless of how the
        # chunker splits the text. (No claim about the ASCII anchor's count — it occurs
        # twice, so its value does depend on chunking. Entity-id collapse is asserted
        # directly below rather than inferred from this count.)
        assert node["chunk_count"] == 1, (
            f"{name!r} has chunk_count={node['chunk_count']}, expected 1 — it is mentioned "
            "exactly once in the document, so its mention row was dropped or duplicated"
        )

    assert len({by_name[name]["entity_id"] for name, _type in _NON_LATIN_ENTITIES}) == len(
        _NON_LATIN_ENTITIES
    ), "the three non-Latin names collapsed onto fewer entity ids"
