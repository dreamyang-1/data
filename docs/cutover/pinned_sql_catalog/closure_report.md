# Pinned catalog SQL planning acceptance

Baseline: `b0c2d9da2a836b9e16b9986c90d1e3b2065026f0` (Draft PR #30). Branch: `cutover-pinned-sql-catalog-20260909-01`.
Scope selected by the user: **81 / [205]**, for metadata-only validation.

**Internal SQL catalog seam: PASS offline. V1 replacement: NOT_READY.**

Oagnet now captures entity base tables, source IDs, declared sub-table mappings,
metric dependencies and registered sub-table fields inside the existing single
read-only catalog transaction. These private facts affect catalog_version.
The public DSL and physical catalog endpoint retain their existing shapes.

The new internal SQL planning function consumes a trusted live publication pin
and the current request scope. It reuses the real ASL 2.0 SQL generator with
snapshot-backed metadata, refuses independent Redis/MySQL fallback, checks
referenced source/table membership and required joins, and withholds SQL unless
the same pin finishes successfully. Execution methods are disabled. It is not
registered as an HTTP route or wired into V1/V2 production routing.

## Actual metadata evidence

Fresh catalog version: `3f9589b95d376ebdf9683425dcf6c646f2394b278718b033527bd5e08b64fe21`. The capture includes
14 entities, 11 metrics, 89 verified attribute/source mappings and two explicitly
registered sub-tables omitted by the legacy base-table-only physical projection.
It still builds 290 static catalog records. No native embeddings/index writes occurred.

The actual metadata was replayed offline through the real publication/pin and
SQL implementations using in-memory storage and fixed fixture vectors:
**14 detail + 11 metric plans = 25/25 accepted**. No generated SQL was executed.
This proves metadata compatibility for those plans, not result correctness,
Gold labels, model quality or native publication. Raw catalog and SQL stay local.

Six relationship declarations contain 12 unqualified endpoints. They cannot
authorize executable joins; unrelated single-table plans remain usable. Their
fully qualified endpoint ownership needs governed catalog evidence. No endpoint
was guessed, no join was inferred by matching column names, and no catalog data
was rewritten. Existing blocker C-03/S-01 retains this limitation.

## Verification

| Service | Baseline passed / failed | Final passed / failed |
| --- | --- | --- |
| DataAnalysis Agent | 2615 / 27 | 2615 / 27 |
| Oagnet | 649 / 8 | 663 / 8 |
| SQL Translator | 200 / 0 | 251 / 0 |

New passing tests: **65** (14 Oagnet, 51 SQL). Critical: **160/160**.
Clarification trace: **89/89**. Old-pass to new-fail: **0**. Collection errors: **0**.
Exact node IDs/delta and complete final outcomes are retained in this folder.
Existing test doubles were extended for the added metadata reads; no old
expectation was relaxed and no non-blocking historical failure was patched.

Self-review added real cross-service pin acceptance tests, catalog/source/scope
mismatch contrasts, missing joins, dependency cycles, declared relationship
normalization and execution refusal. It also found and fixed three integration
issues: structured relationship endpoints, numeric metric-level dispatch and
missing physical sub-table capture. No independent reviewer is claimed.

Public input/output changed: NO. Production V2 routing changed: NO.
Prompt changes: 0. New Regex patterns: 0. Real model calls: 0.
Business source reads/SQL executions/production writes: 0.

## Cutover readiness

Current Stage: catalog version propagation into internal plan-only SQL.
Cutover Blocker P0: **8 open**. Cutover Blocker P1: **4 open**.
Catalog Blocker: native publication absent in the previous timestamped read;
Redis durability and deployed caller/reader acceptance remain unproven.
Current discovery confirms the configured Redis target is not this development
host, so local Docker configuration cannot establish its storage topology.
Evaluation Gap: Gold and real-model benchmark remain gated.
Shadow Gap: actual plan-only shadow has not run.
V1 Replacement Readiness: **NOT_READY**.

Next shortest blocking path: establish Redis deployment/storage ownership and
recovery evidence, authorize and run the refreshed guarded native publication,
then verify full native read-back and deployed pin acceptance. Source work still
needs a loss-aware V2 TypedPlan -> actual ASL 2.0 lowering with ResultContract
binding; this SQL consumer does not close that seam or claim all query semantics.
Do not mark the frozen ASL 1.0 adapter compatible merely by changing its label.

The current reviewable publication candidate is `publication_candidate.md`.
Its commands have not been executed. Selection of 81/205 is not a production
write permission or system default. V1 remains in control.
