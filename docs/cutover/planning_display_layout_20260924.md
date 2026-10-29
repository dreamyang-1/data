# Task planning display layout

## Scope and evidence

- PROVEN: `orchestrator.py` inserted the business domain between the split decision and its explanation, and appended a planned tool-chain line to every task.
- PROVEN: `api.py` replaced the whole planning message on missing-input clarification, discarding task details. Updating the normal template alone would not update that path.
- Change is presentation-only. ASL, SQL, routing, permissions, pending state, execution tools, environment and service configuration are unchanged.

## Display contract

Normal single-task example:

```text
拆分判断完成。当前问题无需拆分，按单任务执行。
任务1：查询问题
任务意图：明细查询
业务域：医药销售域
参数提取：...
```

For split requests, the first line is `拆分判断完成。已拆分为以下任务：`, followed by each task with the same field order. Domain labels are existing request-authorized labels (or their existing ID fallback), not inferred from task text. Missing labels are omitted as before. Structured extraction JSON is unchanged.

For missing-input clarification, use `当前任务参数不完整，暂停子任务拆分。` instead of a completed split decision, while preserving task details below it. Do not claim SQL execution will occur while waiting for input. Remove the old planning-chain text. Confirmed candidate replies still restore the complete original task before rendering.

`规划调用` is no longer rendered. Actual execution events and the existing six-node analytic sequence remain unchanged. Existing clarification heading names and routing are outside this change.

## Validation and review

- Targeted API suite: 66 passed.
- Critical suite plus candidate-confirmation contract: 255 passed.
- Six new streaming cases cover single-task, composite and clarification with explicit-domain and model-wide scope. Assertions cover decision placement, field order, absent planned-chain text, and all six analytic nodes in order through final output.
- Two existing candidate-confirmation tests now assert the new planning layout and absence of planned-chain text.
- STALE_TEST: previous assertions requiring `规划调用` were updated only because the user explicitly removed that display field.
- Full baseline: 4,280 passed, 11 failed, no collection errors. All 11 failures concern MCP analysis tests referencing obsolete method/configuration names (`_run_mcp_analysis`, `_should_dispatch_mcp_analysis`, `mcp_analysis_tool_timeout_seconds`). This task does not change that module or its tests.
- Full final: 4,286 passed, the same 11 failed, no collection errors; six added tests passed, no removed tests, no old-pass → new-fail or old-fail → new-pass changes. The final critical run also verified the strengthened six-node order assertions.
- Review: two production files only; no request/response schema changes, no tool-chain removal, no prompt/ASL/SQL edits, and no credentials or runtime configuration in the change manifest.

## Change manifest and release

- `app/services/orchestrator.py`
- `app/api.py`
- `tests/test_api.py`
- `tests/test_semantic_choice_contract.py`
- This report

Exact-file synchronization to the version repository requires SHA-256 equality. The implementation request did not include production deployment; the subsequent explicit deployment request is recorded below.

Current stage: V1 presentation maintenance. No V2 cutover, catalog, evaluation or shadow readiness claim; their existing gaps remain outside this change.

## User-authorized deployment

- Follow-up request explicitly authorized deployment and restart. Release: `recent-e1bdd6e-20260924-132628`.
- Two runtime files updated: `app/api.py` and `app/services/orchestrator.py`. Only the DataAnalysis service restarted, at 2026-09-24 13:26:44 CST; new PID 3005389, active, readiness HTTP 200 / READY, no automatic restarts.
- Drift guard initially stopped deployment before any runtime write. The remote API contained a colleague's additional planning-RUNNING event-release logic. That change was reviewed and retained; only the display hunk from `e1bdd6e` was applied to the remote API. Therefore this is **not** a whole-file-identical deployment of the repository API.
- Remote API before SHA-256: `18d37ca79d1efbe3497623cbbd6eb7880fe1399e48c3651a7045a54685f66e72`; merged after: `0d2c9b5d6382b5d06e4ec737b890470fb69cc5e754fb2135a2aa99949a1032f2`. Orchestrator after: `1bfb17d8f39bca9d09247190f11e9776c7901a8e9376e41d668ffbc481a509d1`.
- Remote API + candidate-confirmation tests: 109 passed (one dependency deprecation warning).
- Configuration hashes unchanged. Oagnet and SQL service PIDs unchanged; vector health passed. A protected pre-deployment backup and rollback manifest are retained under the release name.
- Live stream check: an isolated conversation querying the regional hospital total completed in 47.9 seconds, with no error code. Decision and explanation on the same line, business domain immediately below task intent, planned-call text absent, and extracted parameters retained: all passed. Intent → planning → SQL execution → result validation → insight order passed, followed by a COMPLETED final response. No business result rows or credentials retained in this report.
