"""BE-9 (collection-descriptions plan, C1): the integration ``anthropic`` stub carries ``AsyncAnthropic``.

``tests/integration/conftest.py`` replaces the ``anthropic`` module for every integration test, so the session guard on the
real SDK never sees it. The stub therefore has to hold what ``AnthropicClient`` builds on its first call, and has to refuse
to build it exactly as the session guard does: a test that reaches the client either patches ``AsyncAnthropic`` (which needs
the attribute to exist) or gets the refusal, never a real endpoint.
"""

from __future__ import annotations

import sys
from unittest.mock import MagicMock

import pytest

from archon_search.llm_client.anthropic_client import AnthropicClient
from archon_search.llm_client.protocol import LlmCallFailure, LlmCallRequest

pytestmark = pytest.mark.integration

_MAX_OUTPUT_TOKENS = 1024
_TIMEOUT_SECONDS = 5.0
_REFUSAL = "Test suite attempted to instantiate the Anthropic client"


def _request() -> LlmCallRequest:
    return LlmCallRequest(prompt="describe this collection", max_output_tokens=_MAX_OUTPUT_TOKENS, timeout_seconds=_TIMEOUT_SECONDS)


def test_stub_async_anthropic_refuses_to_build_a_client() -> None:
    import anthropic

    with pytest.raises(RuntimeError, match=_REFUSAL):
        anthropic.AsyncAnthropic(max_retries=0)


async def test_anthropic_wrapper_reaches_the_refusal_and_reports_a_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-not-a-real-key")

    with pytest.raises(LlmCallFailure, match="unexpected RuntimeError"):
        await AnthropicClient(model="a-model").call(_request())


def test_stub_async_anthropic_can_be_patched_by_a_test(monkeypatch: pytest.MonkeyPatch) -> None:
    replacement = MagicMock()

    monkeypatch.setattr(sys.modules["anthropic"], "AsyncAnthropic", replacement)  # raising=True: the attribute must exist

    import anthropic

    assert anthropic.AsyncAnthropic(max_retries=0) is replacement.return_value
