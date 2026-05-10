# REST API Reference

Base URL: `http://localhost:8080/api/v1`

## Authentication

All endpoints require a Bearer token in the `Authorization` header:
```
Authorization: Bearer <your-token>
```

## Endpoints

### POST /search
Perform a semantic search over indexed documents.

**Request body:**
```json
{
  "query": "how to configure rate limiting",
  "collection": "docs",
  "top_k": 5
}
```

**Response:**
```json
{
  "results": [
    {
      "doc_id": "doc-001",
      "score": 0.92,
      "text": "Rate limiting is configured via..."
    }
  ]
}
```

### POST /ingest
Add a document to a collection.

### GET /collections
List all available collections with document counts.

### DELETE /collections/{name}
Remove a collection and all its documents.
