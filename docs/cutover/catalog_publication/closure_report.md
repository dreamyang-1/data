# Catalog publication and pinned reads — cutover blocker progress

Date: 2026-09-08. Baseline: `3ab8b4336b290d3560756345c4328278ee1ca9b3`
(Draft PR #18). Branch: `cutover-catalog-publication-20260908-01`.

**Readiness remains NOT_READY.** This delivery implements and tests the actual
source path from captured catalog to generated rows, isolated Milvus generation,
Redis activation and verified pinned reads. It does not close live catalog,
deployment, V2 integration, Gold, Shadow or Canary gates. No runtime operations
were executed. Source implementation and real-target proof are tracked separately.

## What changed and why

Before this change, release verification accepted only supplied documents and
markers. Nothing connected those documents to the actual record builders,
complete vector-store read-back, persisted activation or query acceptance. A
model-wide capture also omitted model-owned metrics without a business domain
when the model had domains. These are C-01/C-02/C-03 cutover blockers.

The new `catalog_generation.py` uses existing semantic and physical builders,
checks their results against source record identities, groups physical tables
by their real data source, and rejects missing IDs, ownership mismatches,
collisions, inconsistent shared definitions and silent omissions. Explicit
releases contain only owned records/projections; model-wide releases also cover
shared definitions and unassigned model metrics. Orphan metric domains fail
closed. Unsupported/unowned dimension projections are recorded as exclusions,
not relabeled as authorized or claimed complete business coverage.

Each generation has independent record IDs, scope and generation filters, full
text/metadata hashes, float32 vector hashes, a target identity and an embedding
contract. `MilvusCatalogStore` uses dedicated semantic/physical collections and
rejects overlap with every normalized V1 collection name. This separation is
necessary: adding generation records to current V1 collections could contaminate
unversioned V1 retrieval. Current production collection configuration and V1
record/rebuild behavior are unchanged.

`CatalogPublication.publish()` captures authority, builds the generation,
reserves the publication ID in the existing Redis service, writes isolated rows,
reads back the complete generation with strong consistency, rechecks authority,
then uses Redis WATCH/MULTI to atomically activate the manifest and marker. No
previous rows are deleted. Reserved IDs cannot be blindly reused after failures.
Publication records and markers have no TTL. Redis persistence/eviction/replica
configuration and actual server concurrency still require target verification.

Pinned reads accept only the requested model/domain's active release and exact
target. ANN and exact reads use strong consistency; capped compatibility reads
cannot establish catalog coverage. Every returned record is checked against
the manifest and all stamps. ANN additionally requires the same query embedding
model/endpoint/dimension contract. `finish()` rechecks current authority, full
vector inventory and activation before the caller may accept or cache a plan.
The cache fingerprint includes current scope, target, release and activation.
The caller must retain its full AuthorizedSemanticScope and state identity
fingerprint, including database/knowledge-base restrictions where applicable.

`reactivate()` provides explicit index rollback only when the previous generation
still agrees with current authoritative catalog and full index contents. A new
activation ID invalidates readers and cache keys even for A → B → A. A lost Redis
commit acknowledgement reports `CATALOG_ACTIVATION_OUTCOME_UNKNOWN`; it never
asserts the previous marker must still be active. Verification resolves the
outcome before a retry. This is index activation rollback, not V1/V2 traffic
rollback or authorization to perform a live rollback.

The operator CLI `Oagnet/scripts/manage_catalog_publication.py` provides
`verify`, `initialize`, `publish`, and `reactivate`. Scope is validated before
opening a client. Verification opens an existing schema without creating,
loading or modifying collections, and reads current MySQL/index/Redis evidence.
Initialize/publish/reactivate are explicit controlled operations. Constructors,
ordinary HTTP requests and module imports do not call them. Current Agent and
Oagnet HTTP request/response/SSE formats are unchanged.

## Review and validation

Self-review followed source capture → existing builders → isolated generation →
full strong inventory → Redis CAS → pinned acceptance → rollback. No separate
agent/reviewer was used. Review specifically added safeguards for float32
storage precision, legacy collection name sanitization, bounded read timeout,
query embedding mismatch, commit-ack ambiguity and full marker compatibility
with the existing private-bundle checker. The latter still reports only
VERIFIED_SUPPLIED_EVIDENCE and never upgrades a saved bundle to live proof.

The installed pymilvus 2.6.17 source was checked for iterator kwargs and timeout
support. Native server behavior was not substituted with claims from those
signatures. Existing network-denying offline runners supplied all external
seams; no dependencies were installed and no external services were contacted.

| Service | Baseline passed / failed | Final passed / failed |
| --- | ---: | ---: |
| DataAnalysis Agent | 2258 / 27 | 2258 / 27 |
| Oagnet | 401 / 8 | 488 / 8 |
| SQL Translator | 200 / 0 | 200 / 0 |

- 87 new tests pass, including complete source generation, model-wide/explicit
  scope, model/domain changes, orphan/foreign identity, partial write, missing/
  extra/corrupt rows and vectors, source drift, competing activation, ABA,
  obsolete-catalog rollback, cache separation, embedding mismatch and CLI bounds.
- Critical existing scope/multi-turn suite: 160/160. Agent modules: all 94 in
  established serial offline batches. The classifier clock remains controlled.
- Old pass → new fail: 0. Old fail → new pass: 0. Missing prior nodes: 0.
  Final collection errors: 0. Existing 27 Agent and 8 Oagnet failures are unchanged.
- Observed clarification reason trace coverage: 89/89; not a production-wide
  measurement. Current V2 accuracy and real Shadow metrics remain unevaluated.
- All 193 Agent app/tests Python files, Oagnet API/agent/prompt/model configuration,
  embedding transport and SQL runtime remain identical to PR #18.
- The existing capture test received only the missing global-metric test double
  for the newly covered source read; no old expected behavior was weakened.
- One preliminary focused invocation named a nonexistent test module; corrected
  before valid focused/full runs. Final full Oagnet regression was repeated after
  review changes; Agent/SQL full results remain valid because their code and
  tested interfaces did not change afterward.

`test_delta.json` contains exact new/remaining nodeids, result-map and raw
offline-evidence hashes. `final_Oagnet.json` records the changed full suite.
Agent/SQL prior maps remain byte-for-byte equivalent in meaning and are linked
through `test_delta.json`; their complete suites were rerun in this delivery.

## Deliberate remaining boundaries

- Actual MySQL/Milvus/Redis target, deployed revisions, trusted caller boundary,
  durable markers, full native iterator read-back and concurrent multi-process
  publication are **not verified**. No collection/index/marker was created.
- Captured DSL/physical record coverage is not full business capability coverage.
  External entity-value snapshots, data-source routing, independent SQL metadata
  reads, owned default projections and opaque dimension rules remain explicit
  dependencies/gates. No hardcoded business fields or inferred authorization.
- The current V2 parser still consumes supplied parse/patch structures; its
  current-scope bridge and plan path are not wired to the new pinned view.
- Current V1 Oagnet query evidence and SQL caches do not carry this release.
  Their eventual execution integration must preserve public I/O and is still C-03.
- A caller of the low-level pinned view must finish verification before emitting
  or caching a plan. Enforcing this in the actual V2 pipeline is still required.
- Index generation verification is intentionally conservative and scans the full
  inventory at pin/finish. Target latency/load must be measured before deployment;
  do not replace complete verification with a sampled/top-k check for speed.

## Git and operational boundaries

Develop in `E:/YouoAgent/Oagnet`; publish versioned files only through the explicit
manifest into `E:/yy`. Preserve the original Oagnet checkout HEAD and staged
binary diff. Separate implementation and evidence commits, stacked Draft PR on
`cutover-readiness-catalog-20260908-01`; no merge, deployment, credentials, logs,
private catalog records or runtime data are included.

Real model calls: 0. Real embedding calls: 0. Live MySQL/Milvus reads: 0.
External production writes: 0. Collections created: 0. Index publications: 0.
Production V2 routing/model default/public I/O changes: NO.

Source rollback is a reviewed revert plus explicit file synchronization and
regression. No operational rollback is needed for this source-only delivery.
The new operator path is ready for review against a named approved target;
source tests do not constitute that target's authorization or acceptance.

## CUTOVER READINESS

Current Stage: Catalog generation/publication/pinned-read source integration

Cutover Blocker P0: 9 open across all classes; B-01 remains fixed from PR #18

Cutover Blocker P1: 4 open across all classes

Catalog Blocker: 4 open for cutover; C-01/C-02 source paths now implemented,
C-03 pinned contract partial, live/capability proofs outstanding

Evaluation Gap: grounded Gold, deterministic comparative metrics and real-model
benchmark absent; no model winner inferred

Shadow Gap: real plan-only V1/V2 comparison absent

V1 Replacement Readiness: **NOT_READY**

Next shortest blocking path: identify/verify the actual catalog publication target
and governed coverage using this operator path; connect V2 current-scope and
plan-only grounding to pinned acceptance; after Catalog/Phase0C gates, proceed
to Gold/evaluator and the controlled model comparison. Real Shadow and Canary
remain gated. The replacement goal is active; do not request formal V1 replacement
approval until READY_FOR_USER_APPROVAL is actually proven.
