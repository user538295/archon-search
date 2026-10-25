"""Tests for archon_search.text_batching."""

from __future__ import annotations

import random

import pytest

from archon_search.text_batching import DEFAULT_MAX_ITEMS, DEFAULT_TOKEN_BUDGET, plan_batches

_PROPERTY_SEED = 20261024
_PROPERTY_ROUNDS = 200


def test_defaults_are_the_measured_values() -> None:
    assert DEFAULT_TOKEN_BUDGET == 4096
    assert DEFAULT_MAX_ITEMS == 256


def test_batches_are_a_permutation_of_the_input() -> None:
    lengths = [5, 90, 3, 40, 7, 200, 1]

    batches = plan_batches(lengths, token_budget=100, max_items=10)

    assert sorted(i for batch in batches for i in batch) == list(range(len(lengths)))


def test_each_batch_respects_the_budget_except_single_oversize_items() -> None:
    lengths = [30, 30, 30, 500, 10]

    batches = plan_batches(lengths, token_budget=100, max_items=10)

    # ascending: 10, 30, 30 fit (3 * 30 = 90); a fourth would pad to 120 > 100; 500 is oversize.
    assert batches == [[4, 0, 1], [2], [3]]


def test_short_items_are_grouped_before_long_ones() -> None:
    lengths = [100, 1, 50, 2]

    batches = plan_batches(lengths, token_budget=1000, max_items=10)

    assert batches == [[1, 3, 2, 0]]


def test_max_items_caps_batch_size() -> None:
    batches = plan_batches([1] * 7, token_budget=1000, max_items=3)

    assert [len(b) for b in batches] == [3, 3, 1]


def test_equal_lengths_keep_input_order() -> None:
    assert plan_batches([4, 4, 4], token_budget=1000, max_items=10) == [[0, 1, 2]]


def test_empty_input() -> None:
    assert plan_batches([]) == []


@pytest.mark.parametrize("budget,items", [(0, 8), (-1, 8), (8, 0), (8, -3)])
def test_invalid_budget_raises(budget: int, items: int) -> None:
    with pytest.raises(ValueError):
        plan_batches([1, 2], token_budget=budget, max_items=items)


def test_randomized_batches_keep_the_invariants() -> None:
    rng = random.Random(_PROPERTY_SEED)
    for _ in range(_PROPERTY_ROUNDS):
        lengths = [rng.randint(1, 600) for _ in range(rng.randint(0, 80))]
        budget = rng.randint(1, 1200)
        max_items = rng.randint(1, 20)

        batches = plan_batches(lengths, token_budget=budget, max_items=max_items)

        flat = [i for batch in batches for i in batch]
        assert sorted(flat) == list(range(len(lengths)))
        assert [lengths[i] for i in flat] == sorted(lengths)
        for batch in batches:
            assert 1 <= len(batch) <= max_items
            assert len(batch) == 1 or len(batch) * max(lengths[i] for i in batch) <= budget
