# Data Model

## Core Entities

### Document
| Field | Type | Description |
|-------|------|-------------|
| `id` | UUID | Primary key, server-assigned |
| `collection_id` | UUID | Parent collection |
| `source_path` | string | Original file path or URL |
| `content` | text | Raw document content |
| `metadata` | JSONB | Arbitrary key-value pairs |
| `created_at` | timestamp | Ingestion timestamp (UTC) |
| `updated_at` | timestamp | Last modification (UTC) |

### Chunk
| Field | Type | Description |
|-------|------|-------------|
| `id` | UUID | Primary key |
| `document_id` | UUID | Parent document |
| `text` | text | Chunk content |
| `vector` | float[] | Embedding vector |
| `chunk_index` | integer | Order within document |

### Collection
| Field | Type | Description |
|-------|------|-------------|
| `id` | UUID | Primary key |
| `name` | string | Unique, URL-safe name |
| `embedding_model` | string | Model used for embeddings |
| `doc_count` | integer | Cached document count |

## Relationships

A Collection contains many Documents. Each Document is split into one or more Chunks during ingestion. Semantic search operates over Chunk vectors.
