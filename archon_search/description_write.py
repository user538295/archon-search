"""C3: the request and the result of the guarded description write (``SearchStore.write_description``).

One write changes only the description fields, and each field says whether it is kept, set or cleared, so
"unchanged" is never confused with "empty". A generated write and a recorded failure carry the state the generation
started from (``ExpectedState``) and are refused when the row inside the collection lock differs; set, clear and
import are the authority and carry ``UNCONDITIONAL``.
"""
from __future__ import annotations

import dataclasses
import math
from datetime import datetime
from enum import StrEnum
from typing import Any, Final, Literal

from archon_search.collection_meta import (
    DESCRIPTION_SOURCE_MANUAL,
    DESCRIPTION_SOURCES,
    DESCRIPTION_STORED_STATUSES,
    CollectionMeta,
    DescriptionSource,
    DescriptionStoredStatus,
    check_stored_description_value,
)


class DescriptionWriteOutcome(StrEnum):
    """What a guarded description write did. Only ``WRITTEN`` changed the row."""

    WRITTEN = "written"
    #: The collection lock was not free in time. Reported to the caller, never swallowed.
    LOCK_TIMEOUT = "lockTimeout"
    COLLECTION_MISSING = "collectionMissing"
    #: The row's source is ``manual`` inside the lock.
    REFUSED_MANUAL = "refusedManual"
    #: The row's description, source or last-described differ from the state the generation started from.
    REFUSED_STALE = "refusedStale"


class FieldOp(StrEnum):
    KEEP = "keep"
    SET = "set"
    CLEAR = "clear"


@dataclasses.dataclass(frozen=True)
class FieldChange[T]:
    """One field of a description write: keep the stored value, set *value*, or clear it.

    ``value`` is present exactly when the action is ``SET``. Clear unsets the field (a timestamp or the baseline
    returns to "never").
    """

    action: FieldOp
    value: T | None = None

    def __post_init__(self) -> None:
        if (self.action is FieldOp.SET) != (self.value is not None):
            raise ValueError(f"FieldChange {self.action.value!r}: a value is given exactly when the action is 'set'")

    @classmethod
    def keep(cls) -> FieldChange[Any]:
        return cls(FieldOp.KEEP)

    @classmethod
    def set_to(cls, value: T) -> FieldChange[T]:
        return cls(FieldOp.SET, value)

    @classmethod
    def clear(cls) -> FieldChange[Any]:
        return cls(FieldOp.CLEAR)


# ``DescriptionFields`` attribute -> ``CollectionMeta`` field it changes.
_META_FIELD_OF: Final[dict[str, str]] = {
    "description": "description",
    "source": "description_source",
    "status": "description_status",
    "last_described": "last_described",
    "described_at_doc_count": "described_at_doc_count",
    "description_embedding": "description_embedding",
}


@dataclasses.dataclass(frozen=True)
class DescriptionFields:
    """The fields one description write owns. Every field is required, so a writer states each one.

    Recording a failure is status ``set`` to ``failed`` and the rest ``keep``; clear is every field ``clear``; a
    generated write sets the text, source, status, last-described and baseline, and the routing vector with them
    (C11), because the text and its vector change in one step.
    """

    description: FieldChange[str]
    source: FieldChange[DescriptionSource]
    status: FieldChange[DescriptionStoredStatus]
    last_described: FieldChange[datetime]
    described_at_doc_count: FieldChange[int]
    description_embedding: FieldChange[list[float]]

    def __post_init__(self) -> None:
        # Refused up front, before a lock is taken or a refusal could mask the mistake. ``pending`` is memory only
        # and is never stored.
        check_stored_description_value("description_source", self.source.value, DESCRIPTION_SOURCES)
        check_stored_description_value("description_status", self.status.value, DESCRIPTION_STORED_STATUSES)
        text = self.description.value
        if text is not None and not text.strip():
            raise ValueError("description cannot be set to blank text: clear it instead")
        baseline = self.described_at_doc_count.value
        if baseline is not None and baseline < 0:
            raise ValueError(f"described_at_doc_count {baseline!r} cannot be stored: a negative count reads as unset")
        vector = self.description_embedding.value
        # The reader drops anything but plain finite numbers, so the writer refuses it up front.
        if vector is not None and (not vector or not all(type(x) in (int, float) and math.isfinite(x) for x in vector)):
            raise ValueError("description_embedding must be a non-empty list of finite numbers")
        # Source, status and the routing vector follow every description change (C3, C11, S43, S69): a set or cleared
        # text never keeps the source, the status or the vector of the old one.
        if self.description.action is not FieldOp.KEEP:
            kept = [
                name
                for name, change in (
                    ("source", self.source),
                    ("status", self.status),
                    ("description_embedding", self.description_embedding),
                )
                if change.action is FieldOp.KEEP
            ]
            if kept:
                raise ValueError(f"a description that is set or cleared must also set or clear {', '.join(kept)}")

    def apply_to(self, meta: CollectionMeta) -> CollectionMeta:
        """*meta* with each field kept, set or cleared; every field this write does not own is untouched."""
        changes: dict[str, Any] = {}
        for attribute, meta_field in _META_FIELD_OF.items():
            change: FieldChange[Any] = getattr(self, attribute)
            if change.action is FieldOp.SET:
                changes[meta_field] = change.value
            elif change.action is FieldOp.CLEAR:
                changes[meta_field] = None
        return dataclasses.replace(meta, **changes)


@dataclasses.dataclass(frozen=True)
class ExpectedState:
    """The description state a generation started from, read before its slow step.

    The guard compares exactly these three values (C3 ``WriteGuard``), not the stored status, so a row that was
    changed and then returned to exactly this state while the generation ran (a clear of an empty row that holds a
    ``failed`` status) is not detected as changed.
    """

    description: str | None
    source: DescriptionSource | None
    last_described: datetime | None

    def refusal(self, row: CollectionMeta) -> DescriptionWriteOutcome | None:
        """Why a guarded write over *row* (read inside the lock) must not happen, or ``None`` when it may.

        A manual source always wins. Otherwise the row must still hold what the generation started from; an empty
        description and none are the same thing to the store, so they compare equal.
        """
        if row.description_source == DESCRIPTION_SOURCE_MANUAL:
            return DescriptionWriteOutcome.REFUSED_MANUAL
        unchanged = (
            (row.description or None) == (self.description or None)
            and row.description_source == self.source
            and row.last_described == self.last_described
        )
        return None if unchanged else DescriptionWriteOutcome.REFUSED_STALE


UNCONDITIONAL: Final = "unconditional"
#: Set, clear and import write unconditionally; a generated write and a recorded failure carry their expected state.
type WriteGuard = ExpectedState | Literal["unconditional"]
