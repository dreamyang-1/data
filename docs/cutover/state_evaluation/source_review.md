# Multi-turn evaluator source review

Baseline: `66c11a1bfa414ba63b4592cb5ff3713492a5b324` / Draft PR #39.
The next cutover gap is E-01: current-turn axis scoring does not establish
materialized multi-turn state, selected task or Pending correctness. This change
adds offline tooling and evidence; production services, routing and public I/O
are not modified.

## Ground truth and observation independence

Twenty transitions use the user's explicit ADD/REPLACE/REMOVE/CLEAR, new-topic,
history, lineage, Pending and Dataset rules and exact frozen catalog names.
Expected answers are written in `build_transition_gold.py`, independently of
model output. The existing 100 axis rows stay byte-for-byte unchanged. The total
120 record inventory overlaps business families; it is not 120 independent real
user failures. No record is labeled COMPLETE.

The first development review found that combining operations from every slot
misclassifies an incidental time ADD as a failure of a metric REMOVE. Labels and
observations now identify metric, dimension and region operations separately.
The intended business operation and expected materialized state did not change.
Remaining discrepancies are retained, including a region trace that says ADD
while its materialized replacement is correct. Full TaskPatch equivalence is
still unlabelled; per-slot evidence does not imply equivalence of all edits.

`run_transition_baseline` removes labels before invoking observation code.
`observe_legacy_rules` calls the existing classifier, admission, merge and
explicit-slot protection services. It does not call the rewriter, model,
historical selector, catalog grounding or full orchestrator. New/prior task
observations come from the admission decision; the harness assigns local handles
without selecting the desired historical task. Its canonical metric ID remains
unobserved. Required missing slots are not presented as observed public questions.
Three Dataset controls call the actual local follow-up planning service using
two explicitly synthetic rows and declared completeness.

The V2 adapter reads actual `RawTurnPlanner` next-state, active task version,
compiled patch, canonical refs or typed clarification decision. It rejects
scope/model/domain/database/knowledge-base mismatch, catalog hash/version
mismatch and an invalid state digest. It keeps ASK and ANSWER_PENDING distinct.
Its integration tests run the real scoped planner with scripted model responses
and synthetic catalogs. They test observation fidelity, not current model quality
on the 81/205 production catalog. The adapter cannot create new observations by
copying Gold answers.

## Evaluator safety

Every labeled denominator is fixed. Missing, failed and NOT_RUN observations
are unobserved failures, not deleted cases. Collections compare without ordering
but preserve duplicates; JSON types remain distinct. Unknown/duplicate cases,
mixed components or modes and mismatched scope/catalog receipts are rejected.

All seven required safety categories are visible. No observations means
NOT_EVALUATED, never a zero-risk PASS. An explicitly observed safety violation
fails even if that particular case originally omitted the safety label. Perfect
semantic scores cannot override that failure. The evaluator never issues a
production-cutover PASS from component evidence.

The offline runner denies socket connection methods and fixes the existing
classifier date per case. It runs sequentially because monkeypatching a module
clock is process-wide. Patches restore on exit. No new dependency, prompt rule,
regex, authorization inference, source query or production write is introduced.
The user-requested model retry is a separate receipt, outside offline tests.

The new tests cover destructive ADD contrasts, duplicates, wrong task/canonical
binding, missing evidence, wrong types, cross-scope receipts, mode contamination,
unlabeled safety violations, network denial and actual V1/V2 service outputs.
Existing production regressions and original Oagnet Git state are preserved.
