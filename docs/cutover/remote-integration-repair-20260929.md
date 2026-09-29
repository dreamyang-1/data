# Remote integration and regression repair — 2026-09-29

## Scope and evidence

Current task: preserve the newest remote attachment changes, repair the observed
regression differences, and publish the integrated code. This is not a V2 cutover.
Baseline commit: `eff07e3`. Baseline: 4400 passed, 31 failed, no collection errors.

- PROVEN: the remote orchestrator changed again after the preceding sync. Import
  its exact snapshot: derived computation summaries do not promise an attachment;
  composite exports use explicit response dataset IDs rather than guessing the
  latest dataset. Preserve original task links when a combined export is unavailable.
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
- Deployment results will be recorded after completion.

## Release boundaries

Deploy only snapshot-verified changed files. Stop if the remote snapshot drifts.
Back up replaced files, verify configuration hashes, run remote offline regression,
restart only the affected DataAnalysis service, then check readiness and peer-service
health. The new test is staged for remote validation, not installed as a runtime file.
No semantic database writes, vector rebuilds, V2 cutover or PR merge are authorized
by this task.
