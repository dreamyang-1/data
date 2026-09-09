# V2 replacement readiness — collection replacement evidence

Current Stage: REPLACEMENT_REPRESENTATION_AND_PARTIAL_BINDING_ORACLE.
Baseline: `299513e6d0b7d02f7e7dffb68feeeba4f677f071` / Draft PR #44.
Branch: `cutover-replacement-oracle-20260909t045212z`.
Final commit: the commit containing this report. **NOT_READY** for V1 replacement.
The inherited production acceptance groups remain 8 P0 / 4 P1.

## Proven failure and change

The captured S81-002 REPLACE draft selected the current frozen catalog's
`order_count` metric with MEASURE role and the correct current mention. Its exact
handle was reconstructed against the existing frozen publication and independently
verified, without a model or source database call. The first rejecting boundary
was the collection representation required by TaskPatch, not missing retrieval.

An explicit REPLACE on an existing restored set-valued task slot may now normalize
an exact offered-handle object to a complete one-element list. A target at its
current version is required. Hydration verifies the offered handle; the original
type, role, current evidence, operation and scope validators still run. The
operation remains REPLACE, so old values are removed. Internal reason code:
`CURRENT_TURN_SINGLETON_REPLACEMENT`. No full business text is added to the trace.

This does not infer a task, change SET to REPLACE, normalize arbitrary dictionaries
or relax the final collection schema. Initial assignments still require a complete
collection. Existing SET/REPLACE rejection tests on new tasks remain unchanged.
There are no prompt, regex, schema-model, scope, public API/SSE, model/key, V1 routing
or cross-service source changes. The code change is confined to recognition.py.

## Oracle and live evidence

The partial Oracle Binding tests supply a uniquely correct offered catalog choice
and execute real scoped binding, TaskPatch, reducer, materialized state and logical
compiler. Three positive nodes compare singleton/array metric and dimension
replacement and distinguish ADD from REPLACE; nine controls retain invalid-input
guards. This is not a complete Oracle Mention/Role/Candidate/Relation suite.

Verified red run: five failures and seven passes. Three failures were positive
representation cases; two invalid cases previously stopped at generic list typing
before the retained precise role/evidence guards. An initial dimension fixture
lacked its second governed dimension; that fixture was corrected before the
verified red run. Final focused suite: **174 passed**. No old assertion changed.

The full unchanged 20-transition model run has **five observable plans, 11 failures
and four NOT_RUN fixtures**, using 48 HTTP200 calls. Metric ADD/REPLACE/REMOVE axes
are 3/3, but **S81-002 fails its turn-relation label**: the model called the correct
current-task replacement RETURN_TO_TOPIC instead of FOLLOW_UP. It is not a passed
whole case. A separate four-call repeat produced FOLLOW_UP and the same correct
metric/target; that later row does not replace the original failed observation.
The repeat emitted an array, so it does not prove singleton normalization executed
in that live call. The captured candidate and deterministic Oracle tests establish
the code effect; the live observations establish behavior and remaining variation.

Total current-stage real model requests: **52**, all HTTP200. Source SQL: 0;
production external writes: 0. Four cases reached the denied source-value boundary;
two previously reaching it instead failed earlier in model source-request linkage.
The frozen values are still absent, so this lower denial count is not improved
coverage. Pending and Dataset entry fixtures, whole-plan labels and hard-safety
observations remain incomplete. No unobserved safety is marked zero-violation PASS.

## Regression, review and remaining path

New regressions: **12**. Full Agent: **2874 passed / 27 historical failures**;
Oagnet: **663 / 8**; SQL Translator: **381 / 0**. Old-pass -> new-fail 0;
removed tests 0; collection errors 0; Critical 160/160; clarification traces 89/89.
All started evaluation and regression processes are terminal before release.

Self-review checked scoped current target/version, exact handle-only shape,
strict hydration before normalization, retained role/evidence checks, explicit
REPLACE semantics and complete list validation before state mutation. Private
captures stay outside Git; only curated candidate metadata/hash and evaluation
receipts are published. Explicit manifests compare normalized source hashes and
preserve unrelated files and the original Oagnet HEAD/index.

Catalog Gap: shape-specific identity/grain/endpoints and native production
publication. Evaluation Gap: model turn variation, operation/shape failures,
frozen entity-value observations, full Pending/Dataset entry and complete
Oracle/candidate/whole-plan labels. Shadow Gap: required offline acceptance has
not passed. Redis recovery and native publication remain production-stage gates.

Next shortest path: complete the missing frozen source-value/observation evidence
and diagnose the remaining operation/query-shape errors without guessed repair.
Continue controlled model comparison once the evaluator supports those labels;
keep V1 production until explicit user approval after all readiness gates pass.

Evidence: replacement_oracle/comparison.json, oracle_evaluation_report.json,
first_divergence_report.json, test_delta.json and the shared readiness matrix.
Earlier Gold, catalog, identity and Redis evidence remains at its original paths.
