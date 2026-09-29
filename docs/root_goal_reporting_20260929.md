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

Pending guarded deployment and read-only live verification. Optional model-output defects retain real results rather than blocking the response. Model-selected relevance remains probabilistic; no new semantic-review gate is introduced.
