"""SearchFilters — query-time metadata filter model for A2.

Filters are validated at the API boundary (REST and MCP) and forwarded into
``hybrid_search`` via ``store_filters.build_where`` for SQL predicate pushdown.
"""
from __future__ import annotations

import fnmatch
import re
from datetime import datetime, timezone

from pydantic import BaseModel, ConfigDict, field_validator, model_validator


class SearchFilters(BaseModel):
    """Validated filter parameters for hybrid_search.

    All fields are optional and default to ``None``/``False``.
    An empty ``SearchFilters()`` applies no filtering.
    """

    model_config = ConfigDict(extra="forbid")

    file_type: str | None = None
    """Filter by file extension (lowercased, leading dot stripped)."""

    source_path_prefix: str | None = None
    """Filter to chunks whose source_path starts with this string prefix."""

    source_path_glob: str | None = None
    """Filter to chunks whose source_path matches this fnmatch glob (post-RRF)."""

    indexed_after: datetime | None = None
    """Inclusive lower bound on indexed_at (ISO-8601 datetime)."""

    indexed_before: datetime | None = None
    """Inclusive upper bound on indexed_at (ISO-8601 datetime)."""

    language: str | None = None
    """Reserved for C2 — non-empty value raises 422."""

    include_metadata: bool = False
    """When True, include free-form metadata dict in the response."""

    @field_validator("file_type")
    @classmethod
    def validate_file_type(cls, v: str | None) -> str | None:
        if v is None:
            return v
        v = v.lstrip(".").lower()
        if not v:
            raise ValueError("file_type must not be empty")
        return v

    @field_validator("source_path_prefix")
    @classmethod
    def validate_source_path_prefix(cls, v: str | None) -> str | None:
        if v is not None and not v:
            raise ValueError("source_path_prefix must not be empty")
        return v

    @field_validator("source_path_glob")
    @classmethod
    def validate_source_path_glob(cls, v: str | None) -> str | None:
        if v is not None and not v:
            raise ValueError("source_path_glob must not be empty")
        if v is not None:
            re.compile(fnmatch.translate(v))  # defense-in-depth: validate glob compiles
        return v

    @field_validator("language")
    @classmethod
    def validate_language(cls, v: str | None) -> str | None:
        if v:
            raise ValueError("language filtering not yet supported (see C2)")
        return v

    @model_validator(mode="after")
    def validate_date_range(self) -> "SearchFilters":
        # Treat naive datetimes as UTC
        if self.indexed_after is not None and self.indexed_after.tzinfo is None:
            self.indexed_after = self.indexed_after.replace(tzinfo=timezone.utc)
        if self.indexed_before is not None and self.indexed_before.tzinfo is None:
            self.indexed_before = self.indexed_before.replace(tzinfo=timezone.utc)
        # Enforce ordering
        if (
            self.indexed_after is not None
            and self.indexed_before is not None
            and self.indexed_after > self.indexed_before
        ):
            raise ValueError("indexed_after must be <= indexed_before")
        return self
