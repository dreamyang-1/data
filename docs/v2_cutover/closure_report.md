# V2 replacement readiness — frozen source evidence

Current Stage: FROZEN_SOURCE_EVIDENCE_AND_COMPLETE_PRIVATE_TURN_CAPTURE.
Baseline `9696077296b29ee8eb9d381c60e376053d4cf035` / Draft PR #45. Branch `cutover-source-evidence-20260909t052100z`.
Final commit: the commit containing this report. **NOT_READY** for V1 replacement.
The inherited production acceptance groups remain 8 P0 / 4 P1; these are groups,
not a count of independent bugs. V1 production and all public request/response/SSE
formats are unchanged. No runtime source, model default, prompt, regex or external
service source was modified.

## Evidence and correction

The existing opt-in transition runner now captures both successful and failed
executed turns privately: exact model request bodies without headers, raw content,
typed outputs, actual before/after scoped state, plans, clock, catalog and source
fixture identity. Public receipts retain only bounded observations and hashes.
The run captured 24/24 executed turns, with all hashes verified.
Unexecuted history/current/Pending/Dataset turns are not fabricated.

The evaluator previously hashed only the state payload. Native ScopedArtifact
includes source-value binding proofs when present, so a legitimate source-backed
plan was rejected as corrupt. The adapter now uses existing
`scoped_artifact_material`; corrupt payloads and missing proofs remain rejected.
This is an evaluator fix, not relaxed planner validation.

Frozen source observations require an explicit expected hash, exact catalog file
hash/version/scope/mapping/query/limit and valid native observation hashes. Live
model evaluation rejects SYNTHETIC_TEST provenance. The local capture is trusted
evaluation input; its hash is not a signature or proof of current production data.
Native pin, implicit-field policy, binding, state restore and finish checks execute
against the frozen fixture; absent queries cannot fall through to real SQL.

Separate authorized read-only evidence comprises six exact source SELECTs and
three controlled probe SELECTs, with current catalog checks, timeouts, limits and
rollback. The short Gold mentions have no exact rows in the two selected region
fields. A 34-value province probe verifies full administrative forms privately.
The city field has 396 distinct values, exceeds the 64-value budget, and was not
enumerated. Raw source values, source locators and credentials are not published.
These observations do not authorize automatic suffix aliases, identity by name,
different fields or a wider scope. Existing Gold remains unchanged.

## Model and regression results

The same 20-case model run used 48 actual HTTP200 requests: {'OK': 5, 'FAILED': 11, 'NOT_RUN': 4}.
These statuses count observable plans, not whole-case passes. Required unobserved
safety and whole-plan metrics remain NOT_EVALUATED. Exact empty source results
now surface as source-value misses, distinct from missing evaluation fixtures.
S81-002's private capture proves a HISTORICAL parse signal and a selected historical
handle despite the expected ordinary replacement; no silent relation repair was
introduced. See the evaluation details for every observed axis and failure.

Model evaluation executed zero source SQL and zero production writes. The separate
read-only audit executed nine source SELECTs; this total must not be reported as
zero. No production publication, Redis mutation, V2 routing or user answer changed.

Focused regression: 112 passed. New tests: 28. Agent: 2902 passed /
27 historical failures; Oagnet: 663 / 8; SQL Translator: 381 / 0.
Old-pass -> new-fail 0; removed tests 0; collection errors 0. Critical 160/160;
clarification trace 89/89. Review found that the file entry could classify a missing
fixture differently from the imported module because of duplicated exception class
identity. Both entries are now tested using a shared exception. The completed live
run had zero missing-fixture lookups, so its observations are unaffected by this
subsequent diagnostic correction. Focused and full service suites were rerun after
the correction and all final adversarial tests were added.

## Remaining shortest path

Catalog Gap: resolve actual short-name source-value misses under a bounded governed
probe/selection contract; identity/grain/endpoints remain shape-specific. Native
publication and Redis recovery remain production gates only.
Evaluation Gap: complete Oracle/candidate/whole-plan labels and Pending/Dataset
entry coverage; diagnose model operation, relation and shape failures using the
new private full-turn observations. No quality threshold was lowered and no model
winner was selected. Shadow Gap: offline acceptance still has not passed.

Continue the internal evaluation path; no user cutover approval is requested yet.
No Identity or Redis re-audit, duplicate Gold, runner or production authority was
introduced. Review verified manifest-only synchronization, preserved unrelated
Git changes and private-data exclusion. The complete tracked-source equality and
clean Git gates are recorded in the delivery manifests.
