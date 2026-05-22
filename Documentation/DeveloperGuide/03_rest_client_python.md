**Purpose**: Show concrete, copy-paste-correct `httpx` calls against the `archon-search` REST surface, with verified request and response shapes.
**Audience**: Python engineers integrating `archon-search` from another process.
**Status**: Draft
**Last reviewed**: 2026-05-20 / **Next review**: 2027-05-20

# REST Client — Python (`httpx`)

This page shows the most common integration flows. Every code block was checked against `archon_search/server/routes_*.py` and `archon_search/server/schemas.py` on the current `main`. The authoritative contract remains `GET /openapi.json`.

## Principles

1. **One client, one base URL, one bearer.** Build a single `httpx.Client` (or `AsyncClient`) and attach the auth header once.
2. **Use a `with`-context.** `httpx.Client` is a connection pool; closing it cleanly releases sockets.
3. **`raise_for_status()` is your friend.** REST errors come back with the envelope `{"detail": "..."}` and a non-2xx status; let the client surface that as an exception.
4. **`/search` fails loudly on pipeline errors.** Pipeline stage exceptions (embedder, store, reranker) return HTTP 500; a hung pipeline returns HTTP 504. HTTP 200 with `results: []` means the pipeline ran successfully but found no matching documents — it is not an error signal.

## Setting up a client

```python
from pathlib import Path
import httpx

def _load_key() -> str:
    p = Path.home() / ".archon-search" / ".search.env"
    for line in p.read_text().splitlines():
        if line.startswith("ARCHON_SEARCH_API_KEY="):
            return line.split("=", 1)[1].strip()
    raise RuntimeError("archon-search key not found")

BASE_URL = "http://127.0.0.1:8765"
KEY = _load_key()

client = httpx.Client(
    base_url=BASE_URL,
    headers={"Authorization": f"Bearer {KEY}"},
    timeout=httpx.Timeout(60.0, connect=2.0),
)
```

For an async caller, use `httpx.AsyncClient` with the same args and `await client.get(...)`.

## Health check (unauthenticated)

The `/health` endpoint is exempt from auth and returns `HealthResponse` from `schemas.py`:

```python
r = httpx.get(f"{BASE_URL}/health")
r.raise_for_status()
# Example payload (CalVer YY.M.<rev-count>, illustrative): #Unverified
# {"status": "running", "version": "26.5.123"}
print(r.json())
```

Use this in startup probes; do not poll it as a liveness ping for your own app — it does not exercise auth.

## Listing collections

`GET /collections/` returns `list[CollectionSummary]`. Each entry:

```json
{
  "name": "code-archon-search",
  "path": "/Users/me/repos/archon-search",
  "description": "",
  "doc_count": 0,
  "chunk_count": 0,
  "namespace": "default",
  "status": "done"
}
```

Note: `doc_count` and `chunk_count` are placeholders on the list endpoint (always `0`). The per-collection detail (`GET /collections/{name}`) returns the real `doc_count`.

```python
cols = client.get("/collections/").raise_for_status().json()
for c in cols:
    print(c["name"], c["status"])
```

`GET /collections/{name}` returns `CollectionDetail`:

```python
detail = client.get("/collections/code-archon-search").raise_for_status().json()
# Adds: embedding_model, centroid_present, last_indexed,
#       acl_protected_count, acl_open_count, real doc_count.
```

## Adding a collection (async ingest)

`POST /collections/` accepts `{"path": "<dir>"}` and returns `202` plus a `JobResponse`:

```python
r = client.post("/collections/", json={"path": "/Users/me/docs/specs"})
r.raise_for_status()  # 202 Accepted
job = r.json()
# {"job_id": "...", "status": "PENDING", "created_at": "...",
#  "updated_at": "...", "result": null, "error": null,
#  "namespace": "default"}
```

The path must not already be registered (returns `409 {"detail": "collection already registered"}`) and its derived name must be globally unique across namespaces (`409 {"detail": "collection name already registered"}`).

You can attach an `X-Ingested-By` header so the job log shows the calling system:

```python
client.post(
    "/collections/",
    json={"path": "/Users/me/docs/specs"},
    headers={"X-Ingested-By": "my-tool/1.0"},
)
```

## Polling a job

`GET /jobs/{job_id}` returns the same `JobResponse` shape. Statuses are uppercase: `PENDING`, `RUNNING`, `DONE`, `FAILED`, `CANCELLING`, `CANCELLED` (`archon_search/types.py::JobStatus`; serialized via `job_to_dict` in `archon_search/jobs/model.py`).

