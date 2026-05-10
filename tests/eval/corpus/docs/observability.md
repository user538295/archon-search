# Observability

## Metrics

Expose Prometheus metrics at `/metrics`. Key metrics:

| Metric | Type | Description |
|--------|------|-------------|
| `search_requests_total` | Counter | Total search requests by collection and status |
| `search_latency_seconds` | Histogram | End-to-end search latency |
| `embedding_batch_size` | Histogram | Chunks per embedding batch |
| `index_doc_count` | Gauge | Documents per collection |
| `cache_hit_ratio` | Gauge | Embedding cache hit rate |

## Structured Logging

All log lines are JSON with these standard fields:
- `timestamp` (ISO 8601)
- `level`
- `logger` (module path)
- `message`
- `trace_id` (propagated from `X-Trace-Id` request header)

## Tracing

OpenTelemetry spans are emitted for each stage of the ingestion and search pipelines. Configure the OTLP exporter with:

```toml
[telemetry]
otlp_endpoint = "http://localhost:4317"
service_name = "archon-search"
```

## Health Check

`GET /health` returns:
```json
{"status": "ok", "db": "ok", "index": "ok"}
```

Returns `503` with `"status": "degraded"` if any subsystem is unavailable.
