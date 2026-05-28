"""Tests for CollectionMeta dataclass."""
from archon_search.collection_meta import CollectionMeta


def test_description_embedding_default_none():
    meta = CollectionMeta(name="x")
    assert meta.description_embedding is None


def test_description_embedding_stored():
    meta = CollectionMeta(name="x", description_embedding=[0.1, 0.2])
    assert meta.description_embedding == [0.1, 0.2]
