# Report boilerplate removal — 2026-10-10

Current Stage: scoped V1 presentation change closed and deployed, no V2 cutover.

## Proven cause and bounded change

PROVEN: `AnswerPlan.render_report` printed the entire question after the report
introduction, before the actual summary. The root reporter also wrapped the
generic unavailable-calculation paragraph with the full planner instruction.
The user explicitly requested removing both redundant blocks.

- Remove the `本次分析` question echo from the shared single/multi-task renderer.
  Keep the introduction, actual summary, tables, charts and section order.
- Shorten the generic unavailable-calculation message to one factual sentence.
  Preserve FAILED status, FAIL reliability and the absence of a derived artifact.
- Root presentation uses that short status without repeating the calculation
  instruction. The same old boilerplate in cached material is shortened only
  when the structured reliability gate identifies an unsuccessful calculation.
  Specific reasons and independent failures/clarifications remain visible.

This is not a change to calculation support or a claim that the screenshot's
calculation succeeded. No SQL, ASL, semantic data, authorization, model, public
schema or public progress-node behavior is changed.

## Verification and contract migration

- Local scoped baseline: 163 passed. Candidate: 172 passed, including 9 new
  cases for single/root reports, cached generic failure, partial/full success,
  all-failed output and preservation of meaningful reasons and other tasks.
- STALE_TEST: three old assertions (five parametrized identities) expected the
  removed question echo. Updated only that requirement under the user's explicit
  presentation decision; retained table/chart/content/node assertions.
- Local and server Critical Suite: 345 passed.
- Server full baseline: 4,771 passed, 93 failed, 1 collection error; candidate:
  4,780 passed, 93 failed, 1 collection error. Nine new tests, zero new failures,
  zero removed test identities, zero old-fail to new-pass transitions.
- Deployed the exact three runtime files with backup, bounded patching over
  independent remote changes, restart, fresh worker and readiness verification.
  Protected configuration and unrelated source hashes remained unchanged.
- Native screenshot question passed all wording and seven-node checks. The
  response correctly retained PARTIAL_SUCCESS when its calculation was still
  unavailable; this presentation change did not promote it to COMPLETED.
  The question echo, long generic paragraph and repeated calculation instruction
  were absent. Raw service/business evidence remains private, outside Git.
- Review: no task completion/reliability promotion, no discarded data or failure
  facts, no unrelated code/config replacement; production evidence stays private.

## Manifest and boundaries

Runtime: `app/analysis/interpretation.py`, `app/presentation/root_report.py`,
`app/services/orchestrator.py` (one message literal only).
Tests: `test_report_boilerplate_removal.py`, `test_result_introduction_wording.py`,
`test_insight_interpretation.py`, `test_task_dag.py`. Documentation: this report.
Explicit-file canonical synchronization is SHA-256 checked before commit.

Cutover Blocker P0/P1, Catalog/Evaluation/Shadow Gap and V1 Replacement Readiness
are unchanged. There is no remaining observed blocker for this scoped wording
change, not a V2 transition. Existing verified failures are retained only under the user's
no-new-failure waiver; do not describe the full suite as all green.
