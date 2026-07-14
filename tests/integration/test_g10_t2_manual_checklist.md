# Manual Test: G10 T-2 — Live Ollama + Live OpenAI Provider Verification

S1 (Anthropic path unchanged) is covered by automated integration tests (see `tests/test_anthropic_provider.py`). This checklist covers live-infrastructure scenarios requiring real LLM providers.

## Prerequisites

Install the required extras (only those needed per scenario):
- Ollama scenarios (S2, S3, Wizard): `uv sync --dev --extra ollama`
- OpenAI scenarios (S4, S5): `uv sync --dev --extra openai-provider`

Runtime requirements per scenario:
- **S2, S3, Wizard:** Ollama running locally (`ollama serve`), `llama3.2` model pulled (`ollama pull llama3.2`)
- **S4, S5:** `OPENAI_API_KEY` environment variable set with a valid OpenAI key
- **S6:** Both Ollama running (with `llama3.2`) and `ANTHROPIC_API_KEY` set

All scenarios require at least one collection ingested before running. Ingest at least one document:
- [ ] `uv run archon-search ingest --path /path/to/some/docs` — ingest at least one document

**Config:** Before each scenario, replace the **entire** contents of `~/.archon-search/archon-search.toml` with the block shown in that scenario to avoid stale settings from prior runs. Back up your existing config if needed.

**Bearer token:** The API token is stored in `~/.archon-search/.search.env` in the format `ARCHON_SEARCH_API_KEY=<64-hex-chars>`. Extract it with: `grep ARCHON_SEARCH_API_KEY ~/.archon-search/.search.env | cut -d= -f2`. Use that value as the Bearer token in all `Authorization: Bearer <token>` headers below. If the file is absent, start the server once (`uv run archon-search serve`) — it auto-generates the key on first run.

---

## S2 — Live Ollama HyDE

**Config (`~/.archon-search/archon-search.toml`):**
```toml
[hyde]
enabled = true
provider = "ollama"
model = "llama3.2"
ollama_base_url = "http://localhost:11434"
```

**Steps:**
- [ ] Stop any running server (Ctrl-C) from prior scenario
- [ ] Start server: `uv run archon-search serve`
- [ ] POST `/search` with `{"query": "test", "hyde": true}` using a valid Bearer token
- [ ] Verify: `hyde_applied=true` in response
- [ ] Verify: non-empty `results` in response
- [ ] Verify Ollama provider via `GET /status`: `curl -s -H "Authorization: Bearer <token>" http://localhost:8765/status | jq '.hyde.provider, .hyde.key_available'` — expected: `"ollama"` and `true`
- [ ] Stop server (Ctrl-C)

---

## S3 — Live Ollama RAG Fusion

**Config (`~/.archon-search/archon-search.toml`):**
```toml
[rag_fusion]
enabled = true
provider = "ollama"
model = "llama3.2"
ollama_base_url = "http://localhost:11434"
```

**Steps:**
- [ ] Stop any running server (Ctrl-C) from prior scenario
- [ ] Start server: `uv run archon-search serve`
- [ ] POST `/search` with `{"query": "test", "rag_fusion": true}` using a valid Bearer token
- [ ] Verify: `rag_fusion_applied=true` in response
- [ ] Verify: `rag_fusion_queries_used >= 1` in response
- [ ] Verify: non-empty `results` in response
- [ ] Verify Ollama provider via `GET /status`: `curl -s -H "Authorization: Bearer <token>" http://localhost:8765/status | jq '.rag_fusion.provider, .rag_fusion.key_available'` — expected: `"ollama"` and `true`
- [ ] Stop server (Ctrl-C)

---

## S4 — Live OpenAI HyDE

**Config (`~/.archon-search/archon-search.toml`):**
```toml
[hyde]
enabled = true
provider = "openai"
model = "gpt-4o-mini"
```

**Steps:**
- [ ] Stop any running server (Ctrl-C) from prior scenario
- [ ] Ensure `OPENAI_API_KEY` is set in environment
- [ ] Start server: `uv run archon-search serve`
- [ ] POST `/search` with `{"query": "test", "hyde": true}` using a valid Bearer token
- [ ] Verify: `hyde_applied=true` in response
- [ ] Verify: non-empty `results` in response
- [ ] Verify OpenAI provider via `GET /status`: `curl -s -H "Authorization: Bearer <token>" http://localhost:8765/status | jq '.hyde.provider, .hyde.key_available'` — expected: `"openai"` and `true`
- [ ] Stop server (Ctrl-C)

