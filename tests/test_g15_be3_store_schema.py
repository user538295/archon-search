"""Unit tests for BE-3: ACL provenance columns in _schema() (G15).

Tests that _schema() includes acl_source (utf8 nullable), acl_sidecar_path
(utf8 nullable), and acl_warning (list<utf8> nullable).
"""
from __future__ import annotations

import pyarrow as pa
import pytest

from archon_search.store import SearchStore


def test_schema_contains_acl_provenance_fields():
    """_schema() must include all three ACL provenance columns with correct types."""
    schema = SearchStore._schema(4)

    assert "acl_source" in schema.names, "acl_source column missing from _schema()"
    assert "acl_sidecar_path" in schema.names, "acl_sidecar_path column missing from _schema()"
    assert "acl_warning" in schema.names, "acl_warning column missing from _schema()"

    # acl_source: utf8, nullable
    acl_source_field = schema.field("acl_source")
    assert acl_source_field.type == pa.utf8(), (
        f"acl_source must be utf8, got {acl_source_field.type}"
    )
    assert acl_source_field.nullable, "acl_source must be nullable"

    # acl_sidecar_path: utf8, nullable
    acl_sidecar_path_field = schema.field("acl_sidecar_path")
    assert acl_sidecar_path_field.type == pa.utf8(), (
        f"acl_sidecar_path must be utf8, got {acl_sidecar_path_field.type}"
    )
    assert acl_sidecar_path_field.nullable, "acl_sidecar_path must be nullable"

    # acl_warning: list<utf8>, nullable
    acl_warning_field = schema.field("acl_warning")
    assert pa.types.is_list(acl_warning_field.type), (
        f"acl_warning must be a list type, got {acl_warning_field.type}"
    )
    assert acl_warning_field.type.value_type == pa.utf8(), (
        f"acl_warning list value type must be utf8, got {acl_warning_field.type.value_type}"
    )
    assert acl_warning_field.nullable, "acl_warning must be nullable"


def test_acl_provenance_migration_spec_in_all_migrations():
    """migrate_acl_provenance must appear in _all_migrations() with introduced_at=0."""
    specs = SearchStore._all_migrations()
    spec = next((s for s in specs if s.name == "migrate_acl_provenance"), None)
    assert spec is not None, "migrate_acl_provenance not found in _all_migrations()"
    assert spec.introduced_at == 0, (
        f"migrate_acl_provenance must have introduced_at=0 (catalog-only), got {spec.introduced_at}"
    )
