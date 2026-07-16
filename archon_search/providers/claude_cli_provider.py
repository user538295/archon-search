"""ClaudeCLIQueryExpansionProvider — Interface Adapter for the Claude Code CLI.

Implements the QueryExpansionProvider protocol by shelling out to the Claude
Code CLI: ``claude -p "<prompt>" --output-format text [--model <model>]``.

No API key: availability is whether ``claude`` is on PATH (``shutil.which``),
checked once at ``__init__``. No rate limiting — this mirrors Ollama's
local/free path (skipped at the generator's call site).

Privacy: raw query text is never logged; only ``_query_fingerprint()`` tokens
appear in log messages. The prompt IS passed to the ``claude`` subprocess as an
argv element (list form, no shell → no injection concern).

Errors are silenced internally — returns ``None`` / ``[]`` on any failure.
Never raises to callers (C1 adapter contract).
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import re
import shutil

from archon_search._privacy import _query_fingerprint
from archon_search.constants import DEFAULT_FAST_MODEL

_CONTROL_CHARS_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]")
# ANSI escape sequences — belt-and-suspenders; --output-format text is plain,
# but older Claude Code versions may still emit status/color codes.
_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")

_logger = logging.getLogger(__name__)

_CLAUDE_BIN = "claude"

_HYDE_PROMPT_TEMPLATE = """\
Write a short passage that would directly answer the following question.
Output only the passage — no preamble, no explanation.

---
{query}
---"""

_RAG_FUSION_PROMPT_TEMPLATE = """\
You are a search query decomposer. Given a user query, generate {num_queries} alternative \
search queries that capture different facets of the same information need.
Rules: each query on its own line, plain text, under 500 characters.
Output exactly {num_queries} queries, one per line.

