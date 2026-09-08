# Catalog version and publication contract — cutover preflight

Status: **SOURCE_PUBLICATION_AND_PINNING_VERIFIED_OFFLINE; CATALOG_BLOCKER remains
OPEN**. Capture/preparation now connects to an operator publication path and a
pinned read view in isolated Catalog collections. The current V1 API/collections,
SQL caches and V2 production routing remain unchanged. Actual 81 / 205 read-only
capture is recorded in `catalog_identity/closure_report.md`; no deployment or
index publication occurred. The V2 plan compiler now uses current scope and
pinned acceptance in `v2_scope_bridge/closure_report.md` (offline integration).
The raw-input entry and its remaining semantic coverage are documented in
`v2_recognition/closure_report.md`; recorded model responses are not native
publication or model-quality evidence.
Grounded clarification/resume acceptance also uses the pin's full finish;
see `v2_pending/closure_report.md`. Questions are not exempt from catalog drift
checks, and this does not create executed-result evidence.

## Authority and identity

The trusted business backend alone grants the current request's semantic model
and business domains. This contract identifies catalog material inside that
grant; it never grants access. Empty domains mean MODEL_WIDE, one explicit
domain remains exact, and multiple explicit domains fail closed. Shared `-1`
index records are not permission to add a domain to the request.

`Oagnet/catalog_release.py` implements:

| Identity | Material |
| --- | --- |
| Scope | Strict positive model ID, normalized domain set, MODEL_WIDE / EXPLICIT_DOMAINS |
| Source identity | SHA-256 of MySQL server UUID and database name; public receipts do not expose them |
| Catalog version | SHA-256 of contract version, source identity hash, scope, captured DSL documents and physical catalog |
| Publication ID | Nonempty identifier supplied by the trusted publisher, never by a model or historical task |
| Record hash | Entire record ID, text and metadata, excluding only the four release stamps |
| Vector index version | SHA-256 of scope/catalog/source identity, publication ID, producer revision, embedding contract and sorted record hash inventory |
| Generation/target | Isolated physical record IDs, scope key, generation token, exact Milvus target hash, embedding dimension and source coverage are included in the manifest digest |
| Activation | Redis PUBLISHED marker with a fresh activation ID; same-release reactivation invalidates old readers and cache fingerprints |

Canonical JSON sorts dictionary keys and domain documents, preserves ordered
semantic arrays, and rejects unsupported values. It does not silently sort
semantic arrays or equate reordered rules. Input snapshots/records are copied.
The standalone supplied-evidence checker does not read embeddings. The integrated
generation path additionally hashes float32 vectors and verifies them during
full Milvus read-back. This proves storage equality, not vector quality or model
recognition accuracy. ANN reads require the matching embedding transport contract.

`capture_catalog()` uses the existing MySQL loaders within one read-only
REPEATABLE READ transaction. It checks that all ten tables read by those loaders
exist and use InnoDB. Model-wide capture enumerates each model-owned domain;
calling the existing `get_dsl_by_scope(model, None)` alone would omit entities.
It also captures model-owned metrics with no business domain in a separate
model-wide document; orphan domain IDs are rejected. Explicit capture never
performs this model-wide metric read.
Explicit capture uses only the requested domain, including the existing scoped
physical table loader. The transaction rolls back and closes on success/failure.
Ordinary loader calls retain their connection lifecycle outside this opt-in
context. Actual scoped capture was exercised in the 81 / 205 identity closure;
native publication and deployed query acceptance remain unverified.

Captured material is the semantic/physical metadata consumed by these loaders.
It is not a claim to cover all source tables: global enums, entity-value source
rows/policies, data-source routing and SQL's independent fallback reads require
their own governed dependency/coverage proof before a release is complete.
No credentials or source business result rows are captured.

## Implemented verification boundary

`prepare_release()` rejects duplicate/empty inventories, foreign model/domain
records and pre-stamped records. Explicit releases may contain owned domain
projections, but never shared originals. Model-wide records remain within the
captured model's domains (and its shared record family). It returns PREPARED
material; it writes no marker or vector data.

`verify_release()` requires an exact PUBLISHED marker, matching current authority
identity/scope/version, and the full record ID/hash inventory. Missing rows,
extra rows, duplicates, legacy stamps and changed text/metadata fail closed.
It verifies supplied material; callers must prove the export is complete and
comes from the trusted runtime. A manifest generated from an arbitrary subset
does not prove snapshot-to-record completeness.

`Oagnet/scripts/check_catalog_release.py` accepts a private bundle with
`current_snapshot`, `manifest`, `index_records`, `marker_before`, `marker_after`
and `inventory_complete`. Changed markers or an incomplete inventory are rejected.
The report contains only bounded reason codes/identities/counts. Exit 0 means
**VERIFIED_SUPPLIED_EVIDENCE**, always `live_runtime_verified=false`; it does not
mean CATALOG_GATE PASS. It does not instantiate a vector client, call embedding,
publish an index or certify the origin/freshness of a saved file.

## Source integration and remaining runtime gates

