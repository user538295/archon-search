# Error Handling

## Error Response Format

All API errors return a consistent JSON envelope:

```json
{
  "error": {
    "code": "VALIDATION_ERROR",
    "message": "Field 'query' is required",
    "details": [
      {"field": "query", "reason": "missing"}
    ]
  }
}
```

## Error Codes

| Code | HTTP Status | Meaning |
|------|-------------|---------|
| `VALIDATION_ERROR` | 422 | Request body fails schema validation |
| `NOT_FOUND` | 404 | Resource does not exist |
| `UNAUTHORIZED` | 401 | Missing or invalid auth token |
| `FORBIDDEN` | 403 | Token lacks required permissions |
| `RATE_LIMITED` | 429 | Too many requests |
| `INTERNAL_ERROR` | 500 | Unexpected server error |

## Retrying Requests

Retry on `429` and `5xx` responses using exponential backoff:
- Initial delay: 500ms
- Multiplier: 2x
- Maximum delay: 30s
- Maximum attempts: 5

Never retry `4xx` errors except `429`.
