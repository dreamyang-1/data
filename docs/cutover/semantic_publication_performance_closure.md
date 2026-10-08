# Semantic model publication performance

## Scope and verified cause

PROVEN: the existing Java publication endpoint already starts an asynchronous
task, and Oagnet embedding already uses a bounded batch thread pool. Oagnet
publication nevertheless regenerated the full semantic, physical and attribute
value vectors on each run. Attribute values were read with a new connection per
attribute, including repeated reads of a shared physical column.

This change targets that repeated work in Oagnet. Public API requests, responses,
status meanings, frontend buttons, authorization and the seven data-analysis
stages are unchanged. No Java-wide HTTP timeout or executor changes are included.

## Behavior and review

- Prepare stable records before embedding. Reuse requires the same record ID,
  exact text, model/domain/entity/attribute/physical ownership, provider/model/
  dimension signature and a finite vector with the correct dimension. Names alone
  cannot select a cached vector. Unknown-dimension compatibility stores regenerate.
- Read prior vectors in bounded inventories of at most 256 IDs, with explicit
  model/type filters and domain filters for domain-owned records. There is no
  cross-request text cache, new vector collection or production schema migration.
- Generate only missing/changed vectors, using the existing bounded embedding
  pool. Skip writes only when the complete stored metadata and vector match.
  Metadata-only changes still update the record. Parent records whose embedded
  child metadata changes are updated without re-embedding unchanged parent text.
- Existing scoped stale-record deletion, disabled-attribute cleanup, empty
  authoritative snapshots, model-level publication locks and physical read-back
  verification remain. Preparation failure does not delete old value records.
- Reuse source connections only inside one authorized value-read invocation and
  only for identical datasource and connection settings. Read each physical column
  once, then project values separately to all its owning entities/attributes.
  All connections close on success or failure. SQL and existing row limits remain.
- Log stage durations and outcomes, embedding/reuse counts and changed write
  counts without logging credentials or business values in the new instrumentation.
- Provider/model/dimension changes invalidate reuse. An optional existing-env
  override `OAGNET_EMBEDDING_REVISION` invalidates vectors when an operator changes
  weights under the same model alias. No deployed environment file is changed.

REVIEW: no ASL/SQL changes, scope widening, query-time rebuild, frontend change,
status shortcut, skipped validation, global transaction/thread change, new public
field or V2 cutover. Shared HTTP client timeouts are left unchanged because they
also govern other workflows; shortening them without runtime timings can break
legitimate first publications.

## Tests and measurable work reduction

- Local baseline: 1,312 Oagnet tests passed. Final: 1,348 passed. Added cases: 36;
  collection failures 0; old-pass to new-fail 0; no old assertion changes.
- Targeted Oagnet publication/scope/ownership/catalog Critical regression: 316
  passed. DataAnalysis cross-service and Critical regression: 135 passed.
- Server isolated baseline: 1,312 passed. Candidate: 1,348 passed, with the same
  offline mocked database and vector-store boundary. No live publication was run.
- A 1,000-record unchanged snapshot requires 1,000 embeddings on its initial
  generation and 0 on the next run; old-vector reads remain bounded. The existing
  publication API synthetic full-chain fixture similarly reduces 4 embeddings to
  0 for an unchanged repeat, preserving its response and stored records exactly.
- Changing an enum requires 2 new embeddings (semantic attribute and physical
  field); associated parent metadata updates without recomputing parent vectors.
- Three owned attribute definitions sharing one datasource and two physical
  columns use one connection and two queries, retaining all three owner mappings.
- Coverage also includes copied/wrong models, same-name different entities,
  source/credential boundaries, metadata-only changes, old unsigned records,
  changed embedding contracts, invalid vectors, additions/deletions, empty source,
  failed preparation, connection cleanup and Chroma inventory compatibility.

## Explicit manifest

- `Oagnet/api.py`
- `Oagnet/vector_store.py`
- `Oagnet/mysql_tool.py`
- `Oagnet/physical_catalog_sync.py`
- `Oagnet/publication_vectors.py` (new)
- `Oagnet/tests/test_publication_performance.py` (new)
- `docs/cutover/semantic_publication_performance_closure.md` (this report)

Only the five production Oagnet files are deployed. Tests and this report remain
in version control; credentials, logs, backups and private release tools do not.

Deployment verified: remote drift was absent, original files were backed up,
installed hashes match the tested candidate, the affected Oagnet service restarted
with a fresh worker, its vector health is healthy, DataAnalysis readiness is READY
and SQL Translator health is ok. The complete public OpenAPI schema is unchanged.
The release manifest excludes environment/configuration files and unrelated
source. An initial non-blocking restart check observed the old worker; a separate
service inspection and start-time verification confirmed the completed restart.
Installed-module offline smoke also passed all 36 new cases with database/vector
clients mocked; it did not write production indexes.

## Operational limits

The first publication after this patch regenerates legacy records lacking the
signature; later publications can reuse them. Every publication still reads the
authorized current source values so data changes are not missed. First-publication
data volume, slow source queries and upstream rate limits can still take time.
Wall-clock speedup on the user's production model is not yet measured; work-count
reductions above are offline test evidence, not a promised latency percentage.

Current Stage remains `V2_CONTEXT_V1_EXECUTION`. This scoped publication
optimization is not a V1 replacement-readiness or broader Catalog/Evaluation/
Shadow gap closure. No semantic database mutation, index rebuild, production
cutover or automatic PR merge is performed during validation/deployment.
