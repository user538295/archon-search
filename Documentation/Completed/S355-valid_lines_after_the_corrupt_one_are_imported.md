## Bug: `import --on-error skip` completes and reports a non-zero `skipped` count

**ID**: S355-valid_lines_after_the_corrupt_one_are_imported
**Scenario**: S355
**Severity**: medium
**Version**: archon-search, version 26.8.1848

### What happened
AssertionError: the import reported imported=1, skipped=1, total=3: 1 valid chunk(s) were neither imported nor counted as skipped. UserManual/90_export_import.md:87 says `skip` logs the corrupt line and CONTINUES, so with exactly one corrupt line every other line must be imported (:118 shows imported == total when skipped is 0)
stdout:
Import job submitted: c9429542-89e9-40bd-b253-fa579143f40f
[indexing] 1/3
Done. imported=1, skipped=1, total=3

stderr:
Warning: 1 document(s) were skipped due to errors.

assert 1 == (3 - 1)

### What should happen
- Step 3 exits **`0`** — 90:87 says `skip` "continues", so the job reaches `DONE`, and
  100:98 makes `DONE` an exit-`0` outcome for a `--wait` command.
- Step 3 prints the documented completion triple `Done. imported=<i>, skipped=<s>, total=<t>`
  (90:88 "print progress + `imported/skipped/total`"; 90:118 shows the exact line).
- The reported `skipped` count is **non-zero** (`>= 1`) — 90:87 "surfacing the count in the
  result" is only observable when the corrupt line the archive carries is actually counted.
- `total` equals the number of `documents.jsonl` lines in the archive — 90:31 "One JSON object
  per chunk", and 90:118 shows `total` equal to the archive's chunk count.
- Step 4: the job's `status` is **`DONE`** and its `result` object carries exactly the three
  documented keys `imported`, `skipped`, `total_in_archive` (90:151). `result["skipped"]` and
  `result["total_in_archive"]` equal the `skipped` and `total` the CLI printed.
- **`skip` continues past the corrupt line**: the archive holds exactly one corrupt line, so
  continuing to the end of `documents.jsonl` imports every other line —
  `imported == total - skipped` (90:87 "continues"; 90:118 shows `imported == total` when
  `skipped` is 0). A smaller `imported` means valid chunks were dropped without being counted
  as skipped, which neither line documents.
- Step 5 exits **`0`** as well, with a non-zero `skipped` count: 90:87 attaches no positional
  condition to `skip` — a corrupt **first** line is still "logged and continued past", not a
  reason to abort. Aborting there would be the documented behavior of `--on-error fail`, the
  mode the caller explicitly did not choose.

### Steps to reproduce
1. Export the seeded collection into a directory inside the server data dir:
   ```bash
   mkdir -p ~/.archon-search/s355-work
   archon-search export archon_test_docs --output-dir ~/.archon-search/s355-work --wait
   # note the printed archive path as $ARCHIVE
   ```
2. Build two copies of `$ARCHIVE`, each with exactly **one** `documents.jsonl` line replaced by the
   invalid JSON text `{ this is not valid json`, leaving `manifest.json` untouched (so
   `schema_version` and `active_embedding_model` still match and the only defect is the line):
   ```bash
   python3 - <<'PY'
   import io, tarfile
   from pathlib import Path
   src = Path("<$ARCHIVE>")
   work = Path.home() / ".archon-search" / "s355-work"
   with tarfile.open(src, "r:gz") as t:
       manifest = t.extractfile("manifest.json").read()
       lines = t.extractfile("documents.jsonl").read().decode().splitlines()
   for label, idx in (("corrupt-middle", 1), ("corrupt-first", 0)):
       out = list(lines)
       out[idx] = "{ this is not valid json"
       payload = ("\n".join(out) + "\n").encode()
       with tarfile.open(work / f"{label}.tar.gz", "w:gz") as t:
           for name, data in (("manifest.json", manifest), ("documents.jsonl", payload)):
               info = tarfile.TarInfo(name); info.size = len(data)
               t.addfile(info, io.BytesIO(data))
   PY
   ```
3. `archon-search import s355_skip_middle ~/.archon-search/s355-work/corrupt-middle.tar.gz --on-error skip --wait; echo "exit=$?"`
4. `curl -s -H "Authorization: Bearer $ARCHON_SEARCH_API_KEY" http://127.0.0.1:8765/jobs/<job_id from step 3> | python3 -m json.tool`
5. `archon-search import s355_skip_first ~/.archon-search/s355-work/corrupt-first.tar.gz --on-error skip --wait; echo "exit=$?"`
6. Clean up: `archon-search collection remove s355_skip_middle; archon-search collection remove s355_skip_first`

### Evidence
```
E   AssertionError: the import reported imported=1, skipped=1, total=3: 1 valid chunk(s) were neither imported nor counted as skipped. UserManual/90_export_import.md:87 says `skip` logs the corrupt line and CONTINUES, so with exactly one corrupt line every other line must be imported (:118 shows imported == total when skipped is 0)
E     stdout:
E     Import job submitted: c9429542-89e9-40bd-b253-fa579143f40f
E     [indexing] 1/3
E     Done. imported=1, skipped=1, total=3
E     
E     stderr:
E     Warning: 1 document(s) were skipped due to errors.
E     
E   assert 1 == (3 - 1)
```
