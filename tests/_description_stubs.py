"""Stand-ins for the description generator that pipeline tests inject (``SearchPipeline(description_generator=...)``).

The real ``DescriptionGenerator`` reads the store and calls the shared LLM client; a pipeline test that only cares what the
ingest does with the outcome supplies one of these, whose ``generate`` answers a fixed ``DescriptionOutcome``.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

from archon_search.description_generator import (
    DescriptionFailed,
    DescriptionFailureReason,
    DescriptionGenerated,
    DescriptionGenerator,
)


def stub_description_generator(text: str | None) -> MagicMock:
    """A ``DescriptionGenerator`` whose ``generate`` answers *text* as a generated description, or a failed call when ``None``."""
    outcome = (
        DescriptionFailed(DescriptionFailureReason.CALL_FAILED, "the provider is down")
        if text is None
        else DescriptionGenerated(text=text)
    )
    return MagicMock(spec=DescriptionGenerator, generate=AsyncMock(return_value=outcome))
