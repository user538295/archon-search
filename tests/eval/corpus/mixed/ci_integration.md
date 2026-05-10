# CI Integration for the Eval Harness

## Overview

The eval harness runs offline (no live server required) so it can execute in any CI environment.

## Running in CI

```bash
cd packages/archon-search
uv sync --group dev
uv run pytest tests/eval/ --no-cov -v
```

## What the Corpus Contract Tests Check

- Document count is in the required range (50–100).
- Query count is in the required range (25–30).
- Every query has at least one positive relevance label.
- Positive labels for retrieval queries belong to the correct collection.
- At least 2 distinct collections exist.
- All `doc_id` values are unique and stable.
- `routing/collections.jsonl` exists and lists all collections.

## Failing Tests

If corpus contract tests fail, the fix is always in the data files — not in the test code. Common causes:

| Error | Fix |
|-------|-----|
| Document count out of range | Add or remove entries in `documents.jsonl` and `corpus/` |
| Query without positive label | Add a label row in `labels.jsonl` |
| Unreachable positive label | Correct the collection field in the label or the document |
| Missing corpus file | Add the file under `corpus/` |
| Orphan corpus file | Add the file to `documents.jsonl` or delete it |
