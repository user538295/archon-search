"""Helper to normalize the ``X-Ingested-By`` REST/MCP header value.

Boundary normalization rules (Task 3.3):
- header missing or empty → ``"http"``
- value ∈ ``INGESTED_BY_VALUES`` → use as-is
- value == ``LEGACY_INGESTED_BY`` → normalize to ``"cli"``
- unknown value → ``"http"`` with a WARNING log (value truncated to 32 chars)
"""
from __future__ import annotations

import logging
from typing import cast

from archon_search._types import IngestedBy
from archon_search.constants import INGESTED_BY_VALUES, LEGACY_INGESTED_BY

logger = logging.getLogger(__name__)

_TRUNCATE_LEN = 32


def parse_ingested_by_header(value: str | None) -> IngestedBy:
    """Map a raw ``X-Ingested-By`` header to a canonical ``IngestedBy``."""
    if not value:
        return "http"
    if value in INGESTED_BY_VALUES:
        return cast(IngestedBy, value)
    if value == LEGACY_INGESTED_BY:
        return "cli"
    truncated = value[:_TRUNCATE_LEN]
    logger.warning("unknown X-Ingested-By header value: %r (coerced to 'http')", truncated)
    return "http"