1. **Implemented offline:** build records from the captured snapshot with deterministic IDs and a
   source-to-record coverage manifest. Account separately for owned projections,
   shared definitions, registered attributes, physical metadata, enum/rule
   dependencies and entity-value policy. The new generator checks coverage for
   captured DSL and physical source records. Shared originals are excluded from
   explicit releases, while missing owned projection/default/rule coverage and
   external entity-value dependencies remain C-04. Missing stable/governed IDs,
   inconsistent shared definitions and silently omitted records fail closed.
2. **Implemented offline:** stage immutable generations in dedicated semantic
   and physical collections. Neither their names nor normalized names may
   overlap V1 collections. Generation IDs and filters isolate versions; old rows
   remain available for rollback. The original V1 rebuild is unchanged.
3. **Implemented offline:** persist the active PUBLISHED marker through a trusted, atomic activation
   boundary (existing Redis WATCH/MULTI, no TTL) after complete strong read-back
   and current authority checks. Reserve publication IDs before writes, reject
   competing activation, retain old generations and detect ABA with activation
   IDs. Lost activation acknowledgement is explicitly outcome-unknown; read back
   before retrying. Native Redis persistence and deployment are not yet verified.
4. **Partially implemented:** the pinned read view verifies model/domain/release,
   exact target, full inventory and query embedding identity. ANN and exact reads
   use strong consistency. `finish()` rechecks source, complete inventory and
   activation before a caller may accept/cache a plan. `ScopedPlanSession` now
   connects this identity through candidate bindings, restored state, checked
   TaskPatch, turn resolution, LogicalPlan and cache keys. Its compile method
   requires full `finish()` before returning a plan-only artifact. Reject
   missing/changed identity in this V2 path; current
   public Agent request/response/SSE formats and V1 routing remain unchanged.
   Bind the current AuthorizedSemanticScope independently of catalog identity.
   `RawTurnPlanner` now connects model surface extraction and scoped handle/slot
   interpretation to this boundary for common payloads. Full Pending/semantic
   coverage, production state-store integration and the current Oagnet HTTP API
   remain open. The plan
   session combines the catalog pin with current request/identity/database/KB
   in its fingerprint; the pin does not replace those state-isolation checks.
5. **Still open:** bind SQL metadata caches and auxiliary authoritative lookups to that same
   release. Versioning only Milvus cannot detect SQL using newer MySQL formulas
   or older process-local metadata. Execution integration remains gated after
   plan-only evidence; no V2 production SQL is added by this contract.
6. **Still open:** obtain target-bound deployed revisions, trusted caller-boundary evidence,
   fresh MySQL identity/capture, a complete Milvus export and a stable active
   marker. Compare MySQL → expected records → actual records, then run model-wide,
   explicit-domain and scope-change acceptance against that exact release.

`Oagnet/scripts/manage_catalog_publication.py` exposes operator-only `verify`,
`initialize`, `publish` and `reactivate` actions, requiring a model and optional
single explicit domain. Invalid/multiple scope is rejected before opening clients.
`verify` creates/loads/writes no collection. The other actions require the named
approved operational target; none was executed here. Reader opening checks schema;
initialization is a separate explicit operation. Record private runtime receipts
with exact target, model/domain, deployed revision and durable registry evidence.

The prior `phase0c_oagnet_closure/catalog_publication_plan.md` is historical.
Its existing `/vector/rebuild` step alone cannot close the newly identified
version/activation gaps. Do not execute it as if a release marker already exists.
Production/index operations still require an identified, approved target and
the existing controlled release process; credentials stay private.

## Evidence required to close the four catalog blockers

| Blocker | Required evidence | Present now |
| --- | --- | --- |
| C-01 | Consistent authority capture plus complete deterministic scoped generation | Actual 81/[205] capture generates 290 records locally after UUID/model isolation fixes; real embedding/publication pending |
| C-02 | Persisted active marker, verified full read-back, mixed-generation rejection and rollback | Actual target inspected: isolated collections and active marker absent; 142 legacy semantic rows unstamped |
| C-03 | Same release on query, binding, caches and downstream metadata | V2 raw-input/common-payload path, scoped compiler and pin acceptance integrated offline; full semantics, production stores/Oagnet API/SQL integration pending |
| C-04 | Current owned projections/IDs/defaults/rules and actual MySQL/Milvus coverage | Actual 13/13 dimensions have owned projections; external values/defaults/routing and runtime publication still pending |

X-01 separately tracks the named runtime target and trusted operational evidence.
Old local Chroma counts do not describe the current Milvus deployment.

Current evidence: `catalog_identity/closure_report.md`. The user selected the
current configured target model 81 / domain 205. Read-only MySQL/Milvus/Redis
inspection is complete; this is not authorization for index writes. Entity and
attribute VARCHAR keys may be UUIDs. Model copies reuse entity IDs, so all
attribute/relation identity joins require model membership before deduplication.

## Scoped structured-edit source acceptance

`v2_slot_edits/closure_report.md` extends the same current-scope raw planner with
exact task-version filter handles and partial temporal edits. Restored state is
validated before transient labels/values reach the model. Unchanged references
come from that restored task; new references use current pinned binding handles.
Full catalog acceptance still precedes returned plan/state artifacts. This is
offline source evidence and does not close native catalog publication or trust.
