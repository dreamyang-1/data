# Remote integration and regression repair — 2026-09-29

## Scope and evidence

Current task: preserve the newest remote attachment changes, repair the observed
regression differences, and publish the integrated code. This is not a V2 cutover.
Baseline commit: `eff07e3`. Baseline: 4400 passed, 31 failed, no collection errors.

- PROVEN: the remote orchestrator changed again after the preceding sync. Import
  its exact snapshot: derived computation summaries do not promise an attachment;
  composite exports use explicit response dataset IDs rather than guessing the
  latest dataset. Preserve original task links when a combined export is unavailable.
- A second preflight detected further remote planning presentation edits before
  any deployment write. Preserve those exact edits too: computation intent label,
  non-repeated dependency prose and one-based displayed task numbering. Internal
  dependency indexes remain zero-based. Related recheck: 150 tests passed.
- PROVEN: the standalone MCP runner referenced removed settings. Reuse existing
  file-analysis budgets and intent-model settings; do not revive automatic MCP
  takeover of ordinary SQL requests. The official file-analysis route still
  requires an uploaded file and configured MCP server.
- PROVEN: public relation labels had been applied to internal relation enums.
  Restore modification/history semantics internally while retaining the public
  label “问题追问”. Context mode, scope and authorization contracts are unchanged.
- STALE_TEST: accepted planning wording, relation presentation, the `off_topic`
  schema default, and the official file-analysis runner replaced older contracts.
  Update their assertions/mocks to the current contracts, retaining routing,
  failure handling, schema and tool-loop assertions. No failures were skipped.

## Explicit change manifest

Runtime:

- `app/services/orchestrator.py` — exact remote attachment snapshot.
- `app/planning/task_dag.py` — exact subsequent remote task-numbering prompt change.
- `app/services/mcp_analysis_runner.py` — current settings compatibility only.
- `app/semantic_v2/canonical_execution_bridge.py` — lossless internal relations.

Tests:

- `tests/test_api.py`
- `tests/test_intent_recognition_display.py`
- `tests/test_mcp_analysis_runner.py`
- `tests/test_v2_canonical_execution_bridge.py`
- `tests/test_v2_context_v1_execution_bridge.py`
- `tests/test_v2_model_parse_repairs.py`
- `tests/test_v2_raw_turn_recognition.py`
- `tests/test_remote_sync_attachment_contract.py` (new)

Development and canonical Git files are synchronized using this explicit manifest
and SHA-256 comparisons. No environment files, credentials, business query rows,
logs or backups are included. Oagnet and SQL Translator code are unchanged.

## Validation

- Initial targeted regression: 392 passed.
- Additional attachment/internal-relation cases and related regressions: 39 passed.
- Four new cases cover internal relations and imported attachment behavior.
- Full regression: **4435 passed, 0 failed**, no collection errors (389.99s).
  Eight existing JUnit `record_property` compatibility warnings are non-failures.
  Delta: 31 old-fail to new-pass, 0 old-pass to new-fail, 4 new passing cases.
  Includes critical, scope, pending-choice and stage-order regression suites.
- Final merged version full regression: **4435 passed, 0 failed**, no collection
  errors (405.46s); same eight reporting warnings.

Review: the second snapshot's unrelated old test assertions were not imported;
doing so would restore the removed default time range and regress current tests.
Only the identified new runtime changes were merged. The second full regression
passed for the final merged version before deployment.

## Release boundaries

Deploy only snapshot-verified changed files. Stop if the remote snapshot drifts.
Back up replaced files, verify configuration hashes, run remote offline regression,
restart only the affected DataAnalysis service, then check readiness and peer-service
health. The new test is staged for remote validation, not installed as a runtime file.
No semantic database writes, vector rebuilds, V2 cutover or PR merge are authorized
by this task.

## Deployment evidence

- Runtime commit: `dc0fe23` (includes repair commit `8c83015`), pushed to the
  existing feature branch; no PR merge.
- First publication attempt stopped on concurrent remote edits before writes.
- Next attempt exposed a missing fixture in the temporary validation package,
  not a business-code test failure. All replaced files were restored before any
  service restart. The package was corrected to include its recorded JSON fixture.
- Successful release: `sync-repair-dc0fe23-20260929-113211`.
- Remote offline regression: **650 passed**, one dependency deprecation warning.
- DataAnalysis restarted at **2026-09-29 11:33:17 CST**, new PID **26361**,
  active and READY (HTTP 200).
- SQL Translator health OK, Oagnet UP, vector health successful. These peer
  services were not restarted. Configuration hashes remained unchanged.
- Replaced files have a server-side rollback backup and final SHA-256 verification.
  The orchestrator and task planner already matched the imported snapshot and
  were not overwritten during publication.
- Post-restart read-only dealer-list query: COMPLETED in 51.0s. All seven main
  stages appeared in order; SQL completed, the result table was delivered, and
  ordinary SQL routing was not taken over by MCP. No raw business rows recorded.
- Current task has no remaining test/deployment blocker. Broader V2 replacement
  readiness remains outside this release; catalog/evaluation/shadow gaps and the
  existing cutover authorization boundary are not changed or claimed resolved.
