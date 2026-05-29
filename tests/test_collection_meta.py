"""Tests for CollectionMeta dataclass."""
from archon_search.collection_meta import CollectionMeta
from archon_search.router import _ROUTING_FIELDS


def test_description_embedding_default_none():
    meta = CollectionMeta(name="x")
    assert meta.description_embedding is None


def test_description_embedding_stored():
    meta = CollectionMeta(name="x", description_embedding=[0.1, 0.2])
    assert meta.description_embedding == [0.1, 0.2]


def test_centroid_sum_defaults_to_none():
    meta = CollectionMeta(name="x")
    assert meta.centroid_sum is None


def test_mutations_since_recompute_defaults_to_zero():
    meta = CollectionMeta(name="x")
    assert meta.mutations_since_recompute == 0


def test_needs_recompute_defaults_to_false():
    meta = CollectionMeta(name="x")
    assert meta.needs_recompute is False


def test_b5_internal_fields_excluded_from_routing():
    for field in ("centroid_sum", "mutations_since_recompute", "needs_recompute"):
        assert field not in _ROUTING_FIELDS
