"""The CLI search request timeout must outlast the worst-case server path (S52)."""

from archon_search.constants import (
    CLI_SEARCH_REQUEST_TIMEOUT_SECONDS,
    SEARCH_TIMEOUT_SECONDS,
)
from archon_search.services.search_budget import (
    EMBEDDER_WARMUP_WAIT_SECONDS,
    RERANKER_WARMUP_WAIT_SECONDS,
)


def test_cli_search_request_timeout_exceeds_hyde_plus_budget_and_the_real_warmup_bounds() -> None:
    worst_case_server_path = (
        2 * SEARCH_TIMEOUT_SECONDS + EMBEDDER_WARMUP_WAIT_SECONDS + RERANKER_WARMUP_WAIT_SECONDS
    )

    assert CLI_SEARCH_REQUEST_TIMEOUT_SECONDS > worst_case_server_path
