"""Description generator for RAG collections — uses Haiku to generate 2–3 sentence descriptions."""
from __future__ import annotations

import asyncio
import logging
import os
import random

from claude_agent_sdk import ClaudeAgentOptions, ClaudeSDKClient, ResultMessage

from archon_search.constants import DEFAULT_FAST_MODEL

logger = logging.getLogger("archon")

# Serializes concurrent SDK connect() calls during the os.environ mutation window.
# Lazy-initialized per event loop (avoids "bound to a different event loop" errors).
_ENV_LOCK: asyncio.Lock | None = None


def _get_env_lock() -> asyncio.Lock:
    global _ENV_LOCK  # noqa: PLW0603
    if _ENV_LOCK is None:
        _ENV_LOCK = asyncio.Lock()
    return _ENV_LOCK

_TIMEOUT_SECONDS = 30
_MAX_SAMPLE_CHUNKS = 20

_DESCRIPTION_PROMPT = """\
You are generating a concise description for a document collection named "{name}".

Below are sample excerpts from documents in this collection:

{samples}

Write 2-3 sentences that summarize what this collection contains and its purpose. \
Be specific about the topic and type of content. \
Output only the description — no preamble, no explanation.
"""


def _should_regenerate(
    doc_count: int,
    chunk_count: int,
    described_at_doc_count: int | None,
) -> bool:
    """Return True if description should be (re)generated for this ingest batch.

    Rules:
    - chunk_count == 0  → never regenerate (no content to describe)
    - described_at_doc_count is None or 0  → always regenerate
    - abs change >= 20%  → regenerate
    """
    if chunk_count == 0:
        return False
    if described_at_doc_count is None or described_at_doc_count == 0:
        return True
    return abs(doc_count - described_at_doc_count) / described_at_doc_count >= 0.20


async def generate_description(chunks: list[str], collection_name: str) -> str:
    """Sample up to 20 chunks, call Haiku via ClaudeSDKClient, return 2-3 sentence description.

    Session lifecycle: creates a new ClaudeSDKClient with DEFAULT_FAST_MODEL (Haiku),
    permission_mode="bypassPermissions".  Sends a single query, reads response, disconnects.
    A 30-second timeout wraps the entire connect/query/receive/disconnect lifecycle.

    Falls back to returning collection_name (the collection path) on any failure:
    - ANTHROPIC_API_KEY not set
    - SDK exception or timeout
    - No chunks provided
    """
    if not chunks:
        return collection_name

    if not os.environ.get("ANTHROPIC_API_KEY"):
        logger.debug("ANTHROPIC_API_KEY not set; skipping description generation for %r", collection_name)
        return collection_name

    sample = random.sample(chunks, min(_MAX_SAMPLE_CHUNKS, len(chunks)))
    prompt = _DESCRIPTION_PROMPT.format(
        name=collection_name,
        samples="\n\n---\n\n".join(sample),
    )

    try:
        result = await asyncio.wait_for(_call_haiku(prompt), timeout=_TIMEOUT_SECONDS)
        return result if result else collection_name
    except asyncio.TimeoutError:
        logger.debug("Description generation timed out for collection %r", collection_name)
        return collection_name
    except Exception:
        logger.debug(
            "Description generation failed for collection %r",
            collection_name,
            exc_info=True,
        )
        return collection_name


async def _call_haiku(prompt: str) -> str | None:
    """Create a fresh Haiku session, send the prompt, return the response text."""
    client = ClaudeSDKClient(
        options=ClaudeAgentOptions(
            permission_mode="bypassPermissions",
            model=DEFAULT_FAST_MODEL,
            max_turns=1,
            max_buffer_size=10 * 1024 * 1024,
        )
    )
    # Serialise CLAUDECODE env mutation with the same lock used by ClaudeSession.start()
    # to prevent races between concurrent SDK connect() calls.
    async with _get_env_lock():
        claudecode = os.environ.pop("CLAUDECODE", None)
        try:
            await client.connect()
        finally:
            if claudecode is not None:
                os.environ["CLAUDECODE"] = claudecode
    try:
        await client.query(prompt)
        async for msg in client.receive_response():
            if isinstance(msg, ResultMessage):
                text = (msg.result or "").strip()
                return text or None
        return None
    finally:
        try:
            await client.disconnect()
        except BaseException:
            pass
