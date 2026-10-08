# ASL advisory matching: continue with explicit notices

## User decision and root cause (PROVEN)

Advisory structured extraction and the completed question drive ASL. Equal-rank
candidate ties should not block execution: randomly choose one and disclose the
choice. Unmatched advisory mentions may be omitted, but must be disclosed.
This does not relax trusted scope, confirmed bindings, time constraints or SQL
execution safety. It does not label every similarity tie a data error.

The existing selector already randomly chose ties. It returned only a pair, so
the competing candidates and scores were lost. generate_surface_asl also dropped
the existing asl_repair records, preventing final result disclosure.

## Implementation manifest

Oagnet:
- agent.py: deduplicate equivalent top candidates before selection; record all
  remaining top-tie candidates, selected field/value, string-similarity score,
  selection policy and duplicate count in existing asl_repair.
- api.py: forward existing Idempotency-Key header internally. Selection uses a
  request/model/domain-scoped seed with sorted candidates. Same key and same
  eligible candidates yield the same selection across transport retries.
- agent.py: unmatched LIKE drafts are removed as well as equality drafts;
  retire only the handled advisory value's stale filter ambiguity, preserving
  unrelated ambiguity and time/metric/relationship constraints.
- tests/test_surface_binding_notices.py: six new regressions.

DataAnalysis:
- app/adapters/surface_asl.py: retain asl_repair in the internal plan.
- app/adapters/asl_notices.py: render disclosures; attach one grouped audit
  record through existing execution_transforms (no new public root schema).
- app/adapters/http.py: display notices in the existing ASL stage and preserve
  them with the successful query result. No new lifecycle stage or SQL changes.
- app/services/orchestrator.py: carry disclosure evidence into reliability
  warnings and final answer, even if answer synthesis omits it. Warnings do not
  reject a successful result; they prevent declaring the selection verified.
- tests/test_asl_binding_notices.py: three new tests including completed query,
  final-answer warning, and validation-before-insight order.

Only these code/test files and this report are included. Existing development
files diverge from the release repository; equivalent minimal patches were
applied without copying unrelated code over the canonical repository. Matching
new files and the orchestrator were SHA-256 compared.

## Verification / delta

- Oagnet: 764 existing + 6 new = 770 passed; no existing assertion changed.
- DataAnalysis full baseline at 4a929ee in detached worktree:
  4135 passed, 52 failed, no collection errors.
- DataAnalysis full modified run: 4138 passed, 52 failed, no collection errors.
- Failure identities compared using pytest's lastfailed sets: identical 52;
  old-pass -> new-fail = 0; old-fail -> new-pass = 0; new passing tests = 3.
- Focused surface/critical/API/stage-order suite after final wording edits:
  93 passed. End-to-end mocked result remains COMPLETED with concrete notices.
- Full baseline failures include existing lifecycle-event omissions, legacy
  offline model stubs lacking agent_prompt, and existing pending/region/product
  contracts. No fixes or assertion changes to those unrelated paths.

## Review and boundaries

Same-task selection reproducibility assumes the candidate set and algorithm
remain the same; this is not a full persisted ASL snapshot or protection from
catalog changes during a retry. Calls without an idempotency key keep existing
random behavior. Downstream consumes the selected ASL without selecting again.

The surface-advisory route already bypasses legacy caller-contract rechecks;
those checks remain for user-confirmed and dependency-bound plans. A missing
executable field, formula or relation can still fail. No production data writes,
SQL Translator changes, index rebuild or deployment were performed.

Current stage: scoped V1 advisory-matching/disclosure change. Not a V2 cutover;
known baseline failures remain and production readiness is not asserted.
