#!/usr/bin/env python3
"""
Synthesize quality GitHub release notes from raw commits using Claude.

Usage: git log ... | python3 synthesize_release_notes.py <tag>
Output: release notes body (no ## header — that comes from git-cliff)

Strategy:
1. ANTHROPIC_API_KEY set → direct API call (fast, no CLI overhead)
2. API call fails or key absent → `claude -p` CLI fallback (uses claude.ai OAuth)
3. Both fail → exit 1 (release.sh falls back to git-cliff notes)
"""

import json
import os
import subprocess
import sys
import urllib.error
import urllib.request

TAG = sys.argv[1]
COMMITS = sys.stdin.read().strip()

PROMPT = f"""\
Write GitHub release notes for archon-search version {TAG}.

archon-search is a standalone hybrid retrieval + routing server: LanceDB vector store, \
fastembed dense embeddings, cross-encoder reranker, multi-collection router, \
FastAPI HTTP control plane, MCP endpoint.

Style rules — follow exactly:
- First line is bold and summarizes the release theme (e.g. "**Search filters + /explain endpoint + SQL injection defense**")
- If there are 3+ distinct feature areas, use bold section labels on their own lines (not ## Markdown headers)
- Bullet points use backtick formatting for: class names, method names, config keys, HTTP routes, CLI flags, env vars
- Each bullet explains both WHAT changed and WHY/what it means (not just the commit subject verbatim)
- Multiple fine-grained task commits for one feature collapse into a single bullet
- Never include commit types (feat, fix, docs, etc.) in the output — they are internal metadata
- Never include task IDs like (A1-2.3), (C0-3.5), (FEAT-046) in the output — readers don't know them
- No filler phrases like "this release includes", "we are happy to announce", "improvements to"
- Do NOT include a version header line — just the body

Raw commits (some are fine-grained task steps — synthesize them into coherent user-facing descriptions):
{COMMITS}

Write only the release notes body. Nothing else."""


def _via_api(api_key: str) -> str:
    payload = json.dumps({
        "model": "claude-haiku-4-5-20251001",
        "max_tokens": 2048,
        "messages": [{"role": "user", "content": PROMPT}],
    })
    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages",
        data=payload.encode(),
        headers={
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
        method="POST",
    )
    with urllib.request.urlopen(req) as resp:
        return json.loads(resp.read())["content"][0]["text"]


def _via_cli() -> str:
    env = {k: v for k, v in os.environ.items() if k != "ANTHROPIC_API_KEY"}
    result = subprocess.run(
        ["claude", "-p", PROMPT],
        capture_output=True,
        text=True,
        env=env,
        check=True,
    )
    return result.stdout


api_key = os.environ.get("ANTHROPIC_API_KEY", "")

if api_key:
    try:
        print(_via_api(api_key), end="")
        sys.exit(0)
    except urllib.error.HTTPError as e:
        print(f"API key failed ({e.code}) — trying claude CLI", file=sys.stderr)
    except Exception as e:
        print(f"API call failed ({e}) — trying claude CLI", file=sys.stderr)

try:
    print(_via_cli(), end="")
except (subprocess.CalledProcessError, FileNotFoundError) as e:
    msg = e.stderr if isinstance(e, subprocess.CalledProcessError) else str(e)
    print(f"claude CLI error: {msg}", file=sys.stderr)
    sys.exit(1)