```python
import time

def wait_for_job(job_id: str, timeout: float = 300.0) -> dict:
    deadline = time.monotonic() + timeout
    while True:
        j = client.get(f"/jobs/{job_id}").raise_for_status().json()
        if j["status"] in {"DONE", "FAILED", "CANCELLED"}:
            return j
        if time.monotonic() > deadline:
            raise TimeoutError(f"job {job_id} did not finish in {timeout}s")
        time.sleep(1.0)
```

`DELETE /jobs/{job_id}` is idempotent: terminal jobs return `200` with the unchanged record; active jobs transition to `cancelling` and return `202`.

## Standalone ingest (existing collection)

`POST /ingest` enqueues an ingest into an already-registered collection. The body schema (`IngestRequest`) supports either a path on disk or an inline document list:

```python
# Ingest from a directory
r = client.post("/ingest", json={
    "collection": "code-archon-search",
    "path": "/Users/me/repos/archon-search/docs",
})
r.raise_for_status()  # 202
job = r.json()

# Or ingest inline documents
r = client.post("/ingest", json={
    "collection": "scratch",
    "documents": [
        {"doc_id": "note-1", "text": "hello world", "source_path": "memory://note-1"},
    ],
})
```

`ingested_by` defaults to `"archon-search-cli"`; supplying it via the `X-Ingested-By` header is the recommended pattern (the JSON body field is overwritten by the header — see `routes_jobs.py:98`).

## Searching

`POST /search` runs hybrid search inside one collection:

```python
r = client.post("/search", json={
    "collection": "code-archon-search",
    "query": "how is the API key bootstrapped",
})
r.raise_for_status()
payload = r.json()
# {"results": [
#     {"doc_id": "...", "chunk_id": "...", "text": "...",
#      "score": 0.78, "source_path": "/abs/path"}, ...
#  ],
#  "acl_filtered": false}
for hit in payload["results"]:
    print(f"{hit['score']:.3f}  {hit['source_path']}")
```

**Gotchas verified against `routes_search.py`:**

- `top_k` in the request body is accepted (Pydantic validates `1 ≤ top_k ≤ 100`) but **ignored** — the pipeline uses `[search] top_k_return`. See `BREAKING.md`.
- Empty `query` or empty `collection` → `422` from Pydantic validators.
- Unknown / cross-namespace collection → `404 {"detail": "collection not found"}`.
- Meta lookup failure → `503 {"detail": "service unavailable"}`.
- Pipeline failure mid-search → HTTP 500 with `{"detail": "Internal Server Error"}`. Pipeline timeout → HTTP 504 with `{"detail": "Search timed out"}`. HTTP 200 with `results=[]` means no matches found on a healthy pipeline.

There is no REST `/search/with_context` endpoint. That capability is exposed only as the MCP tool `search_with_context` (`mcp.py:77`). If you need it from REST today, ingest the document and issue follow-up `/search` calls; or use the MCP transport.

## Routing across collections

`POST /route` picks which collections to query for a given prompt via centroid pre-ranking:

```python
r = client.post("/route", json={"query": "summarize this auth flow", "slots": 3})
r.raise_for_status()
# {"pre_context": "...", "pinned_names": [...], "routable_names": [...],
#  "decomposer_invoked": false}
```

`slots` is optional; when omitted, `[routing] shortlist_size` is used. Errors (exact `detail` strings from `routes_route.py`):

- `400 {"detail": "query must not be empty"}` — empty/whitespace `query`.
- `400 {"detail": "slots must be >= 1"}` — `slots` provided and `< 1`.
- `504 {"detail": "routing timed out"}` — after a 30 s routing timeout.

## Removing a collection

```python
r = client.delete("/collections/code-archon-search")
r.raise_for_status()
# {"name": "code-archon-search", "deleted": true}
```

`404` if the name is unknown or belongs to another namespace; `409` if the path is only present in `pinned_collections` (must be removed from config first).

## Related documents

- [`../Architecture/600_api_reference_or_public_interface.md`](../Architecture/600_api_reference_or_public_interface.md) — full reference.
- [`06_error_handling.md`](./06_error_handling.md) — status codes and retry guidance.
- [`../../BREAKING.md`](../../BREAKING.md) — `top_k` deprecation and other contract changes.
- [`../Architecture/140_error_handling_strategy.md`](../Architecture/140_error_handling_strategy.md) — full status code matrix.
