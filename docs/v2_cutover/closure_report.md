# V2 replacement readiness — recorded Oracle diagnostics

Current Stage: RECORDED_ORACLE_AND_TEMPORAL_CANDIDATE_DIAGNOSIS.
Baseline `adb10068be9e5ed5669c5f2acf59ec2a87bf8f64` / Draft PR #46. Branch `cutover-recorded-oracle-20260909t055600z`.
Final commit: the commit containing this report. **NOT_READY** for V1 replacement.
Production acceptance groups remain 8 P0 / 4 P1, not independent-bug counts.

## What is now reproducible

The existing raw-transition tool now exposes a recorded-turn replay/Oracle seam.
It verifies the private capture hash, current case/clock/scope/catalog and frozen
source evidence. It creates a fresh in-memory publication with the recorded
evaluation activation identity, then restores the original state through native
scope/digest/binding checks. It does not rewrite the state to make restore pass.
Only this MemoryStore/FakeRedis publication uses the recorded activation; no
production registry, authentication, source publication or runtime route changes.

All 24 executed turns from PR #46 reproduce their original success or rejection.
Every successful complete result, including plans and state, equals its original
private recording. Exact replay verifies model contexts, prompts, schemas and
stage order. The typed seam deliberately rejects missing/untyped model outputs;
it cannot reconstruct the original unretained historical parser failures.

An Oracle intervention changes only explicitly supplied model outputs and runs
the real scoped binder, TaskPatch, reducer and compiler. Changed later contexts
are reported. Downstream model outputs are recorded, not regenerated; this is a
controlled contract diagnostic, not a full model Oracle accuracy measurement.

## Diagnosed failures

S81-012 current text is `把分组换成经销商`. The parse creates REMOVE/ADD markers,
while the draft expresses the expected REPLACE. Replacing the generic grouping
mention/operation evidence with the reviewed replacement parse makes the existing
dealer replacement pass. No production text rule or marker relaxation was added.

S81-014 first fails TimeSpec's IANA timezone validation. Fixing that one diagnostic
input exposes the next rejection: the model selected the sales_order subject
entity as the time field. TIME_RANGE supplied zero TIME_FIELD candidates, although
`order_count.time_caliber.time_anchor` declares `sales_order.created_date`, which
matches one captured catalog attribute. Normalizing only timezone/grain still
fails role validation. The governed anchor and deterministic time normalization
are concrete remaining contract work, not a request for an owner to invent a key.

S81-017 selects the intended historical task and correct metrics, but predicts a
grouped payload with no group_by member. A reviewed scalar-shape intervention
accepts that task and metric state. The guard against invalid grouped plans stays.
No Gold label, model winner or quality threshold was changed by these experiments.

## Validation and remaining path

This stage used zero real model calls, zero source SQL and zero production writes.
Focused: 167 passed. New tests: 12. Agent: 2914 passed / 27 historical failures;
Oagnet: 663 / 8; SQL Translator: 381 / 0. Critical 160/160, traces 89/89;
old-pass -> new-fail 0, removed tests 0, collection errors 0.
Runtime source, prompt, regex, model default, public request/response/SSE and V1
production routing are unchanged. Capture contents stay private; curated receipts
and catalog metadata alone are published through explicit manifests.

Catalog Gap: the proven metric-time-anchor candidate path and controlled source
value resolution, plus existing shape-specific identity/grain/endpoints.
Evaluation Gap: complete candidate/whole-plan/Oracle labels, model quality,
Pending/Dataset entry fixtures and holdout coverage. Shadow Gap: offline acceptance
has not passed. Native publication and Redis recovery remain production gates;
neither was re-audited or promoted to a global offline prerequisite.

Next shortest path: implement the governed temporal contract using this evidence,
then validate with recorded counterexamples, unchanged Gold and live model runs.
Continue internally; do not ask for V2 replacement approval before readiness.
Evidence: oracle_evaluation_report.json, recorded_oracle/, test_delta.json,
first_divergence_report.json and the shared readiness matrix.
