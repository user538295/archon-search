**Purpose**: Show the same integration flows as the Python guide, in TypeScript against the standard `fetch` API, with types matching `archon_search/server/schemas.py`.
**Audience**: TypeScript / Node engineers integrating `archon-search` from another process or a browser-adjacent runtime.
**Status**: Draft
**Last reviewed**: 2026-05-20 / **Next review**: 2027-05-20

# REST Client — TypeScript (`fetch` / `axios`)

This page mirrors `03_rest_client_python.md` for TypeScript. Types are hand-written to match the Pydantic models in `archon_search/server/schemas.py` exactly. The authoritative contract is `GET /openapi.json`; if you need machine-generated types, run `openapi-typescript` against that endpoint.

## Principles

1. **Hand-roll the auth.** There is no SDK. Build a thin client that owns the base URL and the bearer header.
2. **Types track the Pydantic models.** Keep one file (e.g. `archonTypes.ts`) and update it when `schemas.py` changes. Cross-reference `BREAKING.md`.
3. **Don't trust `/search` to fail loudly on pipeline errors.** Pipeline execution errors return HTTP 200 with `results=[]` today (`routes_search.py` `search()` exception handler — debt `CON-5` / roadmap A4 #Unverified). Note that not *all* failure modes collapse to 200: a missing collection returns 404 and a meta-store lookup failure returns 503. If "no hits" is operationally significant for you, alarm on a sustained empty-results window in addition to 4xx/5xx.

## Types

Mirroring `archon_search/server/schemas.py` and the request models in `routes_search.py` / `routes_collections.py` / `routes_jobs.py`:

```ts
// archonTypes.ts

export interface HealthResponse {
  status: "running";   // server emits the literal string "running"
  version: string;
}

export interface CollectionSummary {
  name: string;
  path: string;
  description: string;
  doc_count: number;
  chunk_count: number;
  namespace: string;
  status: string;
}

export interface CollectionDetail extends CollectionSummary {
  embedding_model: string;
  centroid_present: boolean;
  last_indexed: string | null;
  acl_protected_count: number;
  acl_open_count: number;
}

export interface JobResponse {
  job_id: string;
  // Wire values are UPPER-CASE — matches archon_search/types.py JobStatus enum.
  status: "PENDING" | "RUNNING" | "DONE" | "FAILED" | "CANCELLING" | "CANCELLED";
  created_at: string;
  updated_at: string;
  result: string | null;
  error: string | null;
  namespace: string;
}

export interface DeleteResponse {
  name: string;
  deleted: boolean;
}

export interface SearchRequest {
  collection: string;
  query: string;
  // Accepted (server bounds it to [1, 100]) but ignored at runtime —
  // see BREAKING.md. Pipeline uses `[search] top_k_return` from config.
  top_k?: number;
}

export interface SearchResult {
  doc_id: string;
  chunk_id: string;
  text: string;
  score: number;
  source_path: string;
}

export interface SearchResponse {
  results: SearchResult[];
  // `true` indicates one or more candidate results were dropped by ACL
  // before being returned to the caller. The result list itself is the
  // post-ACL set — `acl_filtered` is the signal that filtering happened.
  acl_filtered: boolean;
}

export interface RouteRequest {
  query: string;
  // Server rejects empty/whitespace query with HTTP 400 and rejects
  // `slots < 1` with HTTP 400. Type allows any number for flexibility.
  slots?: number | null;
}

export interface RouteResponse {
  pre_context: string | null;
  pinned_names: string[];
  routable_names: string[];
  decomposer_invoked: boolean;
}

export interface IngestRequest {
  collection: string;
  path?: string | null;
  documents?: Array<Record<string, unknown>> | null;
  // NOTE: server overwrites this from the `X-Ingested-By` header (default
  // "archon-search-cli"). Setting it in the JSON body is a no-op — only the
  // header is honoured. See routes_jobs.py / routes_collections.py.
  ingested_by?: string;
}

// AddCollectionRequest accepts only `path`. There is no `ingested_by` body
// field — pass it via the `X-Ingested-By` header. Same for the reindex route.
export interface AddCollectionRequest {
  path: string;
}

// FastAPI's `HTTPException` envelope. Note that 422 validation responses
// use `detail: Array<{...}>` (a list of error objects), not a string.
export interface ErrorDetail {
  detail: string;
}
```

