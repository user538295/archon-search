## Bug: Single-file ingest over `max_file_mb` returns 413 with `code="file_too_large"`

**ID**: S220-rest_returns_413_file_too_large
**Scenario**: S220
**Severity**: medium
**Version**: archon-search, version 26.8.1751

### What happened
AssertionError: 413 body carries no `file_too_large` code anywhere (50_ingestion_and_collections.md:54); status=413 body={'detail': 'File size 3 MB exceeds the configured limit of 1 MB (`[ingest].max_file_mb`). Raise the limit in `archon-search.toml` or split the file.'}
assert 'file_too_large' in '{"detail": "File size 3 MB exceeds the configured limit of 1 MB (`[ingest].max_file_mb`). Raise the limit in `archon-search.toml` or split the file."}'

### What should happen
- Step 1: exits 1; output contains the file size, the configured limit (`1`), and guidance on how to fix it.
- Step 2: HTTP status `413`; response body JSON contains `"code": "file_too_large"`.

### Steps to reproduce
Setup:
```bash
# Set the guard (stop, edit TOML, restart)
archon-search stop
# Edit ~/.archon-search/archon-search.toml: add under [ingest]: max_file_mb = 1
archon-search start
sleep 3

# Create a 2 MB file
dd if=/dev/urandom bs=1024 count=2048 2>/dev/null | base64 > /tmp/archon-bigfile.txt
```

1. `archon-search ingest --path /tmp/archon-bigfile.txt --collection bigfile-test`
2. ```bash
   curl -s -w "\n%{http_code}" -X POST http://127.0.0.1:8765/ingest \
     -H "Authorization: Bearer $ARCHON_SEARCH_API_KEY" \
     -H "Content-Type: application/json" \
     -d '{"path":"/tmp/archon-bigfile.txt","collection":"bigfile-rest"}'
   ```

### Evidence
```
E   AssertionError: 413 body carries no `file_too_large` code anywhere (50_ingestion_and_collections.md:54); status=413 body={'detail': 'File size 3 MB exceeds the configured limit of 1 MB (`[ingest].max_file_mb`). Raise the limit in `archon-search.toml` or split the file.'}
E   assert 'file_too_large' in '{"detail": "File size 3 MB exceeds the configured limit of 1 MB (`[ingest].max_file_mb`). Raise the limit in `archon-search.toml` or split the file."}'
E    +  where '{"detail": "File size 3 MB exceeds the configured limit of 1 MB (`[ingest].max_file_mb`). Raise the limit in `archon-search.toml` or split the file."}' = <function dumps at 0x105a42090>({'detail': 'File size 3 MB exceeds the configured limit of 1 MB (`[ingest].max_file_mb`). Raise the limit in `archon-search.toml` or split the file.'})
E    +    where <function dumps at 0x105a42090> = json.dumps
```
