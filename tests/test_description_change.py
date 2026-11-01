"""BE-32 (C6, C8): the ownership rule and writer call REST and MCP share, ``apply_description_change``."""
from __future__ import annotations

import pytest

from archon_search.collection_meta import CollectionMeta
from archon_search.constants import DEFAULT_NAMESPACE
from archon_search.description_change import (
    DescriptionChangeStatus,
    apply_description_change,
)
from archon_search.description_write import DescriptionWriteOutcome
from archon_search.description_writer import (
    ClearChange,
    DescriptionChange,
    ManualSetChange,
    VectorOutcome,
    WriteResult,
)

_COLLECTION = "planning"
_OTHER_NAMESPACE = "someone-else"
_CHANGES = [pytest.param(ManualSetChange(text="A hand written note."), id="set"), pytest.param(ClearChange(), id="clear")]


def _meta(namespace: str = DEFAULT_NAMESPACE) -> CollectionMeta:
    return CollectionMeta(name=_COLLECTION, namespace=namespace, active_embedding_model="model")


class _Store:
    """Answers the row of each namespace in *rows*, one answer per read for a namespace given a list."""

    def __init__(self, rows: dict[str, list[CollectionMeta | None]]) -> None:
        self._rows = rows
        self.reads: list[tuple[str, str]] = []

    async def get_collection_meta(self, collection: str, namespace: str = DEFAULT_NAMESPACE) -> CollectionMeta | None:
        self.reads.append((collection, namespace))
        answers = self._rows.get(namespace, [None])
        return answers.pop(0) if len(answers) > 1 else answers[0]


class _Writer:
    def __init__(self, outcome: DescriptionWriteOutcome = DescriptionWriteOutcome.WRITTEN) -> None:
        self._outcome = outcome
        self.calls: list[tuple[str, str, DescriptionChange]] = []

    async def apply(self, collection: str, namespace: str, change: DescriptionChange) -> WriteResult:
        self.calls.append((collection, namespace, change))
        return WriteResult(self._outcome, VectorOutcome.REFRESHED)


@pytest.mark.asyncio
@pytest.mark.parametrize("change", _CHANGES)
async def test_the_owner_changes_the_row_and_gets_it_back(change: ManualSetChange | ClearChange) -> None:
    stored = _meta()
    writer = _Writer()

    result = await apply_description_change(_Store({DEFAULT_NAMESPACE: [stored]}), writer, _COLLECTION, DEFAULT_NAMESPACE, change)

    assert (result.status, result.meta) == (DescriptionChangeStatus.CHANGED, stored)
    assert writer.calls == [(_COLLECTION, DEFAULT_NAMESPACE, change)]


@pytest.mark.asyncio
@pytest.mark.parametrize("change", _CHANGES)
@pytest.mark.parametrize("name", ["never-existed", "has.dot", "x" * 65, "-leading-dash"])
async def test_a_collection_the_namespace_does_not_hold_is_not_found_and_writes_nothing(
    change: ManualSetChange | ClearChange, name: str
) -> None:
    writer = _Writer()

    result = await apply_description_change(_Store({_OTHER_NAMESPACE: [_meta(_OTHER_NAMESPACE)]}), writer, name, DEFAULT_NAMESPACE, change)

    assert (result.status, result.meta) == (DescriptionChangeStatus.NOT_FOUND, None)
    assert writer.calls == []


@pytest.mark.asyncio
async def test_a_name_the_store_refuses_is_not_found_without_reading_the_store() -> None:
    store = _Store({DEFAULT_NAMESPACE: [_meta()]})

    result = await apply_description_change(store, _Writer(), "has.dot", DEFAULT_NAMESPACE, ClearChange())

    assert result.status is DescriptionChangeStatus.NOT_FOUND
    assert store.reads == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("outcome", "status"),
    [
        (DescriptionWriteOutcome.LOCK_TIMEOUT, DescriptionChangeStatus.STORE_BUSY),
        (DescriptionWriteOutcome.COLLECTION_MISSING, DescriptionChangeStatus.NOT_FOUND),
    ],
)
async def test_a_writer_outcome_that_did_not_write_maps_to_its_status(
    outcome: DescriptionWriteOutcome, status: DescriptionChangeStatus
) -> None:
    result = await apply_description_change(
        _Store({DEFAULT_NAMESPACE: [_meta()]}), _Writer(outcome), _COLLECTION, DEFAULT_NAMESPACE, ClearChange()
    )

    assert (result.status, result.meta) == (status, None)


@pytest.mark.asyncio
async def test_a_row_dropped_between_the_write_and_the_read_back_is_not_found() -> None:
    result = await apply_description_change(
        _Store({DEFAULT_NAMESPACE: [_meta(), None]}), _Writer(), _COLLECTION, DEFAULT_NAMESPACE, ClearChange()
    )

    assert (result.status, result.meta) == (DescriptionChangeStatus.NOT_FOUND, None)


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", [DescriptionWriteOutcome.REFUSED_MANUAL, DescriptionWriteOutcome.REFUSED_STALE])
async def test_a_refused_unconditional_write_is_a_broken_contract(outcome: DescriptionWriteOutcome) -> None:
    with pytest.raises(RuntimeError, match=outcome.value):
        await apply_description_change(
            _Store({DEFAULT_NAMESPACE: [_meta()]}), _Writer(outcome), _COLLECTION, DEFAULT_NAMESPACE, ClearChange()
        )
