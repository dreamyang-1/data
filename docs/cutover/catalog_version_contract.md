# Catalog version and publication contract — cutover preflight

Status: **PARTIAL_SOURCE_CONTRACT; CATALOG_BLOCKER remains OPEN**. This contract
defines the minimum required identity and implements capture/preparation/supplied
evidence verification. It is not connected to the current publisher or query
path. No deployment, index rebuild or real catalog read was performed.

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

Canonical JSON sorts dictionary keys and domain documents, preserves ordered
semantic arrays, and rejects unsupported values. It does not silently sort
semantic arrays or equate reordered rules. Input snapshots/records are copied.
Embeddings themselves are not hashed; this contract does not certify vector
quality, embedding model execution or numerical equivalence.

`capture_catalog()` uses the existing MySQL loaders within one read-only
REPEATABLE READ transaction. It checks that all ten tables read by those loaders
exist and use InnoDB. Model-wide capture enumerates each model-owned domain;
calling the existing `get_dsl_by_scope(model, None)` alone would omit entities.
Explicit capture uses only the requested domain, including the existing scoped
physical table loader. The transaction rolls back and closes on success/failure.
Ordinary loader calls retain their connection lifecycle outside this opt-in
context. Actual MySQL server behavior has not yet been exercised here.

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

## Minimum integration still required

1. Build records from the captured snapshot with deterministic IDs and a
   source-to-record coverage manifest. Account separately for owned projections,
   shared definitions, registered attributes, physical metadata, enum/rule
   dependencies and entity-value policy. Missing governed IDs or unsupported
   opaque rules are blockers, not inferred defaults.
2. Stage one immutable release through the existing publication mechanism.
   Current upsert-then-delete is not atomic activation: readers can see mixed
   generations. Scope releases need isolated record identity or a generation
   filter; no query may combine unstamped legacy rows with a verified generation.
3. Persist the active PUBLISHED marker through a trusted, atomic activation
   boundary only after a complete read-back agrees with the prepared manifest.
   Keep the previous generation for rollback. Concurrent activation and a
   changed marker during verification must abort acceptance. A JSON marker
   supplied to a pure test is not a persisted publication record.
4. Pin the verified identity at query admission and propagate it through
   retrieval, grounding, TaskPatch/LogicalPlan, cache keys and result provenance.
   Recheck at acceptance. Reject missing/changed identity in the V2 path; current
   public Agent request/response/SSE formats and V1 routing remain unchanged.
   Bind the current AuthorizedSemanticScope independently of catalog identity.
5. Bind SQL metadata caches and auxiliary authoritative lookups to that same
   release. Versioning only Milvus cannot detect SQL using newer MySQL formulas
   or older process-local metadata. Execution integration remains gated after
   plan-only evidence; no V2 production SQL is added by this contract.
6. Obtain target-bound deployed revisions, trusted caller-boundary evidence,
   fresh MySQL identity/capture, a complete Milvus export and a stable active
   marker. Compare MySQL → expected records → actual records, then run model-wide,
   explicit-domain and scope-change acceptance against that exact release.

The prior `phase0c_oagnet_closure/catalog_publication_plan.md` is historical.
Its existing `/vector/rebuild` step alone cannot close the newly identified
version/activation gaps. Do not execute it as if a release marker already exists.
Production/index operations still require an identified, approved target and
the existing controlled release process; credentials stay private.

## Evidence required to close the four catalog blockers

| Blocker | Required evidence | Present now |
| --- | --- | --- |
| C-01 | Consistent authority capture plus complete deterministic scoped generation | Capture/preparation unit coverage; generator integration absent |
| C-02 | Persisted active marker, verified full read-back, mixed-generation rejection and rollback | Supplied marker/inventory validation only |
| C-03 | Same release on query, binding, caches and downstream metadata | Existing selected-metric hashes only; no release propagation |
| C-04 | Current owned projections/IDs/defaults/rules and actual MySQL/Milvus coverage | Source projector tests and historical local snapshot only |

X-01 separately tracks the named runtime target and trusted operational evidence.
Old local Chroma counts do not describe the current Milvus deployment.
