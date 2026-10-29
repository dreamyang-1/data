# Required dependency output readiness — 2026-10-08

## Proven root cause and minimal repair

A successful query with zero rows has `status=COMPLETED`. The DAG previously
checked that status alone, so a dependent entity query entered parsing without
the predecessor's required business object and produced an unrelated question.
The missing-response sentinel also allowed a `None` predecessor to pass.

Check required outputs before child cache restoration, context inference,
dataset compilation and ASL. Empty, failed, pending or already-skipped required
predecessors now produce a typed `SKIPPED` result with the actual reason, not a
new clarification. Propagate this through dependent layers; independent tasks
continue. Progress, final task results and report manifests agree on `SKIPPED`.
Final reports distinguish unexecuted downstream tasks from missing user inputs.

Explicit result evidence determines emptiness, never answer prose or the number
of preview rows. One aggregate row containing the number zero remains usable.
Truncated/unconfirmed results do not prove emptiness. Cached child output is
discarded when its required predecessor becomes unavailable; after recovery the
child can execute again. Existing all-failed template suppression is unchanged.

## Change manifest

- `app/planning/task_dependencies.py`: deterministic readiness and typed skip.
- `app/services/orchestrator.py`: early guard and consistent terminal statuses.
- `app/presentation/root_report.py`: downstream skip wording.
- `tests/test_task_dependency_empty.py`: 30 new regression cases.

No API/SSE schema, model, semantic scope, query conditions, ranking, physical
index or successful dependency-compilation behavior changes. No data/index
rebuild or V2 cutover. This fixes orchestration, not the underlying reason why a
particular hospital query returns no matching rows.

## Validation and review

- Local full baseline: 4,589 passed, no failures or collection errors.
- Local full after implementation: 4,618 passed, no failures or collection errors
  (29 new cases included; one subsequently added UI-order case separately passed).
- Final matching critical suite: 335 passed, including all 30 new cases and public
  stage order, DAG retry, report rendering, pending, API and scope contracts.
- Remote unchanged-source baseline: 305 passed; merged candidate: 335 passed.
  The sole warning in both is an existing Starlette test-client deprecation.
- Original salesperson/products question and three analogous dependency shapes
  tested through the actual DAG orchestrator with recorded empty-result evidence.
  These are offline behavioral regressions, not claims about production sales.
- No old-pass → new-fail; no assertion removals or production configuration edits.
- Reviewed source and remote diffs: only the early guard and three skip consumers
  change; remote-only code is retained by a clean three-way merge rather than
  overwritten. Existing source/remote differences are outside this task.

Current scope is this V1 orchestration defect. V2 replacement readiness is not
being claimed or advanced by this repair.

## Deployment acceptance

Installed the three runtime files after scoped offline regression and a
pre-deployment hash check; original files were backed up. Restarted only the
DataAnalysis service and verified a new process, `READY`, installed hashes,
unchanged OpenAPI schema and unchanged unrelated Python/configuration files.
The existing `V2_CONTEXT_V1_EXECUTION` mode remains unchanged. No other service
restart, database write or vector rebuild was performed. Remote-only additions
remain outside the local feature commit and were preserved in the deployed
three-way-merged orchestrator.
