# Pinned relationship endpoint ownership

Baseline `2881cd7e52a7d8ca21bbb66e808746bd4566e137` (Draft PR #35). Branch `cutover-pinned-relation-endpoints-20260909-01`.
**Scoped endpoint resolution: verified offline. V2 replacement: NOT_READY.**

PROVEN first divergence: SQL_TRANSLATOR, `_SnapshotLoader` relationship adaptation.
The private pinned consumer accepted any globally registered qualified endpoint,
even when it belonged to a different entity, and rejected all unqualified fields.
It now resolves a declared column only among the declaring entity's base table and
explicitly governed sub-tables in the current snapshot. Exactly one physical match
is required. Qualified endpoints must belong to that same owner. Both owners must
use the same data source. Missing, ambiguous or mismatched endpoints remain outside
executable path search; unrelated single-table plans still work.

This uses an existing declared relationship and column, not an inferred bridge.
No base-table preference, global column search, attribute-name fallback, LLM or
catalog list order can resolve a tie. The snapshot and current request scope are
unchanged; final pin validation still precedes SQL acceptance.

## Current 81 / [205] evidence

Authoritative metadata recaptured at `2026-09-08T19:23:26.131579+00:00`.
Catalog version `3f9589b95d376ebdf9683425dcf6c646f2394b278718b033527bd5e08b64fe21` is unchanged. All 89 governed value
field mappings and 290 generated static records pass the existing local checks.

Of 24 relationship declarations:

| Outcome | Count |
| --- | ---: |
| Previously resolved and still accepted | 17 |
| Previously unqualified, now uniquely resolved | 5 |
| Still ambiguous across owned base/sub-table fields | 1 |
| Previously accepted, now rejected for endpoint ownership mismatch | 1 |

Unresolved declarations change from **6 to 2**, not zero. The ambiguous source has
two registered physical matches. The mismatched declaration names a target entity
but points its target key at a bridge owned by the source entity; the missing bridge
to target endpoint cannot be manufactured. Both need governed catalog evidence.
Only hashes/counts are published; private metadata and source connection facts stay local.

The current metadata V2 plan replay remains **55 supported / 61 plans**. Six plans
still lack a proven primary subject for two multi-entity metrics. Relationship
payload lowering remains unsupported, so this change does not claim complete V2
relationship execution or better natural-language accuracy.

## Validation and review

| Service | Baseline passed / failed | Final passed / failed |
| --- | --- | --- |
| Agent | 2697 / 27 | 2697 / 27 |
| Oagnet | 663 / 8 | 663 / 8 |
| SQL Translator | 354 / 0 | 381 / 0 |

27 new SQL regressions pass. Synthetic SQLite execution checks correct joined rows
for qualified, unqualified and mixed endpoints. Contrasts cover wrong owners,
undeclared fields, source-owned bridges presented as targets, multiple physical
matches, list-order reversal, entity ID/code normalization, data-source mismatch,
current-scope mismatch and final pin drift. The pinned source snapshot is unchanged.
Critical 160/160, clarification trace 89/89, old-pass to new-fail 0, collection errors 0.
Every previous Agent/Oagnet/SQL nodeid is unchanged; no old assertion was edited.
Review is self-review, including the actual source-owned bridge contrast added
after metadata replay. Full SQL rerun after that final test: 381 passed.

Prompt changes / new Regex patterns: 0. Public API/SSE/ASL formats: unchanged.
V1 routing / production V2 routing: unchanged. Real model calls, embedding service
calls, business source reads, external SQL execution and production writes: 0.
Authoritative catalog metadata reads only; no native publication or Redis changes.

## Cutover readiness

Current Stage: current-scope catalog endpoint ownership and resolution.
Cutover Blocker P0: 8 open. Cutover Blocker P1: 4 open.
Catalog Blocker: native publication, durable registry recovery and deployed trust
remain unproven; two relationship declarations and two metric subjects remain open.
Source timezone still needs owner evidence. Evaluation Gap: Gold/benchmark gated.
Shadow Gap: no actual plan-only shadow. V1 Replacement Readiness: **NOT_READY**.
Next shortest blocking path: obtain Redis deployment/recovery evidence and explicit
authorization for the reviewed 81/[205] publication, then verify native read-back.
Resolve governed relationship/subject/time facts before extending dependent lowering.
No automatic merge, deployment, production takeover or V1 replacement.
