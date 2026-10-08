# Root-goal insight and final reporting

## Scope and evidence

- PROVEN: combined synthesis received child material without the completed root question's planning context; final composition appended each child's response after the combined insight.
- Change: retain the completed root question, original input, confirmed planning summary, task parameters/dependencies and resumed task scopes. Query/calculation tasks supply evidence; one root synthesis produces detailed insight plus a separate short final presentation plan.
- Final prose addresses the user goal. Program-owned result tables, charts and attachment links remain authoritative. Result selection follows requested deliverables, not the last task or every task mechanically. Missing/unknown result references fall back to available results without an execution gate.
- All task statuses reach synthesis. Partial/empty/failed and preview limitations remain visible. Clarification/checkpoint context is restored only under the existing semantic-scope contract.
- Removed the obsolete composite-answer execution entry point. No SQL, semantic configuration, external API/SSE schema, permissions or V2 routing changes.
- Existing seven-stage order and three-section final layout remain unchanged. Detailed insight is not repeated in the final answer.

## Verification

- Baseline: 4439 passing tests, commit a89b041.
- Final local offline suite: 4449 passed, 8 pre-existing pytest reporting warnings, zero failures or collection errors. Added 10 root-goal cases.
- STALE_TEST: child-by-child insight expectations changed to one root insight; final composition expectations changed to short goal-oriented reporting. Other execution stages, clarification, scope and attachment contracts remain covered.
- Tests cover explicit multiple deliverables versus derived coverage only, invalid model selection fallback, partial/preview results, empty/cached branches, optional output parsing, root planning inputs and pending-context persistence.
- Review: limited to DataAnalysis synthesis, request-private context, DAG orchestration and presentation. Runtime configuration and sibling services remain untouched.

## Change manifest

- app/analysis/synthesis.py
- app/domain/models.py
- app/services/orchestrator.py
- app/presentation/root_report.py
- tests/test_analysis_synthesis.py
- tests/test_task_dag.py
- tests/test_api.py
- tests/test_root_goal_report.py

## Deployment

- Runtime commit: 70ba955. Published to the existing feature branch/Draft PR, not merged.
- Guarded deployment compared each target with the preflight snapshot, backed up originals and verified final hashes. Only the DataAnalysis service was restarted at 2026-09-29 13:44:48 CST; new PID 576387, READY. Sibling services and vector health passed; configuration hashes remained unchanged.
- Remote selected suite: 352 passed, one dependency deprecation warning.
- Live single-task trend: COMPLETED; seven-stage order, three-section layout, table in overview and chart in findings passed.
- Live multi-task query of two Shanghai hospital metrics: COMPLETED in 73.3 seconds. Both requested results delivered, one root insight node, no child insight, no task-number report sections, no detailed-insight repetition, correct stage/section order, no chart in insight and no MCP takeover.
- SSE emits many completed-status fragments for one report. The live harness was corrected to count the public insight heading and root ownership, not individual completed text fragments; no production SSE change was needed.
- Direct real-model probe with explicitly synthetic inputs returned separate detailed claims and a final presentation plan. This is supplementary model-contract evidence, not business-query evidence.

## Known independent limitations

- PROVEN: the original per-dealer plus hospital-total sample returned missing upstream structured extraction and stopped at ASL before SQL. The planner/ASL extraction path was not modified in this release. The deeper cause of that missing output remains UNKNOWN; do not label it a timeout or a semantic defect without evidence.
- A separate Shanghai/Beijing sample returned the available Shanghai result and disclosed the Beijing catalog-value binding failure without dropping the successful result. No semantic data or index was changed.
- A direct-container probe without the production context bridge was not accepted as endpoint verification; only the successful actual HTTP/SSE runs above are reported as live acceptance.
- Optional model-output defects retain real results rather than blocking the response. Model-selected relevance remains probabilistic; no new semantic-review gate is introduced. Existing SQL/metric business semantics are not certified by presentation acceptance.
