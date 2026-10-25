"""Token-budget batching for the PyTorch embedder and reranker.

Pure Python: no ``torch`` import, so the planner is testable without weights.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence

#: Padded tokens per forward pass. Measured on multilingual-e5-large (Apple GPU):
#: 4096 gave the best throughput at 3.2 GB of accelerator memory; 65536 was 32%
#: slower and peaked at 7.7 GB.
DEFAULT_TOKEN_BUDGET = 4096
#: Hard cap on items per batch, independent of the token budget.
DEFAULT_MAX_ITEMS = 256


def plan_batches(
    lengths: Sequence[int],
    token_budget: int = DEFAULT_TOKEN_BUDGET,
    max_items: int = DEFAULT_MAX_ITEMS,
) -> list[list[int]]:
    """Group item indices into batches of similar length.

    Indices are sorted by length ascending (stable, so equal lengths keep input
    order). A batch grows while ``len(batch) * longest <= token_budget`` (the
    padded size) and ``len(batch) < max_items``; every batch holds at least one
    item, so an item longer than the budget gets a batch of its own. The
    concatenation of the returned batches is a permutation of
    ``range(len(lengths))``.

    Raises:
        ValueError: if ``token_budget`` or ``max_items`` is not positive.
    """
    if token_budget <= 0:
        raise ValueError(f"token_budget must be positive, got {token_budget}")
    if max_items <= 0:
        raise ValueError(f"max_items must be positive, got {max_items}")

    batches: list[list[int]] = []
    current: list[int] = []
    for index in sorted(range(len(lengths)), key=lengths.__getitem__):
        # Ascending order: the candidate is the longest item of the batch.
        fits = (len(current) + 1) * lengths[index] <= token_budget
        if current and (not fits or len(current) == max_items):
            batches.append(current)
            current = []
        current.append(index)
    if current:
        batches.append(current)
    return batches


def map_in_token_batches[T](
    lengths: Sequence[int],
    run_batch: Callable[[list[int]], list[T]],
    *,
    release_memory: Callable[[], None],
) -> list[T]:
    """Run *run_batch* over ``plan_batches(lengths)`` and return one result per item, in input order.

    *run_batch* gets a batch of item indices and returns one result per index, in batch order.
    *release_memory* is called right after a batch whose padded size (``len(batch) * longest``)
    exceeded ``DEFAULT_TOKEN_BUDGET``: only a lone item longer than the budget can do that, and it
    leaves the accelerator holding memory sized for that outlier.
    """
    results: dict[int, T] = {}
    for batch in plan_batches(lengths, token_budget=DEFAULT_TOKEN_BUDGET):
        results.update(zip(batch, run_batch(batch), strict=True))
        if len(batch) * max(lengths[index] for index in batch) > DEFAULT_TOKEN_BUDGET:
            release_memory()
    return [results[index] for index in range(len(lengths))]
