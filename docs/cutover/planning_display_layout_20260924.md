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

Exact-file synchronization to the version repository requires SHA-256 equality. No production deployment is part of this request.

Current stage: V1 presentation maintenance. No V2 cutover, catalog, evaluation or shadow readiness claim; their existing gaps remain outside this change.
