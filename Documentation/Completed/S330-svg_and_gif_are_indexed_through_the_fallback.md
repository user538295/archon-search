## Bug: `.gif` / `.svg` / `.rtf` are not skipped: the fallback and markitdown still index them

**ID**: S330-svg_and_gif_are_indexed_through_the_fallback
**Scenario**: S330
**Severity**: medium
**Version**: archon-search, version 26.8.1848

### What happened
AssertionError: diagram.svg was not indexed at all — it is absent from GET /collections/{name}/documents. UserManual/50_ingestion_and_collections.md:39 states .svg 'also uses the plain-text fallback, but yields readable SVG/XML markup (indexable, though it contains XML tags rather than prose)' — and the byte-identical control.zzz indexes fine, so the content is readable. indexed documents: ['control.zzz', 'note.rtf']
assert None is not None

### What should happen
- `control.zzz` is listed as an indexed document — :34, an unknown extension goes to the built-in `Path.read_text` fallback. It carries the **same bytes** as `diagram.svg`, so it separates "the fallback could not read this content" from "this extension is excluded", and proves the ingest ran.
- **`note.rtf` is listed as an indexed document with `chunk_count` ≥ 1** — :29 / :40, handled by core markitdown (:20).
- **`diagram.svg` is listed as an indexed document with `chunk_count` ≥ 1** — :39 calls its output "readable SVG/XML markup (indexable …)".
- **`pixel.gif` is listed as an indexed document with `chunk_count` ≥ 1** — :38, it "falls through to plain-text and produces garbage"; garbage text is still read and indexed, and :38's only documented exclusion is from the *image handler* (:33), not from ingestion.
- The ingest job reaches `DONE`.

### Steps to reproduce
1. Create `/tmp/archon_s330_docs` containing `diagram.svg` (`<svg …><text>…</text>…</svg>`), `control.zzz` (byte-identical to `diagram.svg`), `pixel.gif` (a real 1×1 GIF plus binary filler) and `note.rtf` (`{\rtf1\ansi … \par}`).
2. `curl -s -X POST -H "Authorization: Bearer $ARCHON_SEARCH_API_KEY" -H 'Content-Type: application/json' -d '{"path":"/tmp/archon_s330_docs"}' http://127.0.0.1:8765/collections/`
3. `curl -s -H "Authorization: Bearer $ARCHON_SEARCH_API_KEY" http://127.0.0.1:8765/collections/archon_s330_docs/documents`
4. `archon-search collection remove archon_s330_docs`

### Evidence
```
E   AssertionError: diagram.svg was not indexed at all — it is absent from GET /collections/{name}/documents. UserManual/50_ingestion_and_collections.md:39 states .svg 'also uses the plain-text fallback, but yields readable SVG/XML markup (indexable, though it contains XML tags rather than prose)' — and the byte-identical control.zzz indexes fine, so the content is readable. indexed documents: ['control.zzz', 'note.rtf']
E   assert None is not None
```
