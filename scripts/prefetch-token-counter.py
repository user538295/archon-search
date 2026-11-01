"""Fetch, at image build time, the gpt2 tokenizer the ``ollama`` token counter would download on first use.

Run by the Dockerfile with ``ARCHON_SEARCH_DATA_DIR`` already pointing at ``/data``: the pinned ``tokenizer.json``
(1.3 MB) lands in ``<data dir>/models/token-counter`` through the loader the service uses
(``archon_search.llm_client.token_counter.load_gpt2_tokenizer``). A fresh data volume is seeded from the image, so
there the first sized prompt needs no network; a volume that already holds data and a host bind mount are not
seeded, and the tokenizer downloads on first use (or at the wizard's pre-warm). Counting a sample text fails the
build if the snapshot cannot be loaded.

The release workflow's ``test`` job and ``scripts/test-unlocked-deps.sh suite`` run it too, ahead of the default suite,
whose real-gpt2 ceiling tests (``tests/test_llm_client_token_ceiling.py``) fail on CI when the tokenizer is missing.
"""
from __future__ import annotations

from archon_search.llm_client.token_counter import TokenCounter

_SAMPLE_TEXT = "Prefetch sample text"

if __name__ == "__main__":
    print(f"prefetch: counted {TokenCounter().count(_SAMPLE_TEXT)} gpt2 tokens")
