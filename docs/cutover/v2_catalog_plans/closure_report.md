# V2 governed detail and declared relationship plans

Date: 2026-09-08. Baseline `5583d8c48db49e22d236e2ee543b26dc89fa3264`
(Draft PR #24). Branch `cutover-v2-catalog-plans-20260908-01`.

**Source acceptance PASS; V1 replacement readiness NOT_READY.** Current-request
scope, public HTTP/SSE, V1 routing and production model defaults are preserved.

## Proven root causes and implemented path

Oagnet selects `r.type` in its current-model/domain relation query but discarded
it in `_row_to_relation_dict`. SQL Translator reads that same declaration as
`relation_type` to determine cardinality. Oagnet now preserves it in DSL and
generated catalog metadata. No relationship type, field or business value is
inferred or written back to MySQL. The existing join-field normalization is
unchanged. This is a cross-service source fix, separately committed.

Raw V2 previously required explicit projections for detail plans, without a
catalog default path, and could not materialize its existing RelationshipSpec.
The new catalog grounder selects all current owned attributes explicitly marked
`is_main_attribute: true`, checks their catalog ownership and mapping, and emits
a stable ordered ProjectionSpec. Existing Oagnet source explicitly defines that
flag as default presentation governance. The policy ID is computed from that
governed rule, pin identity and exact entity/attribute record hashes; it is not
an authorization grant or an LLM-generated policy. Missing/inconsistent facts
produce bounded catalog reasons, never a generic request for metric/fields.

Explicit fields remain explicit. Default projections are rebound after a
subject or relationship-target change. Explicit CLEAR stays a barrier. Pending
subject confirmation completes its default projection through the existing
state mutation before the plan is accepted. At compile time the default is
rederived: a forged policy ID, wrong owner or incomplete field list rejects.

RelationshipEditDraft accepts only a current offered relation handle, operation,
direction and current mention evidence. Endpoints, joins and cardinality come
from the pinned record. Reversing a declared edge swaps endpoints and inverts
one-to-many/many-to-one. The model cannot provide cardinality, canonical endpoint
IDs, joins or a whole relationship object. Same-named attribute candidates show
their actual owner; dimension-enum parents are not mislabeled as entities.
The compiler checks the relationship against its current catalog declaration,
rejects contradictory subjects/endpoints and attributes outside the declared
entities, and requires the existing full catalog acceptance before returning.

This implements the existing single RelationshipSpec contract. It does not
claim support for multi-hop or alias-sensitive/self relation semantics; those
need a properly scoped path/alias contract. Model interpretation of which edge
and direction a user meant remains an evaluation requirement. New source
functionality is not evidence that every business relationship is covered.

## Actual 81 / 205 catalog evidence

A fresh read-only authority capture contains 14 entities, each with one main
display attribute, and 24 relations with endpoints, join fields and declared
cardinalities: 11 ONE_TO_MANY, 13 MANY_TO_ONE. Local complete generation still
contains 290 records. The catalog content identity is now
`c054d927062b4728fe40d28efa87bd45336f230f62282c257b4c13233a664d92`.

Using that actual capture with offline publication doubles and blocked network,
all 14 default-detail plans and 24 forward + 24 reverse relation plans passed
typed binding, compiler, result-contract and pin-finish validation. This lab
does not involve model interpretation or native index writes. Its first attempt
used an older test helper that supplies UNRESOLVED for non-metric payloads and
was correctly rejected; the completed lab supplies pinned candidate evidence
through the current RawTurnPlanner resolution builder. It remains typed source
acceptance, not recognition accuracy.

Raw catalog data remains outside Git. The report stores counts, hashes and
bounded receipts only. MySQL was read-only. Real model calls, real embeddings,
native collection/marker changes, production state writes and deployment: zero.
PR20's older catalog version is now historical; its publication candidate must
be regenerated and revalidated before any approved native publication. The user
selected 81 / 205 as the validation scope, not as a request default or write grant.

## Validation and review

32 new Agent regressions and five Oagnet declaration regressions pass. They
cover governed/explicit/multiple defaults, subject/target replacement, exact
policy validation, Pending completion, direct/reverse relationship facts,
unsupported declarations, evidence forgery, unrelated attributes, schema/role
conflicts, clear barriers, RESET preservation and enum/attribute ownership-label contrasts.

| Service | Baseline passed / failed | Final passed / failed |
| --- | --- | --- |
| DataAnalysis Agent | 2452 / 27 | 2484 / 27 |
| Oagnet | 555 / 8 | 560 / 8 |
| SQL Translator | 200 / 0 | 200 / 0 |

Full Agent coverage spans 99 modules and returned 2482/27. Final schema/owner
review removed direct relationship writes from the generic edit schema and
corrected enum owner labeling and preserved TaskPatch RESET while adding defaults,
with two new contrasts. All 466 affected tests
then passed. Final counts replace the nine complete module results, not summed
repeated runs. This excludes the prior ID of a newly added negative case whose
expected rejection moved to the model-schema boundary; all baseline nodes remain.
Critical Suite: 160/160. Existing public clarification trace: 89/89. Missing
prior nodes, old pass to new fail and collection errors: zero. No old expectation
changes. Two new negative assertions were aligned with the actual earlier
model-schema/attribute-ownership rejection stage.

The synthetic raw-turn tests run actual model HTTP seams, catalog/reducer/state
and compiler code with mock responses. They do not measure actual model quality.
The separate catalog lab uses actual captured facts, but fake publication stores.
Full node reports, source hashes and review findings accompany this report.

## Prompt and contract review

Only the V2 draft instruction changes, version `v2-current-recognition-v4`.
Deterministic code now supplies governed defaults and declared relation facts;
the prompt describes those typed handles and owner labels. The model still
interprets user language, but cannot fabricate missing catalog facts. Before:
required explicit fields/no relation builder. After: governed construction with
contrasts rejecting missing facts, guessed cardinality and wrong owners.
Parse prompt, V1 prompts and frozen model/plan schemas remain unchanged. The new
internal draft schema reflects the interface; the unchanged value schema v2 is reused. Regex additions:
zero; cardinality spellings reuse the existing SQL Translator contract.

## Cutover readiness

Current Stage: Phase0C catalog/semantic source-path acceptance.
Cutover Blocker P0: 8 open. Cutover Blocker P1: 4 open.
Catalog Blocker: native isolated publication/activation still awaits the
existing operational approval, with a newly prepared candidate required.
External entity-value/routing and deployed capability evidence remain open.
Evaluation Gap: governed Gold and real V1/V2/model measurements unaccepted.
Shadow Gap: no actual plan-only traffic comparison.
V1 Replacement Readiness: **NOT_READY**.

Next shortest blocking path: scoped multi-hop/alias relationship representation,
dependent temporal comparisons, external entity values and remaining analytical/
decision construction; actual catalog publication and acceptance after approval.
Then the required Gold, real-model benchmark, shadow and controlled canary gates.
No Phase0C closure, production readiness or V1 replacement is claimed.

## Release and rollback

Explicit source/evidence manifests synchronize development to E:/yy, with byte
normalization, SHA-256 and committed-blob checks. The original Oagnet checkout
HEAD/staged user changes are preserved. Oagnet and Agent source commits are
separate, followed by closure evidence. The Draft PR stacks on PR #24; no merge.
Rollback and final commit manifests document the exact boundary and checks.
