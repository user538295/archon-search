
**[2026-08-19] OOM RCA (compacted from learnings.md)**: original detail — per-file RSS CSV instrumentation; log-census = per-logger counts + uniq file paths; also check macOS DiagnosticReports and `sysctl kern.boottime` for crash/reboot correlation.

**[2026-08-19] bug briefs (compacted from learnings.md)**: dropped phrasing — "write the failing repro FIRST; probe every config permutation. A green test proves only code+test agree — diff it against the DOC contract."

## Evicted from learnings.md 2026-08-19 (lowest N, OCR-memory task)
- **[2026-08-18] (×1) instruction-file bloat**: before adding to `CLAUDE.md`, grep `Documentation/` and `pyproject.toml` for the fact — its Architecture section was 57% of the file and fully duplicated. Keep only what is not greppable.
