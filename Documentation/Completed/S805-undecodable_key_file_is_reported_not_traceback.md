## Bug: A key file that exists but cannot be read refuses startup with one `Error:` line and is left untouched

**ID**: S805-undecodable_key_file_is_reported_not_traceback
**Scenario**: S805
**Severity**: medium
**Version**: archon-search, version 26.10.2267

### What happened
Failed: bug: bugs/S805-undecodable_key_file_is_reported_not_traceback.md — serve printed a traceback for a non-UTF-8 key file instead of one `Error:` line (90_incident_runbook.md:216).
~~~~~~~~~~~~~~~^^^^^^^^^^
File "/Users/manczg/.local/share/uv/tools/archon-search/lib/python3.13/site-packages/archon_search/key_manager.py", line 895, in _parse_key_file
content = key_file.read_text()
File "/Users/manczg/.local/share/uv/python/cpython-3.13.14-macos-aarch64-none/lib/python3.13/pathlib/_local.py", line 546, in read_text
return PathBase.read_text(self, encoding, errors, newline)
~~~~~~~~~~~~~~~~~~^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
File "/Users/manczg/.local/share/uv/python/cpython-3.13.14-macos-aarch64-none/lib/python3.13/pathlib/_abc.py", line 633, in read_text
return f.read()
~~~~~~^^
File "<frozen codecs>", line 325, in decode
UnicodeDecodeError: 'utf-8' codec can't decode byte 0xff in position 0: invalid start byte

### What should happen
- Each launch **exits with status 1**, never serves, and does **not** replace the file with a generated key (`30_configuration.md:292`; `160_troubleshooting.md:46`).
- stderr carries exactly **one** `Error:` line naming the key file's path and **no traceback** (`90_incident_runbook.md:216,219`).
- The directory (step 1) is still a directory and the byte content (step 2) is unchanged (`30_configuration.md:292`).

### Steps to reproduce
1. Make `<data-dir>/.search.env` a **directory**; run `archon-search serve`.
2. Write `<data-dir>/.search.env` as bytes that are not valid UTF-8 (`\xff\xfe\x00`); run `archon-search serve`.
3. Inspect `<data-dir>/.search.env` afterwards.

### Evidence
```
E   Failed: bug: bugs/S805-undecodable_key_file_is_reported_not_traceback.md — serve printed a traceback for a non-UTF-8 key file instead of one `Error:` line (90_incident_runbook.md:216).
E                  ~~~~~~~~~~~~~~~^^^^^^^^^^
E     File "/Users/manczg/.local/share/uv/tools/archon-search/lib/python3.13/site-packages/archon_search/key_manager.py", line 895, in _parse_key_file
E       content = key_file.read_text()
E     File "/Users/manczg/.local/share/uv/python/cpython-3.13.14-macos-aarch64-none/lib/python3.13/pathlib/_local.py", line 546, in read_text
E       return PathBase.read_text(self, encoding, errors, newline)
E              ~~~~~~~~~~~~~~~~~~^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
E     File "/Users/manczg/.local/share/uv/python/cpython-3.13.14-macos-aarch64-none/lib/python3.13/pathlib/_abc.py", line 633, in read_text
E       return f.read()
E              ~~~~~~^^
E     File "<frozen codecs>", line 325, in decode
E   UnicodeDecodeError: 'utf-8' codec can't decode byte 0xff in position 0: invalid start byte
```
