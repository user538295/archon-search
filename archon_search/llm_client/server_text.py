"""Server error text made safe for a failure reason (contract C1: a reason never carries the prompt)."""

from __future__ import annotations

import json
from typing import Final

#: Longest server error text a failure reason carries.
MAX_SERVER_TEXT_CHARS: Final = 200


def bounded_server_text(text: str, prompt: str) -> str:
    """Server error text as one short line with the prompt (raw or JSON-escaped) masked; best effort, the text is untrusted."""
    scrubbed = text
    if prompt:
        for echo in (prompt, json.dumps(prompt)[1:-1]):
            scrubbed = scrubbed.replace(echo, "[prompt]")
    one_line = " ".join(scrubbed.split())
    return one_line if len(one_line) <= MAX_SERVER_TEXT_CHARS else f"{one_line[:MAX_SERVER_TEXT_CHARS]}..."
