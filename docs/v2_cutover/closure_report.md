# V2 replacement readiness — deterministic temporal authority

Current Stage: DETERMINISTIC_TEMPORAL_EDIT_AND_COMPARISON_AUTHORITY.
Baseline `42547ca85fcc9a2ad6f83260df444ada3ea81ddd` / Draft PR #49. Branch `cutover-time-components-20260909t070300z`.
Final commit: commit containing this report. V1 Replacement Readiness: **NOT_READY**.
Acceptance groups remain 8 P0 / 4 P1; not independent-bug counts.

## Verified defects and behavior

Executed counterexamples proved that a current 2026 range could accept model
1990 dates, a quarter edit could accept YEAR, and a literal March 2020 comparison
could accept a 1990 baseline. First divergence was typed temporal lowering and
the explicit comparison baseline path, not missing user information or transport.

RANGE/GRAIN edits now derive values from whole current expressions and the
existing business clock. Model values still pass original structural/authority
guards before normalization. Initial legacy TIME_FIELD whole-expression inputs
also derive their date and grain; an actual field name alone cannot authorize an
invented date window. Explicit comparison baselines use current calendar/literal
evidence; relative comparison arithmetic remains deterministic existing code.

CLEAR, anchor binding, current Scope, task versions and comparison dependencies
remain enforced. No new calendar/default/watermark policy is inferred. Unsupported
grammar and conflicting evidence fail closed. The legacy time-field selection
path retains its existing scoped candidate selection; this is not a claim that
every role or anchor choice now meets semantic whole-plan acceptance.

## Validation and review

Focused 198 passed; 40 new tests. Agent 3038 passed / 27 historical failures;
Oagnet 663 / 8; SQL Translator 381 / 0. Critical 160/160, clarification trace
89/89; old-pass -> new-fail 0, removed tests 0, collection errors 0. All 25 recorded
turns from PR #49 reproduce their entire original result or original rejection.
No additional model request was needed; this is recorded pipeline evidence.

Self-review caught three old malformed-input rejection codes changing when the
normalizer ran before structural validation. Original validation order was
restored; no old assertion was changed. Comparisons and legacy initial dates were
audited as part of the same date-authority defect, closing those bypasses too.

Real model calls 0; source SQL 0; production writes 0. Prompt/regex patterns,
public API/SSE, model/key and V1 production routing unchanged. Finite assignment
and comparison grammar with positive/negative controls is documented in
temporal_authority/contract.json. No native publication or Redis changes.

## Remaining shortest path

Cutover P0: semantic quality and complete safety acceptance remain open. The last
live 20-case sample is still PR #49: 6 observable plans, 10 failures, 4 NOT_RUN;
do not promote recorded replay or these new tests to full model accuracy.
Catalog Gap: scoped source values/aliases and shape-specific identity, grain and
relation endpoints. Evaluation Gap: region observation, Pending/Dataset entries,
whole-plan/Oracle/holdout labels and formal available-model comparison. Shadow Gap:
offline acceptance remains incomplete. Native publication and Redis recovery only
constrain their production gates and were not re-audited.

Next: resolve the proven scoped source-value gap using existing catalog facts and
bounded read-only evidence, then continue unchanged Gold/evaluation. Do not lower
acceptance, claim production readiness or ask for replacement approval yet.
