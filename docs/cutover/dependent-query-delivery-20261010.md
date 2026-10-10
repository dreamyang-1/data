# Dependent query delivery repair — 2026-10-10

## Scope and proven first divergence

Current Stage: scoped V1 orchestration repair closed and deployed; no V2 cutover.

PROVEN: the reported salesperson-winner → product-detail chain reused the
winner table as the product result. The downstream extraction already requested
product names. The router ignored that projection, used keywords in question
and predecessor prose, and passed an internally inherited dataset as if the
user had explicitly selected it. No second SQL query was made, and the original
result-column check did not reject the mismatched delivery.

This is not evidence of missing product data, missing vectors, or a database
outage. The source business-account interpretation is outside this repair.

## Minimal changes

- Compare typed output fields, metrics, aggregation grain, time and filters
  before reusing a predecessor dataset. A missing or different entity owner
  cannot establish equivalence merely because field names match.
- Distinguish ordering dependencies from references that consume predecessor
  objects. Preserve independent denominators, local computations and joins.
- Recognize salesperson aliases and Chinese code/ID display columns; retain
  qualified identity provenance from simple executed SQL projections.
- Preserve bounded, complete, scope-checked predecessor datasets and inject
  exact predecessor values into both canonical filters and structured ASL input.
  Recheck those filters before SQL translation; names cannot replace code keys.
- Mark internal dataset inheritance separately from explicit user selection.
  Unsupported local operations may requery rather than return the old table.
- Preserve the planner's complete typed shape for fresh bound children before
  admission and execution; do not replay the parent's intent, entity, fields,
  metrics or ranking through the conversational history merger. Native tracing
  proved that the legacy merger otherwise changed DETAIL_QUERY to METRIC_QUERY
  and replaced the current object with the parent's salesperson grouping.
  The bound child's prose history is also excluded: the legacy raw-history
  recovery fallback can reconstruct the ranking even after completion is marked.
  Exact predecessor identity, current hospital/year and projections stay in the
  structured contract. Pending replies keep their existing restoration path.
- Reject a dependent result missing requested display fields; invalidate the
  same incorrect checkpoint output instead of replaying it as completed/HIGH.
  Do not pass rejected child material into combined insight synthesis.

No public API/SSE/model schema changes, semantic-library writes, index rebuild,
default model change, authorization expansion or other-service deployment.
The seven public stages and the prior empty/unavailable-dependency behavior
remain frozen.

## Verification and review

- New focused module: 30 passing tests, including the original shape and three
  similar dependent queries (products, hospitals, dealers, model attributes),
  code binding, field ownership, changed time/filter/grain and wrong-table rejection.
- Local and server Critical Suite: 294 passed.
- Initial isolated server full baseline: 4,741 passed, 93 failed, 1 collection error.
- Final incremental baseline: 4,771 passed, 93 failed, 1 collection error.
- Final full candidate: 4,771 passed, 93 failed, 1 collection error.
- Old-pass → new-fail: 0; old-fail → new-pass: 0; removed test identities: 0;
  new tests: 30 (26 + 4). No old assertions were loosened or changed.
- An intermediate overbroad dependency binding broke the frozen ordering-only
  test. Fixed the implementation, retained its assertion, added the module to
  Critical Suite and reran the comparison before release.
- Native testing of the first release exposed the inherited query-shape loss.
  Captured the actual completeness errors, added the typed-child isolation fix
  and four owner variants, and reran baseline/candidate/Critical before restart.
  Inconclusive candidate probes with unavailable isolated configuration were not
  counted as native passes.
- An isolated real-adapter replay of the original plan, using the bounded-history
  fix, passed both tasks and the completeness gate. Public API verification is
  tracked separately below; an isolated replay is not a substitute for SSE checks.
- Review: deep-copy structured inputs, retain hospital/year conditions, preserve
  dependency value bounds and scope checks, maintain explicit dataset semantics,
  reject mismatched key types/owners and avoid analysis of rejected child tables.

## Release and manifest

Runtime: `app/planning/dependency_contract.py`, `app/services/orchestrator.py`,
`app/adapters/http.py`. Tests: `tests/test_dependency_query_contract.py`.
Documentation: this report. Explicit-file synchronization to canonical Git is
SHA-256 checked; no credentials, production records or private logs included.

Deployed only the Agent to the agreed host after offline gates. Applied bounded
patches on top of the independent remote orchestrator changes; preserved the
remote planner, guards and all protected configuration/source hashes. Backup,
readiness and fresh-worker checks passed. No SQL/Oagnet restart was needed.

Native read-only public streaming API verification: original and three similar
questions all passed after the final restart. Both tasks completed in every case:

- Original hospital winner followed by product details: product name and model.
- Different hospital winner followed by product names.
- Hospital winner followed by cooperating dealer names.
- Hospital winner followed by explicitly requested product names and models.

Checked a fresh second SQL, the exact predecessor code and source dataset,
scope/ASL constraint evidence, hospital and year bounds, requested columns and
the complete seven-stage public order. Explicit model/specification output was
also present, not merely a generic product column. Final deployment receipt
confirms readiness, a fresh worker and unchanged protected source/config hashes.
Detailed receipts and raw query evidence remain private, outside Git.

## Remaining boundaries

Cutover Blocker P0/P1, Catalog/Evaluation/Shadow Gap and V1 Replacement Readiness
are unchanged by this scoped repair. The full suite is not all green; retain
the verified old failures under the user's existing no-new-failure waiver.
The scoped delivery defect has no remaining observed blocker. This is not a V2
production transition or a claim that every unsupported query is now supported.
Ambiguous/missing/incomplete upstream identity remains fail-closed; this repair
does not invent a relationship or silently drop it.
