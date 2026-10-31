"""LlmClient — the shared LLM client interface, Use Cases ↔ Interface Adapters boundary (contract C1).

One consumer-owned interface serves HyDE, RAG Fusion, graph enrichment and description generation.
This module is the Use Cases side of the boundary; the provider wrappers (Interface Adapters), added
as sibling modules of this package, implement it. Every consumer depends on this module and never on
a wrapper.
Each feature builds its own client, so each keeps its own provider, model, host and context setting.

Contract (C1):
- Stateless: one user message in, text out. No history, no session; two calls never share state.
- Every failure is raised as :class:`LlmCallFailure` (a timeout as its subclass
  :class:`LlmCallTimeout`). A wrapper never swallows one and never returns a fallback value; the
  caller decides its fallback and logs the failure once per process (``warn_once``).
- The failure text never contains the prompt, a query, a hypothetical document or an excerpt.
- Input is sized AHEAD of the call to :meth:`LlmClient.context_window`; there is no truncation
  detection after the call.

Pattern mirrors ``graph_enrichment_protocol.py``: a ``runtime_checkable`` :class:`~typing.Protocol` owned by
the consumer. This module is stdlib-only.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass
from typing import Final, Literal, Protocol, runtime_checkable

#: Context window / input budget of a client that enforces no limit (cloud providers).
UNBOUNDED: Final = "unbounded"
Unbounded = Literal["unbounded"]


class ThinkSetting(enum.Enum):
    """Thinking control of one call.

    ``DEFAULT`` sends no thinking control at all. Only description generation exposes this setting;
    providers with no verified switch (``anthropic``, ``openai``, ``claude_cli``) ignore a non-default
    value. This is the call-level value, not the config file's: the ``[description].think`` parser
    maps ``default``, ``true`` and ``false`` onto it explicitly.
    """

    DEFAULT = "default"
    ON = "on"
    OFF = "off"


@dataclass(frozen=True, slots=True)
class LlmCallRequest:
    """One stateless call: exactly one user message, no history, no session."""

    prompt: str
    #: Generous for every model so that ``ThinkSetting.DEFAULT`` works on reasoning models; callers pass
    #: ``constants.ANSWER_RESERVE_TOKENS`` so the answer space the budget reserves is the ceiling they send.
    max_output_tokens: int
    #: Per attempt.
    timeout_seconds: float
    think: ThinkSetting = ThinkSetting.DEFAULT


@dataclass(frozen=True, slots=True)
class LlmCallResult:
    """The model's answer."""

    text: str


class LlmCallFailure(Exception):
    """Raised on every failure of a call.

    Covers an unreachable provider, a missing key, a provider error, a prompt the server rejects as
    too long, an exhausted graph rate limit, and ``think = ON`` on a model without thinking support.
    ``reason`` never contains the prompt, a query, a hypothetical document or an excerpt.
    """

    def __init__(self, provider: str, reason: str) -> None:
        super().__init__(provider, reason)
        self.provider = provider
        self.reason = reason

    def __str__(self) -> str:
        return f"{self.provider}: {self.reason}"


class LlmCallTimeout(LlmCallFailure):
    """The provider did not answer within ``LlmCallRequest.timeout_seconds``.

    Kept apart from every other failure: the graph model probe reads it as "possibly a reasoning
    model that is too slow", where any other failure reads as a misconfiguration. A caller that does
    not care catches :class:`LlmCallFailure`.
    """


class ContextWindowSource(enum.StrEnum):
    """Where a context window came from; the wire word ``/status`` shows as ``context_window_source``."""

    CONFIGURED = "configured"  # operator-set value (``ollama`` only)
    PROBED = "probed"  # read from the server (``llama_cpp``, its properties endpoint)
    ASSUMED = "assumed"  # ``ASSUMED_CONTEXT_TOKENS`` when unset or the probe failed (startup WARNING)


@dataclass(frozen=True, slots=True)
class ContextWindowTokens:
    """A window the client enforces: ``size`` tokens, known from ``source``.

    A window smaller than answer reserve plus template is representable (it yields ``InputBudget(0)``), so the
    caller can fail naming the window; ``size`` is at least 1 where a configured value is validated."""

    size: int
    source: ContextWindowSource


#: A window of tokens, or ``UNBOUNDED`` for a cloud provider (no limit the client enforces).
ContextWindow = ContextWindowTokens | Unbounded


@dataclass(frozen=True, slots=True)
class InputBudget:
    """How many input tokens a caller may send, or ``UNBOUNDED``.

    The window minus the reserved answer space minus the prompt template, after the provider's
    counting rule (exact for ``llama_cpp``, gpt2 estimate plus ``OLLAMA_ESTIMATE_MARGIN`` for ``ollama``).

    A window too small for reserve and template gives 0, never a negative number (``llm_client.budget``).
    """

    tokens: int | Unbounded


@runtime_checkable
class LlmClient(Protocol):
    """Structural protocol of a shared LLM client; one instance per feature."""

    async def call(self, request: LlmCallRequest) -> LlmCallResult:
        """Send one stateless user message and return the answer.

        Raises:
            LlmCallTimeout: the provider did not answer within the request's timeout.
            LlmCallFailure: any other failure. Callers catch it, apply their own fallback and log a
                WARNING rate limited to once per process.
        """
        ...

    def context_window(self) -> ContextWindow:
        """The window this client sizes to; fixed when the client is built."""
        ...

    async def input_budget(self, template_text: str) -> InputBudget:
        """The budget callers size their input to.

        Args:
            template_text: the LONGEST form of the prompt template the caller will send: the
                template WITH the retry reminder, so a retry of a full sample still fits a small
                window and never needs a second sizing.
        """
        ...


@runtime_checkable
class TokenCountingClient(Protocol):
    """A client that counts tokens itself, in the unit its :meth:`LlmClient.input_budget` is measured in (``llama_cpp``,
    through the server's own tokenizer). A caller that sizes passages to the budget counts with this when the client has
    it, and with the gpt2 counter otherwise (``ollama``, whose budget is in gpt2 tokens)."""

    async def count_tokens(self, text: str) -> int:
        """The number of tokens of *text*.

        Raises:
            LlmCallFailure: the count failed (an unreachable tokenize endpoint).
        """
        ...