## Scope of this client

The `ArchonClient` below covers the integration surface most callers need:
`/health`, `/search`, `/route`, `/ingest`, `/jobs/{id}`, and the
`/collections/*` resources (list, get, add, remove, reindex). It deliberately
omits the operator/observability endpoints — `GET /status`, `GET /state`, and
`GET /telemetry/*` — which exist and are authenticated but are out of scope
for application integration. Add wrappers as needed.

URL-shape note: the list/add handlers are mounted under `prefix="/collections"`
at `"/"`, so the canonical paths are `GET /collections/` and `POST /collections/`
(trailing slash). The per-name handlers `GET/DELETE /collections/{name}` and
`POST /collections/{name}/reindex` have no trailing slash. Don't "normalise"
the slashes — they're meaningful here.

## A minimal client

```ts
// archonClient.ts
import type {
  AddCollectionRequest, CollectionDetail, CollectionSummary, DeleteResponse,
  HealthResponse, IngestRequest, JobResponse, RouteRequest, RouteResponse,
  SearchRequest, SearchResponse,
} from "./archonTypes";

export class ArchonClient {
  constructor(
    private readonly baseUrl: string,
    private readonly token: string,
  ) {}

  private async request<T>(path: string, init: RequestInit = {}): Promise<T> {
    const res = await fetch(`${this.baseUrl}${path}`, {
      ...init,
      headers: {
        "Authorization": `Bearer ${this.token}`,
        "Content-Type": "application/json",
        ...(init.headers ?? {}),
      },
    });
    if (!res.ok) {
      const body = await res.text();
      throw new ArchonError(res.status, body, path);
    }
    return res.json() as Promise<T>;
  }

  async health(): Promise<HealthResponse> {
    // Unauthenticated — bypass the bearer header for parity with curl.
    const r = await fetch(`${this.baseUrl}/health`);
    if (!r.ok) {
      throw new ArchonError(r.status, await r.text(), "/health");
    }
    return r.json() as Promise<HealthResponse>;
  }

  listCollections(): Promise<CollectionSummary[]> {
    return this.request("/collections/");
  }

  getCollection(name: string): Promise<CollectionDetail> {
    return this.request(`/collections/${encodeURIComponent(name)}`);
  }

  addCollection(body: AddCollectionRequest, ingestedBy?: string): Promise<JobResponse> {
    return this.request("/collections/", {
      method: "POST",
      body: JSON.stringify(body),
      headers: ingestedBy ? { "X-Ingested-By": ingestedBy } : {},
    });
  }

  removeCollection(name: string): Promise<DeleteResponse> {
    return this.request(`/collections/${encodeURIComponent(name)}`, { method: "DELETE" });
  }

  // POST /collections/{name}/reindex — returns 202 + JobResponse. Use
  // waitForJob() to follow the job through to a terminal status.
  reindexCollection(name: string, ingestedBy?: string): Promise<JobResponse> {
    return this.request(`/collections/${encodeURIComponent(name)}/reindex`, {
      method: "POST",
      headers: ingestedBy ? { "X-Ingested-By": ingestedBy } : {},
    });
  }

  search(body: SearchRequest): Promise<SearchResponse> {
    return this.request("/search", { method: "POST", body: JSON.stringify(body) });
  }

  route(body: RouteRequest): Promise<RouteResponse> {
    return this.request("/route", { method: "POST", body: JSON.stringify(body) });
  }

  ingest(body: IngestRequest, ingestedBy?: string): Promise<JobResponse> {
    return this.request("/ingest", {
      method: "POST",
      body: JSON.stringify(body),
      headers: ingestedBy ? { "X-Ingested-By": ingestedBy } : {},
    });
  }

  getJob(jobId: string): Promise<JobResponse> {
    return this.request(`/jobs/${encodeURIComponent(jobId)}`);
  }

  // DELETE /jobs/{id} — for terminal jobs (DONE/FAILED/CANCELLED) the
  // server returns 200 with the current JobResponse (idempotent). For
  // active jobs (PENDING/RUNNING) or jobs already in CANCELLING, the
  // server returns 202 with a JobResponse whose status is `CANCELLING` —
  // cancellation has only been *requested*, not confirmed. Poll the job
  // via getJob() (or waitForJob()) until it reaches CANCELLED.
  cancelJob(jobId: string): Promise<JobResponse> {
    return this.request(`/jobs/${encodeURIComponent(jobId)}`, { method: "DELETE" });
  }
}

export class ArchonError extends Error {
  constructor(public status: number, public body: string, public path: string) {
    super(`archon-search ${status} on ${path}: ${body}`);
  }

  /** Best-effort parse of the FastAPI `{detail: string}` envelope.
   * Returns `null` if the body is not JSON, or if `detail` is a list (422
   * validation responses use `detail: [...]`). */
  parsedDetail(): string | null {
    try {
      const parsed = JSON.parse(this.body) as { detail?: unknown };
      return typeof parsed.detail === "string" ? parsed.detail : null;
    } catch {
      return null;
    }
  }
}
```