---
{query}
---"""


class ClaudeCLIQueryExpansionProvider:
    """Claude Code CLI implementation of the QueryExpansionProvider protocol.

    Resolves the ``claude`` binary via ``shutil.which`` at construction; when
    absent, both methods return ``None`` / ``[]`` (one-time warning). The
    ``--model`` flag is omitted when the configured model is blank or equals
    ``DEFAULT_FAST_MODEL`` (the config default sentinel — since a blank
    ``[hyde].model`` is coerced to ``DEFAULT_FAST_MODEL`` at load, treating that
    value as "unset" is what lets Claude Code use its own configured default;
    pick a specific alias such as ``sonnet`` to force a model).

    Assumptions (best-effort adapter — any failure degrades to plain search):
    - Requires a Claude Code version that supports ``-p``/``--output-format text``/
      ``--model`` (verified against the shipped CLI; ``-p`` alone is the fallback).
    - ``max_tokens`` is accepted for protocol conformance but INERT — the CLI has
      no token-cap flag, so output length is bounded only by the prompt and
      ``timeout_seconds``. Expect ``--output-format text`` output to be small.
    """

    def __init__(self, model: str) -> None:
        self._model = model
        self._claude_path: str | None = shutil.which(_CLAUDE_BIN)
        self._claude_available: bool = self._claude_path is not None
        self._warned_not_found: bool = False

    def _model_args(self) -> list[str]:
        """Return ``["--model", <model>]`` unless the model is blank/default."""
        if self._model and self._model != DEFAULT_FAST_MODEL:
            return ["--model", self._model]
        return []

    def is_key_available(self) -> bool:
        """The Claude CLI uses Claude Code's own login — no API key."""
        return True

    async def _run(self, prompt: str, query: str, timeout_seconds: float, label: str) -> str | None:
        """Run ``claude -p`` and return cleaned stdout, or ``None`` on any failure.

        Never raises: timeout kills the subprocess; non-zero exit and spawn
        errors log a fingerprinted warning and return ``None``.
        """
        fp = _query_fingerprint(query)
        argv = [
            self._claude_path,
            "-p",
            prompt,
            "--output-format",
            "text",
            *self._model_args(),
        ]
        proc: asyncio.subprocess.Process | None = None
        try:
            proc = await asyncio.create_subprocess_exec(
                *argv,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, _stderr = await asyncio.wait_for(
                proc.communicate(), timeout=timeout_seconds
            )
        except asyncio.TimeoutError:
            if proc is not None:
                proc.kill()
                with contextlib.suppress(Exception):
                    await proc.wait()
            _logger.warning(
                "ClaudeCLIQueryExpansionProvider: %s call timed out after %.1fs (fp=%s)",
                label,
                timeout_seconds,
                fp,
            )
            return None
        except Exception:  # noqa: BLE001
            _logger.warning(
                "ClaudeCLIQueryExpansionProvider: error running claude CLI for %s (fp=%s)",
                label,
                fp,
            )
            return None

        if proc.returncode != 0:
            _logger.warning(
                "ClaudeCLIQueryExpansionProvider: claude CLI exited %s for %s (fp=%s)",
                proc.returncode,
                label,
                fp,
            )
            return None

        return _ANSI_RE.sub("", stdout.decode("utf-8", errors="replace"))

    async def generate_hypothetical_doc(
        self,
        query: str,
        *,
        max_tokens: int = 200,  # noqa: ARG002 — ponytail: no CLI token cap; prompt bounds output
        timeout_seconds: float = 10.0,
    ) -> str | None:
        """Generate a short hypothetical passage that would answer the query.

        Returns hypothesis text as a plain ``str``, or ``None`` on any failure.
        Never raises.
        """
        if not self._claude_available:
            if not self._warned_not_found:
                _logger.warning(
                    "ClaudeCLIQueryExpansionProvider: 'claude' not found in PATH; "
                    "HyDE provider will not run (fp=%s)",
                    _query_fingerprint(query),
                )
                self._warned_not_found = True
            return None

        prompt = _HYDE_PROMPT_TEMPLATE.format(query=query)
        text = await self._run(prompt, query, timeout_seconds, "HyDE")

        # Strip non-text output (brief: "strip non-text output before returning").
        # _run already removes ANSI CSI sequences; this also removes bare control
        # chars (e.g. a lone ESC or NUL) before the passage reaches the embedder.
        if text is not None:
            text = _CONTROL_CHARS_RE.sub("", text)

        if not text or not text.strip():
            _logger.warning(
                "ClaudeCLIQueryExpansionProvider: empty HyDE response (fp=%s)",
                _query_fingerprint(query),
            )
            return None

        return text.strip()

    async def decompose_query(
        self,
        query: str,
        *,
        num_queries: int = 3,
        max_tokens: int = 450,  # noqa: ARG002 — ponytail: no CLI token cap; prompt bounds output
        timeout_seconds: float = 10.0,
    ) -> list[str]:
        """Decompose the query into semantic variant strings.

        Returns a list of variant strings, or ``[]`` on any failure.
        Never raises.
        """
        if not self._claude_available:
            if not self._warned_not_found:
                _logger.warning(
                    "ClaudeCLIQueryExpansionProvider: 'claude' not found in PATH; "
                    "RAG Fusion provider will not run (fp=%s)",
                    _query_fingerprint(query),
                )
                self._warned_not_found = True
            return []

        prompt = _RAG_FUSION_PROMPT_TEMPLATE.format(num_queries=num_queries, query=query)
        raw_text = await self._run(prompt, query, timeout_seconds, "RAG Fusion")

        if not raw_text:
            _logger.warning(
                "ClaudeCLIQueryExpansionProvider: empty RAG Fusion response (fp=%s)",
                _query_fingerprint(query),
            )
            return []

        # Parse: one variant per line, validate each, truncate to num_queries
        variants: list[str] = []
        for line in raw_text.split("\n"):
            stripped = line.strip()
            if not stripped:
                continue
            if len(stripped) > 500:
                continue
            if _CONTROL_CHARS_RE.search(stripped):
                continue
            variants.append(stripped)
            if len(variants) >= num_queries:
                break

        if len(variants) < num_queries:
            _logger.warning(
                "ClaudeCLIQueryExpansionProvider: requested %d variants but got %d valid ones (fp=%s)",
                num_queries,
                len(variants),
                _query_fingerprint(query),
            )

        return variants
