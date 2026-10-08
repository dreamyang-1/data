# Existing publication: physical catalog and field enums

## Root cause and bounded fix

PROVEN: the platform's existing publish flow called `/vector/rebuild`, whose
only rebuild call generated semantic records. The physical table/field rebuild
was used by the separate migration script, not that publication route. Model
copy created SQL registrations with new model/data-source identities but did
not create their physical vector records. The old model's existing physical
records therefore did not prove the copy/publication path was complete.

PROVEN: ordinary ASL retrieval uses semantic entities/attributes and scoped
dimensions/values; it does not retrieve the physical collection. Publishing
field enums there alone would not make field-only enums usable. The fix attaches
source-proven enums to the semantic attributes as part of publication and
includes them in the attribute embeddings; ASL's existing binder is unchanged.

The existing request/response fields, URLs, model/domain authorization, physical
record IDs, task planning, time parameters, progress nodes and runtime routing
remain unchanged. Existing server-only LIKE filter support is preserved and
covered by a compatibility regression; it is not removed by the deployment.

## Change manifest and review

- `api.py`: existing model lock and one read-only SQL snapshot; prepare physical
  records, publish enriched semantic records, publish/verify physical records,
  return combined counts through the original response fields.
- `mysql_tool.py`: the scoped physical loader includes explicitly declared
  sub-tables and verifies entity model ownership, still using two scoped queries.
- `physical_catalog_sync.py`: data-source grouping, source-proven enum mapping,
  upsert/read-back/stale cleanup, protection of other models/domains/families.
- `vector_store.py`: optional publication-only enum enrichment; original direct
  semantic rebuild callers remain semantic-only; embed available field enums.
- `tests/test_physical_catalog_sync.py`: synthetic API -> vectors -> real prompt
  retrieval -> ASL binding, common enum forms, single/multi-value/code filters,
  multiple data sources, repeat publication, protected scopes and failure paths.
- `SEMANTIC_SCOPE_PUBLICATION_CONTRACT.md` and this closure: updated publication
  behavior and explicit operational limitations.

Self-review: no business rows read for index generation; no API schema additions,
query-time rebuild, shared-domain query grants, production model change, Java
API change or V2 routing change. Cleanup intentionally remains bounded; physical
write failure is not reported as success. This legacy multi-collection flow is
not claimed to offer atomic rollback.

## Verification and test delta

| Suite | Baseline | Final |
| --- | ---: | ---: |
| Local Oagnet full offline | 1,228 passed | 1,266 passed |
| Remote staged Oagnet full offline | — | 1,266 passed |
| Added publication/enum/compatibility cases | — | 38 passed |
| DataAnalysis critical/scope/workflow/contracts | unchanged sources | 177 passed |
| SQL Translator full offline | unchanged sources | 625 passed |

Old-pass -> new-fail: 0. Baseline failures: 0. Final failures and collection
errors: 0. No existing assertions were weakened or relabeled. The remote suite
reports one dependency deprecation warning, not a functional failure.

Read-only source audit of the original model and two copied models confirms the
candidate builds their exact published domains' table/field records and carries
the channel enum label “其他” to its mapped semantic attribute. No vector store
instance or production index write was used for this audit. Synthetic tests,
not a live publication, prove the write/read-back/ASL chain.

## Operations and readiness boundary

Code deployment must compare preflight hashes, preserve remote-only behavior,
back up the four runtime files and protect configuration; restart only Oagnet,
check vector health, unchanged OpenAPI and neighboring service readiness.
The existing feature branch/Draft PR remains unmerged.

Index rebuilding is not authorized by the default code-deployment permission.
This change does not silently rebuild existing model indexes. After deployment,
invoke the platform's existing publish operation once for each copied model.
Only its successful completed synchronization is evidence that production
indexes are populated; existing copy behavior and unpublished status are retained.

Deployment verified: code commit `877d9a5` was pushed to the existing feature
branch/Draft PR without merging. Four explicit runtime files were backed up and
installed, retaining the pre-existing server LIKE support. Oagnet restarted;
the new worker started after the installed files, all runtime SHA-256 hashes
match the validated candidate, and vector health is successful. The full live
OpenAPI equals the pre-deployment source's generated OpenAPI. Neighboring
DataAnalysis readiness is `READY` and SQL health is `ok`.

The initial restart command exceeded its 60-second caller deadline while the
old worker was exiting under the existing service stop policy. Inspection and
subsequent verification confirmed the queued restart had completed; no repeated
restart, manual process kill or service configuration change was needed.
No production vector rebuild was invoked during deployment or verification.

Current stage: V2_CONTEXT_V1_EXECUTION unchanged. Current task code/tests can be
verified independently of cutover. Catalog gap: the old copied-model indexes
still require the authorized platform publication. V1 replacement readiness
and V2 cutover authorization are unchanged; no cutover readiness is claimed.
