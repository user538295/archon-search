"""Whether the API key an LLM provider needs is set in the environment."""

from __future__ import annotations

import os


def provider_key_available(provider: str) -> bool:
    """Return True when the given provider's required API key is set at call time.

    Provider semantics (checked at call time):
    - ``"ollama"``     → always ``True`` (local inference; no key required)
    - ``"claude_cli"`` → always ``True`` (uses Claude Code's login; no key required)
    - ``"llama_cpp"``  → always ``True`` (local inference; no key required)
    - ``"openai"``     → ``OPENAI_API_KEY`` must be set
    - ``"anthropic"``  → ``ANTHROPIC_API_KEY`` must be set (default)
    """
    if provider in ("ollama", "claude_cli", "llama_cpp"):
        return True
    if provider == "openai":
        return bool(os.environ.get("OPENAI_API_KEY"))
    return bool(os.environ.get("ANTHROPIC_API_KEY"))
