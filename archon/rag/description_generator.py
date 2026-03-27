"""Description generator for RAG collections — uses Haiku to generate 2–3 sentence descriptions."""
from __future__ import annotations

import asyncio
import logging
import os
import random

from claude_agent_sdk import ClaudeAgentOptions, ClaudeSDKClient, ResultMessage

from archon.ai.claude_session import _get_env_lock
from archon.ai.constants import DEFAULT_FAST_MODEL

logger = logging.getLogger("archon")

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


async def generate_description(chunks: list[str], collection_name: str) -> str | None:
    """Sample up to 20 chunks, call Haiku via ClaudeSDKClient, return 2-3 sentence description.

    Session lifecycle: creates a new ClaudeSDKClient with DEFAULT_FAST_MODEL (Haiku),
    permission_mode="bypassPermissions".  Sends a single query, reads response, disconnects.
    A 30-second timeout wraps the entire connect/query/receive/disconnect lifecycle;
    if exceeded, returns None.  On any error, returns None without raising.
    """
    if not chunks:
        return None

    sample = random.sample(chunks, min(_MAX_SAMPLE_CHUNKS, len(chunks)))
    prompt = _DESCRIPTION_PROMPT.format(
        name=collection_name,
        samples="\n\n---\n\n".join(sample),
    )

    try:
        return await asyncio.wait_for(_call_haiku(prompt), timeout=_TIMEOUT_SECONDS)
    except asyncio.TimeoutError:
        logger.debug("Description generation timed out for collection %r", collection_name)
        return None
    except Exception:
        logger.debug(
            "Description generation failed for collection %r",
            collection_name,
            exc_info=True,
        )
        return None


async def _call_haiku(prompt: str) -> str | None:
    """Create a fresh Haiku session, send the prompt, return the response text."""
    client = ClaudeSDKClient(
        options=ClaudeAgentOptions(
            permission_mode="bypassPermissions",
            model=DEFAULT_FAST_MODEL,
            max_turns=1,
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
