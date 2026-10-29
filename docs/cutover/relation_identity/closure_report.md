# Relation-list identity evidence

Baseline `011a7193f5eafda1101255209129b4d4e2ad4f94` (Draft PR #36). Branch `cutover-relation-identity-evidence-20260909-01`.
**Audit completed; relationship execution and V2 replacement remain blocked.**

The next intended backend change was lowering declared relation paths to SQL.
The audit found a prerequisite that cannot be determined safely from current
metadata: the identity of the distinct target entity in a relation list.

PROVEN: ResultContractCompiler.compile adds a TARGET_ENTITY output and requires
DISTINCT_TARGETS cardinality. A semantic entity-type ID identifies a definition,
not an individual source entity. Deduplicating display names or arbitrary complete
rows cannot be substituted for that contract. The current ASL2 lowerer therefore
continues rejecting RELATION_LIST, with no behavior or public-format change.

## Authoritative observations

Scope: semantic model 81, explicit domain [205]. Fresh snapshot capture:
`2026-09-08T19:35:05.535876+00:00`. Catalog version:
`3f9589b95d376ebdf9683425dcf6c646f2394b278718b033527bd5e08b64fe21` (unchanged from PR #36).

All **14 entities / 89 attributes** lack governed identity declarations:
entity.primary_key is null, and every attribute's is_primary_key/is_unique is false.
A separate read-only catalog transaction at `2026-09-08T19:36:19.142402+00:00` confirms
all 89 raw flags are numeric zero. This is not a Boolean conversion defect.

The actual authoritative schema contains is_primary_key/is_unique on
semantic_model_attribute_config, but semantic_model_entity_type has no primary-key
definition column; semantic_model_table/semantic_model_field do not preserve physical
key constraints. Thus there is no omitted physical-key column that the current
snapshot builder could simply forward. No source-database schema or business rows
were read. Even a physical row key would still need an entity-identity contract.
The two timestamped reads are not presented as a single atomic observation.

## Required owner decision and implementation boundary

The business/catalog owner must define the identity field or field tuple for each
entity needed by the approved relation-list scenarios. Clarify entity identity
versus physical row identity, composite-key membership and null/equality semantics
where relevant, then represent the approved rule in the governed catalog.
The user has been asked for that rule. No field is preselected in the private
owner worksheet. No catalog write or business-rule inference is authorized by it.

After that decision, relation lowering must preserve:

- Exact declared path, direction and every repeated entity occurrence; never
  silently replace a chosen path with the SQL translator's shortest path.
- Field ownership of every filter and projection occurrence, plus current scope,
  same-source membership and unchanged catalog pin through SQL acceptance.
- A proven target identity output, correct duplicate handling and completeness
  before applying a display limit; same names alone must not collapse identities.
- Existing parameter, output and result receipts; missing keys or incomplete
  relationship declarations must fail closed with a bounded system reason.

Tests must include duplicate target paths, two identities sharing a display name,
multiple rows for one entity, reverse/self paths, source/target filters, nullable
keys, key-definition drift and missing path receipts. Non-target projections that
can vary within one target require an explicit contract, not arbitrary first-row
selection. No implementation is claimed for these still-open cases.

## Validation and delivery

The standalone audit_snapshot.py uses only the standard library and a supplied
private sealed snapshot. It writes hashes/counts, verifies digest and scope, does
not mutate the snapshot and never treats a present key declaration as automatic
identity/cutover proof. Six direct checks passed, including digest/scope rejection,
string-zero handling and declaration-presence versus proof separation.

This delivery changes audit documents/tooling only. No Agent/Oagnet/SQL runtime
source, public API/SSE format, production model, V1 or V2 routing changed. Full
regression was not rerun: the verified PR #36 runtime baseline remains Agent
2697 passed / 27 failed, Oagnet 663 / 8, SQL 381 / 0; Critical 160/160, trace 89/89.
Those are prior results, not newly measured results. Runtime source equality is
checked during explicit-file sync and final committed-blob verification.

Real model calls, embedding-service calls, source business reads and production
writes: 0. No native publication, index rebuild, Redis change, merge or deployment.
Raw metadata and the private owner worksheet are excluded from Git. Review is
self-review; missing business identity remains UNKNOWN_NEEDS_OWNER_REVIEW.

## Cutover readiness

Current Stage: relation-list identity prerequisite audit.
Cutover Blocker P0: 8 open. Cutover Blocker P1: 4 open (existing C-03/S-01 refined).
Catalog Blocker: 14 entities without identity declarations; two relationship
endpoints and two metric subjects unresolved; native publication, registry recovery
and deployed trust still unproven. Source timezone still awaits owner evidence.
Evaluation Gap: Gold/model comparison gated. Shadow Gap: no real plan-only shadow.
V1 Replacement Readiness: **NOT_READY**.
Next shortest blocking path: obtain the governed identity rule and Redis deployment/
recovery evidence plus the pending publication authorization. Implement dependent
relation lowering after identity is known; verify actual native publication before
advancing evaluation. This evidence commit does not close a cutover blocker.
