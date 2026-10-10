# Completed-question validation and insight

## Scope and cause

Current stage: bounded V1 result-validation/insight repair; no V2 cutover change.

PROVEN: DAG children independently emitted public result-validation blocks,
while the parent only combined task statuses. The root insight already used the
completed question, but resumed confirmations could leave an older question in
the accompanying planning context. Child task prose must not replace the root
objective in either public node.

## Changes and manifest

- `app/services/orchestrator.py`: preserve internal child reliability checks;
  publish one parent validation against the completed root question before root
  insight; carry actual child/materialization warnings and missing-result limits.
  Normalize the analysis context to the same completed question and pass the
  validated reliability report as supporting context.
- `app/presentation/reliability.py`: identify the completed question used for
  validation; combine actual query quality without inferring PASS from task
  completion or inventing an unknown quality status.
- `app/analysis/synthesis.py`: normalize the root-question input at the model
  boundary without mutating the caller's stored state or child parameters.
- `tests/test_completed_goal_validation.py`: 17 new cases covering completed,
  partial, empty, failed, clarification, checkpoint and resumed outputs, model
  question authority, quality evidence and input immutability.
- `tests/test_task_dag.py`: actual multi-task flow emits one root validation
  before root insight and retains internal reliability results.
- `tests/test_api.py`: migrate the old child-validation SSE assertion to one
  root-question validation; retain quality/evidence and root/child insight checks.
- `tests/test_full_result_flow.py`: STALE_TEST migration of three deferred-child
  cases; child nodes no longer emit public root validation. Full rows, storage,
  preview cap, facts and non-deferred node-order assertions remain unchanged.
- This document is the only documentation change.

No ASL/SQL parameters, authorization, semantic assets/indexes, computation
support, public node names/order or public response schema are changed. The
root check is evidence-based reliability aggregation, not a new model-based
semantic correctness proof. A completed task is not proof of business causality.

## Verification and release boundary

- Final isolated Critical Suite (including the composite SSE check): 376 passed.
  New root-goal cases separately rerun: 17 passed.
- The migrated composite SSE assertion and new root-goal cases: 18 passed.
- Canonical offline full baseline: 4,778 passed / 95 failed / 1 collection error;
  candidate: 4,795 passed / the same 95 failed / the same 1 collection error.
  There are 17 new test identities, no newly failing identities and no removed
  cases. Runtime and three unaffected shards were unchanged; after the SSE-only
  assertion migration the affected full shard was rerun. Existing failures are
  retained, not described as a fully green suite.
- Server candidate snapshot scoped tests: 84 passed, using existing remote
  runtime overlays and an implementation-independent validated-plan unit fixture.
- Server integration release gate did not pass. Before this repair, its snapshot
  already had 108 failing cases and 12 collection errors: independently changed
  planning modules removed the old `MultiQuestionPlanner` exports still consumed
  by integration tests. The Critical Suite also could not collect those modules.
- The first server snapshot was rejected on source drift. No production source
  was overwritten; subsequent snapshots preserve independent remote changes.
- Deployment/restart/native production verification are NOT performed. Do not
  treat scoped tests, old-failure permission or snapshot tests as a substitute for
  an executable server integration gate. Reconcile the remote planner migration
  and rerun the gate before any release of this patch.

Code review: child checks and evidence remain available, no partial/failed result
is promoted to a successful full answer, synthesis receives actual result data,
and frozen seven-node ordering is protected by existing and new assertions.
Development/canonical files were synchronized by the explicit manifest and
checked with SHA-256. Unrelated local and remote edits are excluded/preserved.
Catalog/Evaluation/Shadow gaps and V1 replacement readiness are unchanged.
The next shortest release blocker is alignment of the remote planning API and
its integration tests, not a semantic database write or an index rebuild.
