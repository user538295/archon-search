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


def test_active_embedding_model_defaults_to_empty_string():
    meta = CollectionMeta("foo")
    assert meta.active_embedding_model == ""


def test_pending_embedding_model_defaults_to_none():
    meta = CollectionMeta("foo")
    assert meta.pending_embedding_model is None


def test_needs_reindex_defaults_to_false():
    meta = CollectionMeta("foo")
    assert meta.needs_reindex is False


def test_reindex_job_id_defaults_to_none():
    meta = CollectionMeta("foo")
    assert meta.reindex_job_id is None


def test_collection_meta_schema_version_default():
    """CollectionMeta() has schema_version == 0 by default."""
    meta = CollectionMeta(name="x")
    assert meta.schema_version == 0