## Loading the key

In Node:

```ts
import { readFileSync } from "node:fs";
import { homedir } from "node:os";
import { join } from "node:path";

function loadArchonKey(): string {
  const text = readFileSync(join(homedir(), ".archon-search", ".search.env"), "utf8");
  for (const line of text.split("\n")) {
    if (line.startsWith("ARCHON_SEARCH_API_KEY=")) return line.slice("ARCHON_SEARCH_API_KEY=".length).trim();
  }
  throw new Error("archon-search key not found");
}

const client = new ArchonClient("http://127.0.0.1:8765", loadArchonKey());
```

## Flow: search

```ts
const { results, acl_filtered } = await client.search({
  collection: "code-archon-search",
  query: "how is the API key bootstrapped",
});
for (const hit of results) {
  console.log(`${hit.score.toFixed(3)}  ${hit.source_path}`);
}
```

`top_k` is accepted but ignored — set `[search] top_k_return` in `archon-search.toml`. See `BREAKING.md`.

## Flow: register a collection and wait for ingest

```ts
async function waitForJob(jobId: string, timeoutMs = 300_000): Promise<JobResponse> {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    const job = await client.getJob(jobId);
    if (["DONE", "FAILED", "CANCELLED"].includes(job.status)) return job;
    await new Promise((r) => setTimeout(r, 1000));
  }
  throw new Error(`job ${jobId} did not finish in ${timeoutMs}ms`);
}

// `addCollection` returns 202 + JobResponse — `fetch` treats 202 as ok, so
// no special handling is needed in the client. Same for `ingest` and
// `reindexCollection`.
const job = await client.addCollection({ path: "/Users/me/docs/specs" }, "my-tool/1.0");
const final = await waitForJob(job.job_id);
if (final.status !== "DONE") throw new Error(final.error ?? "ingest failed");
```

## Flow: route

```ts
const r = await client.route({ query: "summarize this auth flow", slots: 3 });
// r.pinned_names + r.routable_names is the set you can fan out searches over.
```

## Using axios instead

If you already use `axios`, build an instance with `baseURL` and an `Authorization` header in `axios.create({...})`; reuse the same `archonTypes.ts` types as the generic of each call (`http.post<SearchResponse>("/search", body)`). Axios surfaces non-2xx as an exception (`error.response.data.detail`), so the envelope from `06_error_handling.md` is directly accessible without manual `res.ok` checks. Caveat: for 4xx/5xx responses raised via `HTTPException` the `detail` is a string, but for FastAPI's automatic 422 validation responses `detail` is a list of error objects — narrow the type before reading it as text.

## Related documents

- [`03_rest_client_python.md`](./03_rest_client_python.md) — same flows in Python.
- [`06_error_handling.md`](./06_error_handling.md) — status codes and error envelopes.
- [`../Architecture/600_api_reference_or_public_interface.md`](../Architecture/600_api_reference_or_public_interface.md) — full endpoint reference.
- [`../../BREAKING.md`](../../BREAKING.md) — `top_k` deprecation and other contract changes.
