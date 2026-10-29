# Composite completed-question execution and progress

## Root cause and reproduction

- **PROVEN:** `_handle_task_plan` built a fresh child `ChatRequest`, retaining task extraction but losing `_completed_question_execution` and the caller's `prompt`. `_handle` therefore did not enter the completed-question surface-ASL path for that child.
- **PROVEN:** A regression using `查询上海地区各经销商的已合作医院数` plus `查询上海地区的区域全部医院总数` reproduced task 1 COMPLETED and task 2 NEEDS_CLARIFICATION before the patch. The planned metric was present; task 2 was not omitted by the planner. Legacy readiness, rather than ASL binding from the completed question and extraction, blocked it.
- **PROVEN:** Child completeness and clarification events were not isolated by the composite SSE branch. They could render whole-turn "skip all tools" wording while another child was executing. A skipped child had no retrieval terminal event, so the existing result-validation/insight barrier could wait until stream flush.
- **HIGH_CONFIDENCE:** This missing handoff explains the supplied production trace. No production request or live-model replay was performed during this implementation turn.

## Changes

- Independent, already-completed child questions retain their parent's completed-question execution mode, deep-copied user prompt, and existing authorized scope context. Each child continues passing its own extraction unchanged to ASL.
- Pending answers, dependent/contextual children and native authorized plans retain their existing execution path. No keyword list or metric-specific hospital shortcut was added.
- Each child reports a task-local terminal milestone, including clarification, exception, dependency skip and checkpoint reuse. Deduplicated task aliases also retain visible task status.
- Composite SSE suppresses whole-turn clarification scaffolding from child events. A child awaiting input appears in its own execution block; the root response still carries NEEDS_CLARIFICATION and the original pending contract.
- Execution blocks are displayed in task-index order even if later tasks finish first. Backend DAG concurrency is unchanged. Skipped tasks release section barriers without fabricated validation or insight results; successful siblings still show their real evidence.
- Composite final output uses the analytic heading family. Standalone clarification display remains unchanged. The analytic order remains intent → planning → execution → validation → insight → final output.

## Validation

- Pre-fix reproduction: four new cases failed, including the exact pair of task questions and three prompt/context handoff variants.
- Baseline full offline: 4,286 passed, 11 failed; no collection errors.
- Full regression: 4,296 passed, same 11 failures; 10 added cases passed, no removed tests, no old-pass → new-fail or old-fail → new-pass changes.
- Final targeted + Critical Suite: 339 passed. This final run additionally covers two new native/contextual boundary cases and the final terminal-event guard; these last two cases are not counted in the earlier full-run total.
- Existing failures remain confined to MCP analysis tests using obsolete method/configuration names. No old assertions were weakened, and that module was not modified.
- New coverage includes isolated ASL payloads and scope, caller prompt preservation, Pending replies, dependent/native/contextual paths, failures, dependency skips, checkpoint reuse, and a mixed success/clarification SSE stream whose six headings remain ordered and unique.

## Review and release manifest

- `app/services/orchestrator.py`
- `app/api.py`
- `tests/test_composite_surface_execution.py`
- This report

Changes affect only the proven composite request/progress chain. SQL, Oagnet, scope authorization, business definitions, runtime configuration and other teams' modules are unchanged. Exact-file SHA-256 synchronization is required before committing to the version repository.

Production API retains a colleague's extra planning-RUNNING release logic recorded in `planning_display_layout_20260924.md`. A future deployment must preserve that reviewed remote-only difference rather than overwrite the whole API file. This implementation request does not authorize deployment.

## Known independent risk

The user-supplied SQL joins city via `dealer.city_id`: the regional predicate filters dealer location, not hospital location. Whether that matches "经销商在上海地区的已合作医院数" needs a business/semantic-role decision. This task does not change that join or claim to validate its business meaning.

Current stage: V1 composite-flow defect correction. No V2 cutover or catalog/evaluation/shadow readiness change; no production-switch claim.
