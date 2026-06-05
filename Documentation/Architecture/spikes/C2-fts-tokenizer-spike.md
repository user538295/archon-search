# Spike: LanceDB FTS Language Tokenizer

**Date**: 2026-06-05
**LanceDB version**: 0.30.2 (installed at `.venv/lib/python3.12/site-packages/lancedb/`)
**Status**: COMPLETE

---

## 1. FTS Language Tokenizer Support — YES

### Finding

`lancedb.index.FTS` accepts a `language` parameter (type `str`, default `"English"`).

```python
from lancedb.index import FTS
await table.create_index("text", config=FTS(language="French"), replace=True)
```

This was verified empirically: the call succeeded on an async LanceDB table and FTS search returned results.

### Parameter name and accepted values

Parameter name: `language` (string, **not** an ISO code — use full English name with capital first letter).

Supported language values (from `lancedb/index.py:lang_mapping`):

| ISO 639-1 code | `language` string value |
|---|---|
| `ar` | `"Arabic"` |
| `da` | `"Danish"` |
| `du` | `"Dutch"` ⚠️ |
| `en` | `"English"` |
| `fi` | `"Finnish"` |
| `fr` | `"French"` |
| `de` | `"German"` |
| `gr` | `"Greek"` ⚠️ |
| `hu` | `"Hungarian"` |
| `it` | `"Italian"` |
| `no` | `"Norwegian"` ⚠️ |
| `pt` | `"Portuguese"` |
| `ro` | `"Romanian"` |
| `ru` | `"Russian"` |
| `es` | `"Spanish"` |
| `sv` | `"Swedish"` |
| `ta` | `"Tamil"` |
| `tr` | `"Turkish"` |

The `lang_mapping` dict in `lancedb/index.py` maps 2-letter codes to these full-name values. At index creation time, LanceDB raises `ValueError: not support the requested language` if an unsupported value is passed (error string verified in `table.py:3679`).

**⚠️ Non-standard ISO codes in `lang_mapping` — implementation hazard:**

- `"du"` (Dutch): ISO 639-1 standard code is `"nl"`. LanceDB uses `"du"`. If archon-search stores `"nl"` (as fasttext outputs), there is NO key in `lang_mapping` matching it — FTS will silently fall back to English stemming.
- `"gr"` (Greek): ISO 639-1 standard code is `"el"`. LanceDB uses `"gr"`. Same silent fallback risk.
- `"no"` (Norwegian): This happens to be the ISO 639-1 code for Norwegian (Norsk). However, fasttext may output `"nb"` (Bokmål) or `"nn"` (Nynorsk) for Norwegian text — neither matches LanceDB's `"no"` key.

**The `_LANCEDB_TOKENIZER_MAP` in Task 12.1 MUST account for these mismatches.** The map must bridge from fasttext/ISO 639-1 output codes to LanceDB's internal keys:
```python
# Required bridge entries (in addition to 1:1 mappings like "fr" → "French")
"nl": "Dutch",    # ISO 639-1 → LanceDB (LanceDB uses "du", not "nl")
"el": "Greek",    # ISO 639-1 → LanceDB (LanceDB uses "gr", not "el")
"nb": "Norwegian",  # fasttext Bokmål → LanceDB
"nn": "Norwegian",  # fasttext Nynorsk → LanceDB
```

**⚠️ Capitalization: LanceDB requires capitalized names (`"French"`, not `"french"`).** LanceDB raises `ValueError` for unrecognized values. The example in plan Task 12.1 line 495 (`{"fr": "french"}`) uses lowercase — that is WRONG and will raise `ValueError` at runtime. Use capitalized names as shown in the table above.

### Full FTS constructor signature (verified from `help(FTS)`)

```python
FTS(
    with_position: bool = False,
    base_tokenizer: Literal['simple', 'raw', 'whitespace'] = 'simple',
    language: str = 'English',
    max_token_length: Optional[int] = 40,
    lower_case: bool = True,
    stem: bool = True,
    remove_stop_words: bool = True,
    ascii_folding: bool = True,
    ngram_min_length: int = 3,
    ngram_max_length: int = 3,
    prefix_only: bool = False,
)
```

### Current usage in store.py

```python
# archon_search/store.py:rebuild_fts_index
await table.create_index("text", config=FTS(), replace=True)
```

