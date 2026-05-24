"""SearchFilters — Pydantic model for query-side filter parameters.

Kept separate from _types.py to avoid pulling Pydantic into the core
types module (dataclass-only by convention).
"""
from __future__ import annotations

import fnmatch
import re
from datetime import date, datetime, time, timezone
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class SearchFilters(BaseModel):
    """Filter dimensions applied to a hybrid search query."""

    model_config = ConfigDict(extra="forbid")

    file_type: str | None = None
    source_path_prefix: str | None = None
    source_path_glob: str | None = None
    indexed_after: datetime | date | None = None
    indexed_before: datetime | date | None = None
    language: str | None = Field(
        default=None,
        description=(
            "reserved — language extraction is not yet implemented. "
            "This field is tracked as roadmap item C2. "
            "Passing a non-empty value raises a validation error."
        ),
    )
    include_metadata: bool = False

    @field_validator("indexed_after", "indexed_before", mode="before")
    @classmethod
    def _coerce_date_string(cls, v: Any) -> Any:
        if isinstance(v, str) and len(v) == 10 and re.match(r'^\d{4}-\d{2}-\d{2}$', v):
            year, month, day = v.split("-")
            return date(int(year), int(month), int(day))
        return v

    @field_validator("file_type")
    @classmethod
    def _validate_file_type(cls, v: str | None) -> str | None:
        if v is None:
            return v
        if v == "":
            raise ValueError("file_type must not be empty")
        v = v.lstrip(".").lower()
        if not v:
            raise ValueError("file_type must not be empty after stripping leading dots")
        return v

    @field_validator("source_path_prefix")
    @classmethod
    def _validate_source_path_prefix(cls, v: str | None) -> str | None:
        if v is not None and v == "":
            raise ValueError("source_path_prefix must not be empty")
        return v

    @field_validator("source_path_glob")
    @classmethod
    def _validate_source_path_glob(cls, v: str | None) -> str | None:
        if v is None:
            return v
        if v == "":
            raise ValueError("source_path_glob must not be empty")
        # Defense-in-depth: ensure the glob is compilable
        re.compile(fnmatch.translate(v))
        return v

    @field_validator("language")
    @classmethod
    def _validate_language(cls, v: str | None) -> str | None:
        if v == "":
            return None
        if v is not None:
            raise ValueError("language filtering not yet supported (see C2)")
        return v

    @model_validator(mode="after")
    def _coerce_and_validate_dates(self) -> "SearchFilters":
        self.indexed_after = _coerce_date(self.indexed_after, end_of_day=False)
        self.indexed_before = _coerce_date(self.indexed_before, end_of_day=True)

        if (
            self.indexed_after is not None
            and self.indexed_before is not None
            and self.indexed_after > self.indexed_before
        ):
            raise ValueError("indexed_after must be <= indexed_before")

        return self


def _coerce_date(value: Any, *, end_of_day: bool) -> datetime | None:
    """Convert date → datetime and make naive datetimes UTC-aware."""
    if value is None:
        return None
    if isinstance(value, date) and not isinstance(value, datetime):
        # date-only input: coerce to start or end of day in UTC
        if end_of_day:
            t = time(23, 59, 59, 999999)
        else:
            t = time(0, 0, 0, 0)
        return datetime.combine(value, t, tzinfo=timezone.utc)
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value
    return value  # pragma: no cover — unexpected type, let Pydantic handle it
