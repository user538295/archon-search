## Bug: `log_file = ""` disables file logging; startup warning emitted

**ID**: S107-empty_log_file_disables_file_logging_and_warns
**Scenario**: S107
**Severity**: medium
**Version**: archon-search, version 26.8.1751

### What happened
AssertionError: no startup warning that file logging is disabled found in serve.log — the doc says an empty log_file outside container mode warns via load_config; serve.log tail:
INFO:     Started server process [63739]
INFO:     Waiting for application startup.
INFO:     Application startup complete.
INFO:     Uvicorn running on http://127.0.0.1:60502 (Press CTRL+C to quit)
2026-08-01 21:24:02.751 python[63739:35930862] 2026-08-01 21:24:02.751883 [W:onnxruntime:, session_state.cc:1387 VerifyEachNodeIsAssignedToAnEp] Some nodes were not assigned to the preferred execution providers which may or may not have an negative impact on performance. e.g. ORT explicitly assigns shape related ops to CPU to improve perf.
2026-08-01 21:24:02.751 python[63739:35930862] 2026-08-01 21:24:02.751964 [W:onnxruntime:, session_state.cc:1389 VerifyEachNodeIsAssignedToAnEp] Rerunning with verbose output on a non-minimal build will show node assignments.
2026-08-01 21:24:02.811 python[63739:35930862] 2026-08-01 21:24:02.811778 [W:onnxruntime:, coreml_execution_provider.cc:137 GetCapability] CoreMLExecutionProvider::GetCapability, number of partitions supported by CoreML: 39 number of nodes in the graph: 327 number of nodes supported by CoreML: 212
2026-08-01 21:24:03.835 python[63739:35930862] 2026-08-01 21:24:03.835708 [W:onnxruntime:, session_state.cc:1387 VerifyEachNodeIsAssignedToAnEp] Some nodes were not assigned to the preferred execution providers which may or may not have an negative impact on performance. e.g. ORT explicitly assigns shape related ops to CPU to improve perf.
2026-08-01 21:24:03.835 python[63739:35930862] 2026-08-01 21:24:03.835780 [W:onnxruntime:, session_state.cc:1389 VerifyEachNodeIsAssignedToAnEp] Rerunning with verbose output on a non-minimal build will show node assignments.
INFO:     127.0.0.1:60505 - "GET /health HTTP/1.1" 200 OK
assert False

### What should happen
- Step 4: server starts successfully (health returns 200); stderr output contains a
  warning message indicating file logging is disabled (e.g. references `log_file`
  being empty or file logging being off).
- Step 5: no new log file is created at the configured path while file logging is
  disabled (either `FILE_ABSENT` or the file's modification time predates step 4).
- Step 7: managed service restores; `GET /health` returns 200.

### Steps to reproduce
1. `archon-search stop`
2. `cp ~/.archon-search/archon-search.toml ~/.archon-search/archon-search.toml.bak`
3. ```bash
   python3 -c "
   import pathlib, re
   p = pathlib.Path.home() / '.archon-search/archon-search.toml'
   text = p.read_text()
   section = '[logging]\nlog_file = \"\"\n'
   if '[logging]' in text:
       text = re.sub(r'\[logging\][^\[]*', section, text, flags=re.DOTALL)
   else:
       text += '\n' + section
   p.write_text(text)
   "
   ```
4. ```bash
   SERVE_STDERR=$(archon-search serve 2>&1 &
   SERVE_PID=$!
   sleep 5
   curl -s -o /dev/null -w "%{http_code}" http://127.0.0.1:8765/health
   kill $SERVE_PID 2>/dev/null
   wait $SERVE_PID 2>/dev/null)
   echo "$SERVE_STDERR"
   ```
5. ```bash
   NEW_LOG=~/.archon-search/logs/archon-search.log
   ls "$NEW_LOG" 2>/dev/null && echo "FILE_EXISTS" || echo "FILE_ABSENT"
   ```
6. `cp ~/.archon-search/archon-search.toml.bak ~/.archon-search/archon-search.toml && rm ~/.archon-search/archon-search.toml.bak`
7. `archon-search start`

### Evidence
```
E   AssertionError: no startup warning that file logging is disabled found in serve.log — the doc says an empty log_file outside container mode warns via load_config; serve.log tail:
E     INFO:     Started server process [63739]
E     INFO:     Waiting for application startup.
E     INFO:     Application startup complete.
E     INFO:     Uvicorn running on http://127.0.0.1:60502 (Press CTRL+C to quit)
E     2026-08-01 21:24:02.751 python[63739:35930862] 2026-08-01 21:24:02.751883 [W:onnxruntime:, session_state.cc:1387 VerifyEachNodeIsAssignedToAnEp] Some nodes were not assigned to the preferred execution providers which may or may not have an negative impact on performance. e.g. ORT explicitly assigns shape related ops to CPU to improve perf.
E     2026-08-01 21:24:02.751 python[63739:35930862] 2026-08-01 21:24:02.751964 [W:onnxruntime:, session_state.cc:1389 VerifyEachNodeIsAssignedToAnEp] Rerunning with verbose output on a non-minimal build will show node assignments.
E     2026-08-01 21:24:02.811 python[63739:35930862] 2026-08-01 21:24:02.811778 [W:onnxruntime:, coreml_execution_provider.cc:137 GetCapability] CoreMLExecutionProvider::GetCapability, number of partitions supported by CoreML: 39 number of nodes in the graph: 327 number of nodes supported by CoreML: 212
E     2026-08-01 21:24:03.835 python[63739:35930862] 2026-08-01 21:24:03.835708 [W:onnxruntime:, session_state.cc:1387 VerifyEachNodeIsAssignedToAnEp] Some nodes were not assigned to the preferred execution providers which may or may not have an negative impact on performance. e.g. ORT explicitly assigns shape related ops to CPU to improve perf.
E     2026-08-01 21:24:03.835 python[63739:35930862] 2026-08-01 21:24:03.835780 [W:onnxruntime:, session_state.cc:1389 VerifyEachNodeIsAssignedToAnEp] Rerunning with verbose output on a non-minimal build will show node assignments.
E     INFO:     127.0.0.1:60505 - "GET /health HTTP/1.1" 200 OK
E   assert False
```