To support per-collection language tokenizers, the call site only needs to pass `language`:

```python
await table.create_index("text", config=FTS(language=language), replace=True)
```

---

## 2. GROUP BY / Aggregate SQL Support — NO (Python-side workaround required)

### Finding

LanceDB 0.30.2 does **not** expose a `GROUP BY` / aggregate SQL surface through either the sync or async Python API:

- `LanceDBConnection` has no `.sql()` method — `AttributeError 'LanceDBConnection' object has no attribute 'sql'`
- `AsyncTable` has no `.group_by()` method — no such attribute on `AsyncTable` or `AsyncQuery`
- `LanceTable` (sync) also has no `.group_by()` method

Verified commands that failed:
```python
db.sql("SELECT lang, count(*) FROM t GROUP BY lang")      # AttributeError
tbl.group_by("lang").aggregate(...)                        # AttributeError
tbl.query().group_by("lang").aggregate(...)               # AttributeError
```

### Required workaround for `get_dominant_language` (Task 12.1)

Use PyArrow compute on a full column scan:

```python
import pyarrow.compute as pc
from collections import Counter

arrow_table = await tbl.query().select(["language"]).to_arrow()
lang_col = arrow_table.column("language")
counts = Counter(lang_col.to_pylist())
dominant = counts.most_common(1)[0][0] if counts else None
```

This is O(n) in row count — acceptable for typical collection sizes, but should be noted as a scalability constraint.

---

## 3. Empty String WHERE Clause — CONFIRMED

### Finding

`WHERE column = ''` (equality with empty string) returns correct results in LanceDB/DataFusion.

Verified empirically:

```python
db = lancedb.connect('/tmp/spike_test_db2')
tbl = db.create_table('t', pa.table({'lang': ['', 'en', ''], 'val': [1,2,3]}), mode='overwrite')
result = tbl.search().where("lang = ''").to_list()
# result: [{'lang': '', 'val': 1}, {'lang': '', 'val': 3}]  — count=2 ✓
```

Also verified via async `count_rows()` with a filter:

```python
count = await tbl.count_rows("lang = ''")
# count: 2 ✓
```

DataFusion correctly distinguishes `''` (empty string) from `NULL`. The schema stores missing language as `""` (not NULL) as per `store.py:_do_ingest` (`"language": c.language or ""`), so `WHERE language = ''` is the correct predicate for `count_untagged_language_chunks` in Task 9.1.

---

## 4. Phase 12 Decision — EXECUTE

FTS language tokenizer support is fully available in the installed LanceDB 0.30.2. The `language` parameter on `FTS()` is a first-class, documented, empirically verified feature. Phase 12 (per-language FTS index rebuilds) is implementable without library upgrades.

---

## 5. Implementation Notes

### Language code mapping

The `language` parameter uses **full English names** (e.g., `"French"`, not `"fr"` or `"fra"`). The mapping from ISO 639-1 two-letter codes to full names is available in `lancedb.index.lang_mapping`. The archon-search codebase stores language tags in the `language` column — the implementation must map stored tags to `lang_mapping` values before calling `FTS(language=...)`.

If a stored language tag is not in `lang_mapping` (e.g., a custom tag or an unsupported language), fall back to `FTS()` (default `"English"`) and log a warning. Do not let an unsupported language crash the index rebuild.

### GROUP BY constraint

No native SQL `GROUP BY` in LanceDB 0.30.2. All aggregation for `get_dominant_language` must be done Python-side via PyArrow or `Counter`. This is a full column scan — for very large collections this may be slow. Consider adding a separate metadata field for dominant language if this becomes a hot path.

### Empty string vs NULL

The `language` schema column is `pa.utf8()` (non-nullable in schema definition, `store.py:313`). Missing language is stored as `""`. `WHERE language = ''` correctly identifies untagged chunks. `IS NULL` would return zero results and should not be used.

### Multi-language collections

A single FTS index covers one language tokenizer. For a collection with mixed languages (e.g., English + French), only one tokenizer can be active per index. The per-language rebuild approach (Phase 12) implies either: (a) accepting one dominant-language tokenizer for the whole collection FTS index, or (b) maintaining one FTS index per language — a more complex design that requires per-query index selection. The current spike supports option (a); option (b) is out of scope.