---

## S5 — Live OpenAI RAG Fusion

**Config (`~/.archon-search/archon-search.toml`):**
```toml
[rag_fusion]
enabled = true
provider = "openai"
model = "gpt-4o-mini"
```

**Steps:**
- [ ] Stop any running server (Ctrl-C) from prior scenario
- [ ] Ensure `OPENAI_API_KEY` is set in environment
- [ ] Start server: `uv run archon-search serve`
- [ ] POST `/search` with `{"query": "test", "rag_fusion": true}` using a valid Bearer token
- [ ] Verify: `rag_fusion_applied=true` in response
- [ ] Verify: `rag_fusion_queries_used >= 1` in response
- [ ] Verify: non-empty `results` in response
- [ ] Verify OpenAI provider via `GET /status`: `curl -s -H "Authorization: Bearer <token>" http://localhost:8765/status | jq '.rag_fusion.provider, .rag_fusion.key_available'` — expected: `"openai"` and `true`
- [ ] Stop server (Ctrl-C)

---

## S6 — Mixed Providers Live

**Config (`~/.archon-search/archon-search.toml`):**
```toml
[hyde]
enabled = true
provider = "ollama"
model = "llama3.2"
ollama_base_url = "http://localhost:11434"

[rag_fusion]
enabled = true
provider = "anthropic"
model = "claude-haiku-4-5-20251001"
```

**Steps:**
- [ ] Stop any running server (Ctrl-C) from prior scenario
- [ ] Ensure both Ollama running and `ANTHROPIC_API_KEY` set in environment
- [ ] Start server: `uv run archon-search serve`
- [ ] POST `/search` with `{"query": "test", "hyde": true, "rag_fusion": true}` — verify: `hyde_applied=true` and `rag_fusion_applied=true` in response (both providers active simultaneously, no interference)
- [ ] Verify provider config via `GET /status`:
  ```bash
  curl -s -H "Authorization: Bearer <token>" http://localhost:8765/status \
    | jq '{hyde_provider: .hyde.provider, rag_fusion_provider: .rag_fusion.provider}'
  ```
  Expected output: `{"hyde_provider": "ollama", "rag_fusion_provider": "anthropic"}`
- [ ] Stop server (Ctrl-C)

---

## Wizard — Ollama Path

**Steps:**
- [ ] Stop any running server (Ctrl-C) from prior scenario
- [ ] Run `uv run archon-search wizard`
- [ ] When asked *Enable AI query expansion (HyDE + RAG Fusion)? [y/N]*, answer `y`
- [ ] When prompted for HyDE provider, select `ollama`
- [ ] Enter model name `llama3.2` when prompted for HyDE model
- [ ] Enter base URL (accept default `http://localhost:11434` or enter custom) for HyDE
- [ ] When prompted for RAG Fusion provider, select `ollama`
- [ ] Enter model name `llama3.2` when prompted for RAG Fusion model
- [ ] Enter base URL (accept default `http://localhost:11434` or enter custom) for RAG Fusion
- [ ] Verify: wizard does not block or fail when `ANTHROPIC_API_KEY` is unset
- [ ] Verify: `~/.archon-search/archon-search.toml` contains `provider = "ollama"` and `model = "llama3.2"` under `[hyde]`
- [ ] Verify: `~/.archon-search/archon-search.toml` contains `provider = "ollama"` and `model = "llama3.2"` under `[rag_fusion]`
- [ ] Start server: `uv run archon-search serve`
- [ ] Verify: server starts without `ConfigError`
- [ ] POST `/search` with `{"query": "test", "hyde": true}` — verify: non-empty `results` in response and `hyde_applied=true`
- [ ] Stop server (Ctrl-C)

---

## Acceptance

All steps above passed without errors. Mixed-provider config (S6) runs without interference. Wizard writes correct TOML without requiring `ANTHROPIC_API_KEY`.
