# Catalog 81 / 205: governed identity and copied-model isolation

Date: 2026-09-08. Baseline: `a1708a796c9eb103309bc189280a94fb51b0ebfd`
(Draft PR #19). Branch: `cutover-catalog-identity-20260908-01`.

**Source closure PASS; V1 replacement readiness NOT_READY.** The user selected
the current configured environment with model 81 and explicit domain 205 for
read-only acceptance. This is an acceptance target, not a ChatRequest default
or permission to create collections or publish an index.

## Proven failures and changes

Both new failures first diverge in `OAGNET_SEMANTIC_RETRIEVAL`, before model
recognition or ASL generation.

- B-02: entity/attribute IDs are governed VARCHAR keys including UUIDs. The
  publication builder required positive integers and dimension ownership only
  recognized decimal IDs. Actual capture failed `CATALOG_GOVERNED_ID_REQUIRED`.
  Recognize exact 36/32-character UUIDs and positive decimal keys with bounded
  length. Preserve UUID spelling/case; alternate forms cannot invent aliases.
  Public model/domain IDs remain strict positive integers.
- B-03: model copies retain entity IDs and regenerate attribute UUIDs. Oagnet
  joined attributes by entity ID alone, then selected the newest row per code.
  Eleven model-81 dimension bindings were replaced with other-model attribute
  IDs. Require attribute/relation model membership before deduplication and in
  discovery, value authorization, main-field uniqueness and vector-source reads.
  Entity/domain model membership must also agree.

The model-copy finding is supported by actual metadata and Java
`SemanticModelServiceImpl.copyVersionData()`. All eleven unmatched bindings
existed in raw current-model rows; each selected replacement belonged to another
model. Physical mappings/names happened to agree in this observation: wrong
catalog identity is proven, business-result leakage is not claimed. Synthetic
SQL regressions also prove a changed foreign mapping could authorize the wrong
field or suppress the valid current-model primary-name field. Physical column
membership cannot substitute for semantic model membership.

No backend Java, Agent Python or SQL Translator code changed. No permission
lookup, role-derived grant, Prompt/Regex rule, public I/O change, production V2
integration or default-model change was introduced. These proven scope P0 fixes
are within the V1 freeze exception; unrelated Legacy failures remain unchanged.

## Actual target evidence

`live_scope_evidence.json` contains timestamps, counts and hashes. Raw metadata,
UUIDs, endpoint addresses, credentials and private snapshots stay outside Git.
Oagnet uses the shared `E:/YouoAgent/.env`; the separate Agent environment was
not substituted for Oagnet configuration.

| Observation | Actual result |
| --- | --- |
| Scope | model 81 / EXPLICIT_DOMAINS [205] |
| MySQL capture | Consistent read-only transaction; 14 entities, 11 metrics, 13 dimensions |
| Initial generation | Rejected governed UUID identities |
| After UUID support only | 276 local records; 2 owned dimensions; 11 unmatched dimensions |
| After model isolation | 290 local records; 89 attributes; 13/13 owned dimensions; 15 scoped enums |
| Physical metadata | 14 tables, 110 fields |
| Existing scoped semantic Milvus records | 142; none has complete release stamps |
| Existing scoped physical Milvus records | 0 under the exact model/domain filter |
| Dedicated catalog generation collections | Both absent |
| Redis active catalog release | Absent for this exact target/scope |

Final captured catalog version:
`74de30abbbc4b3022e69fafcfee4bea036d8e49d6780f9fa232bfbd7c82e737c`.
Initial captured version:
`16ceba0fe78c5d058dedb7ad8b5b8f2091560c34eb8ff53d13122c9b72f61b7e`.
Source fixes change captured content; previous release identities cannot be
reused. Fresh authority must be recaptured before any actual publication.

The physical count describes only the exact-domain query; it does not assert
that the legacy physical collection lacks model-wide rows. Generation used
local placeholder vectors with zero real embedding/model calls. It is **not a
published index**. Metadata coverage also does not certify external entity
values, routing, governed display defaults or every business capability.

Read-only reproduction from Oagnet can use existing `capture_catalog(81, [205])`
and `build_catalog_records(snapshot, placeholder)` helpers, printing only the
coverage summary. Actual release verification is:

```powershell
python scripts/manage_catalog_publication.py verify --semantic-model-id 81 --business-domain-id 205
```

The verifier must fail closed while isolated collections/release are absent;
it cannot initialize/publish. Additional observations used the configured native
Milvus client's schema and strong query iterator APIs, plus Redis GET. No service
restart, deployment, production result query, index write or MySQL update occurred.

## Regression and review

| Service | Baseline passed / failed | Final passed / failed |
| --- | ---: | ---: |
| DataAnalysis Agent | 2258 / 27 | 2258 / 27 |
| Oagnet | 488 / 8 | 555 / 8 |
| SQL Translator | 200 / 0 | 200 / 0 |

- 67 new tests pass: 58 identity/generation/pinned acceptance and contrast cases,
  plus 9 tests executing actual loader SQL against synthetic SQLite metadata.
  The fixture only adapts MySQL numeric/string equality for SQLite; it is not a
  native MySQL optimizer or concurrency benchmark.
- UUID cases reproduced five failures before the fix. Six initial SQL cases
  failed before model constraints. Invalid/missing IDs, foreign ownership,
  mismatched UUID spelling, duplicate ownership and opaque mappings fail closed.
- Full Agent regression: all 94 modules, established offline batches; Critical
  Suite 160/160. Observed clarification trace coverage remains 89/89.
- Old pass → new fail: 0; collection errors: 0; remaining failed nodeids unchanged.
  One obsolete SQL predicate assertion was corrected with formal evidence in
  `stale_test_decision.md`. Its non-main field behavior remains unchanged.
- Self-review covered every Oagnet attribute metadata read, relation loading,
  the unique-main-field anti-join, model-wide/explicit scope and ID validation.
  No separate reviewer/agent was used. An early synthetic UUID fixture had an
  invalid length; it was corrected before final full validation.

Exact nodeids/result hashes and review evidence are in `test_delta.json`,
`final_Oagnet.json` and `source_review.json`. Full regressions ran after the final
source and contrast changes. The original Oagnet checkout HEAD and staged diff
are preserved; versioning uses explicit files in `E:/yy`.

## Remaining path

`publication_candidate.md` describes a concrete operation for review, not an
executed release. It requires explicit authorization under project AGENTS.md.
The V2 bridge was inspected, then deferred when the target exposed these earlier
catalog blockers. V-01/V-02/C-03 remain open: current trusted Scope must flow
through autonomous plan-only recognition, pinned binding and mandatory acceptance
without faking old permission hashes. SQL metadata/cache version integration
also remains open. No Gold, real benchmark, Shadow or Canary gate is advanced.

## CUTOVER READINESS

Current Stage: actual 81/[205] catalog capture and identity/isolation closure.

Cutover Blocker P0: 9 open across classes; B-02/B-03 fixed in source with read-only
target evidence, alongside previously fixed B-01. Cutover Blocker P1: 4 open.

Catalog Blocker: 4 remain open for full cutover. Source capture and 13/13 dimension
ownership are evidenced; actual publication, broader capability verification
and V2/SQL propagation remain incomplete.

Evaluation Gap: grounded 80–120 Gold, deterministic V1/V2 metrics and real-model
benchmark absent. Shadow Gap: real plan-only comparison absent.

V1 Replacement Readiness: **NOT_READY**.

Next shortest blocking path: controlled actual publication plus V2 current-scope
plan bridge; then gated Gold/evaluator, benchmark, real Shadow and <=5% Canary
readiness. No formal V1 replacement approval is sought before those gates pass.
