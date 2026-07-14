# Manual Test: G10 T-2 — Live Ollama + Live OpenAI Provider Verification

## Prerequisites

Install the required extras (only those needed per scenario):
- Ollama scenarios (S2, S3, Wizard): `uv sync --dev --extra ollama`
- OpenAI scenarios (S4, S5): `uv sync --dev --extra openai-provider`

Runtime requirements per scenario:
- **S2, S3, Wizard:** Ollama running locally (`ollama serve`), `llama3.2` model pulled (`ollama pull llama3.2`)
- **S4, S5:** `OPENAI_API_KEY` environment variable set with a valid OpenAI key
- **S6:** Both Ollama running (with `llama3.2`) and `ANTHROPIC_API_KEY` set

All scenarios require at least one collection ingested (e.g. `uv run archon-search ingest /path/to/docs`).

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
- [ ] Start server: `uv run archon-search serve`
- [ ] POST `/search` with `{"query": "test", "hyde": true}` using a valid Bearer token
- [ ] Verify: `hyde_applied=true` in response
- [ ] Verify: non-empty `results` in response

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
- [ ] Start server: `uv run archon-search serve`
- [ ] POST `/search` with `{"query": "test", "rag_fusion": true}` using a valid Bearer token
- [ ] Verify: `rag_fusion_applied=true` in response
- [ ] Verify: `rag_fusion_queries_used > 0` in response
- [ ] Verify: non-empty `results` in response

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
- [ ] Ensure `OPENAI_API_KEY` is set in environment
- [ ] Start server: `uv run archon-search serve`
- [ ] POST `/search` with `{"query": "test", "hyde": true}` using a valid Bearer token
- [ ] Verify: `hyde_applied=true` in response
- [ ] Verify: non-empty `results` in response

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
- [ ] Ensure `OPENAI_API_KEY` is set in environment
- [ ] Start server: `uv run archon-search serve`
- [ ] POST `/search` with `{"query": "test", "rag_fusion": true}` using a valid Bearer token
- [ ] Verify: `rag_fusion_applied=true` in response
- [ ] Verify: `rag_fusion_queries_used > 0` in response
- [ ] Verify: non-empty `results` in response

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
- [ ] Ensure both Ollama running and `ANTHROPIC_API_KEY` set in environment
- [ ] Start server: `uv run archon-search serve`
- [ ] POST `/search` with `{"query": "test", "hyde": true, "rag_fusion": true}` using a valid Bearer token
- [ ] Verify: `hyde_applied=true` in response
- [ ] Verify: `rag_fusion_applied=true` in response
- [ ] GET `/status` and verify: `data["hyde"]["provider"] == "ollama"` and `data["rag_fusion"]["provider"] == "anthropic"`

---

## Wizard — Ollama Path

**Steps:**
- [ ] Run `uv run archon-search wizard`
- [ ] When prompted for HyDE provider, select `ollama`
- [ ] Enter model name `llama3.2` when prompted
- [ ] Enter base URL (accept default `http://localhost:11434` or enter custom)
- [ ] Verify: wizard does not require `ANTHROPIC_API_KEY` during HyDE/RAG Fusion prompt
- [ ] Verify: `~/.archon-search/archon-search.toml` contains `provider = "ollama"` and `model = "llama3.2"` under `[hyde]`
- [ ] Start server: `uv run archon-search serve`
- [ ] Verify: server starts without `ConfigError`

---

## Acceptance

All steps above passed without errors. Mixed-provider config (S6) runs without interference. Wizard writes correct TOML without requiring `ANTHROPIC_API_KEY`.
