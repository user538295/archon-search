"""HyDE stand-ins shared by the REST and MCP HyDE tests: an enabled ``[hyde]`` plus a stub generator.

HyDE resolves through ``server/_hyde.RequestHyDE``, which skips a request with no generator
or a disabled ``[hyde]`` before any LLM call — so a test that wants HyDE to run must supply
both, and controls the outcome through the stub's ``generate``.

``ScriptedLlmClient`` is the stand-in for the shared LLM client the real ``HyDEGenerator``, ``RAGFusionGenerator`` and ``DescriptionGenerator`` call
(``app.state.hyde_client``, ``app.state.rag_fusion_client``, the ``[description]`` client): it answers with a fixed text or raises a fixed failure and records every request.
``BudgetedLlmClient`` is the same with a finite window and input budget and a word-count ``count_tokens``, for the query-sizing tests.
"""
from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any
from unittest.mock import AsyncMock, MagicMock

from archon_search.config import SearchConfig
from archon_search.hyde import HyDEGenerator
from archon_search.llm_client.protocol import (
    UNBOUNDED,
    ContextWindow,
    ContextWindowSource,
    ContextWindowTokens,
    InputBudget,
    LlmCallRequest,
    LlmCallResult,
    ThinkSetting,
)


class ScriptedLlmClient:
    """An ``LlmClient`` that answers ``reply`` (or raises ``failure``) and records each ``LlmCallRequest``."""

    def __init__(self, reply: str = "a hypothetical passage", failure: Exception | None = None) -> None:
        self.reply = reply
        self.failure = failure
        self.requests: list[LlmCallRequest] = []

    async def call(self, request: LlmCallRequest) -> LlmCallResult:
        self.requests.append(request)
        if self.failure is not None:
            raise self.failure
        return LlmCallResult(text=self.reply)

    def context_window(self) -> ContextWindow:
        return UNBOUNDED

    async def input_budget(self, template_text: str, think: ThinkSetting = ThinkSetting.DEFAULT) -> InputBudget:
        return InputBudget(UNBOUNDED)


class BudgetedLlmClient(ScriptedLlmClient):
    """A scripted client with a finite window and a fixed input budget; it counts ``tokens_per_word`` (1) tokens per whitespace-separated word.

    Records every template ``input_budget`` was asked about and every text ``count_tokens`` counted. ``count_failure``
    makes the count (not the call) raise, like an unreachable tokenize endpoint.
    """

    def __init__(
        self,
        *,
        window_tokens: int,
        budget: int,
        reply: str = "a hypothetical passage",
        count_failure: Exception | None = None,
        tokens_per_word: int = 1,
    ) -> None:
        super().__init__(reply=reply)
        self.window_tokens = window_tokens
        self.budget = budget
        self.count_failure = count_failure
        self.tokens_per_word = tokens_per_word
        self.templates: list[str] = []
        self.counted: list[str] = []

    def context_window(self) -> ContextWindow:
        return ContextWindowTokens(size=self.window_tokens, source=ContextWindowSource.CONFIGURED)

    async def input_budget(self, template_text: str, think: ThinkSetting = ThinkSetting.DEFAULT) -> InputBudget:
        self.templates.append(template_text)
        return InputBudget(self.budget)

    async def count_tokens(self, text: str) -> int:
        self.counted.append(text)
        if self.count_failure is not None:
            raise self.count_failure
        return self.tokens_per_word * len(text.split())

    def token_ceiling(self, text: str) -> int:
        return self.tokens_per_word * len(text.encode("utf-8"))  # a word is at least one byte


def stub_hyde_generator(**generate: Any) -> MagicMock:
    """A ``HyDEGenerator`` stand-in whose ``generate`` is ``AsyncMock(**generate)``.

    ``return_value=`` a vector applies HyDE, ``return_value=None`` is a failed expansion,
    ``side_effect=`` raises or delays.
    """
    return MagicMock(spec=HyDEGenerator, generate=AsyncMock(**generate))


def hyde_enabled_config() -> SearchConfig:
    """A default ``SearchConfig`` with ``[hyde] enabled = true`` (for MCP ``create_app``)."""
    config = SearchConfig()
    config.hyde.enabled = True
    return config


@contextmanager
def rest_hyde_generating(app: Any, **generate: Any) -> Iterator[AsyncMock]:
    """Enable ``[hyde]`` on a REST *app* and serve HyDE from :func:`stub_hyde_generator`.

    Yields the stub's ``generate`` mock; restores the app's ``[hyde]`` state on exit.
    """
    hyde_config = app.state.config.hyde
    previous = (hyde_config.enabled, getattr(app.state, "hyde_generator", None))
    hyde_config.enabled = True
    app.state.hyde_generator = stub_hyde_generator(**generate)
    try:
        yield app.state.hyde_generator.generate
    finally:
        hyde_config.enabled, app.state.hyde_generator = previous
