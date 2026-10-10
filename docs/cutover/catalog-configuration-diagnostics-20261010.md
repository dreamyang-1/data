# Actionable catalog configuration failures

## Root cause and scope

PROVEN: request-scoped catalog loading failed before ASL/SQL execution because
governed entity/attribute codes were blank. The value-source validator discarded
the failing slot and object; the V2/V1 bridge then replaced the bounded error code
with a generic configuration paragraph. This is not evidence of a SQL join error.
The existing DSL table-name fallback does not populate the authoritative code
column read by value-source capture. That accepted fallback is unchanged.

Oagnet now carries optional structured metadata issues while `str(error)` remains
the original stable code. Value-source capture reports distinct defects in the
same authorized metadata snapshot, including missing entity and attribute codes,
mapping slots, invalid identifiers/routes, and duplicate mappings. Existing
generation and SQL-source validation failures now identify missing relation codes,
join slots, duplicate metrics and malformed dependency lists where available.
No additional validation rule or permissive execution fallback was introduced.

Agent renders a deterministic list with object names/IDs, physical mappings,
configuration locations and correction guidance. Only reviewed metadata slots are
rendered. SQL expressions, credentials and connection locators are excluded.
Foreign-scope failures do not disclose foreign object details. When the upstream
error provides no object, the answer explicitly says the object is unknown.
The answer preserves SAFE_FALLBACK/SEMANTIC_CATALOG_INVALID, persistence,
idempotency and the existing SSE envelope. It never claims SQL was executed.

## Verification

- Oagnet scoped baseline: 142 passed; final: 162 passed, including 20 new cases.
- Agent scoped/critical/API/stage contract suite: 206 passed. The 16 new
  diagnostics tests additionally pass without importing legacy planner fixtures.
- Agent full offline baseline: 4778 passed, 95 failed, one existing collection
  error (`test_no_default_time.py`). Final full run: 4793 passed, the same 95
  failures and the same collection error. Old-pass/new-fail and old-fail/new-pass
  are both zero. An additional SSE diagnostics case was added after full-suite
  collection and passed in the final scoped suite; do not count it twice.
- Oagnet full offline baseline: 1605 passed/24 failed; final: 1625 passed/24 failed.
  No new failure or collection error. The final validator cleanup was rerun with
  the same result. Existing failures remain under the recorded no-new-failure
  waiver; neither full suite is represented as all green.
- A direct broad Oagnet pytest invocation could not collect 41 legacy modules
  without a local Milvus client. It was not used as acceptance evidence; the
  maintained offline runner disables clients/network and supplies explicit fakes.
- Remote preflight found independent planner/bridge changes. A legacy bridge
  test imported retired MultiQuestionPlanner and was incompatible with that
  runtime; diagnostics tests were made self-contained, not production patched.
  Current-server API/critical/stage baseline has 81 passed/8 pre-existing planning
  wording failures. Deployment compares exactly this failure set and rejects any
  new failure or collection error. Earlier attempted changes were rolled back.

## Explicit manifest and release boundaries

Oagnet: `catalog_release.py`, `catalog_value_sources.py`, `catalog_generation.py`,
`catalog_sql_sources.py`, `tests/test_catalog_configuration_diagnostics.py`.

Agent: `app/presentation/catalog_diagnostics.py`,
`app/semantic_v2/context_v1_execution.py`,
`tests/test_catalog_configuration_diagnostics.py`, and this report.

Only these files are synchronized to the canonical repository. Unrelated dirty
worktree edits are excluded from staging. The runtime release merges the bridge
import/answer hunks into its current server version, rather than replacing the
remote planner or compound-judgment changes. Configurations and independent
runtime files are protected by before/after hashes; original/candidate files
remain in private deployment backups, not Git.

No semantic database write, business write, index rebuild, permission change,
new API/SSE schema or V2 cutover is authorized by this change. Staff still need
to correct the underlying catalog configuration and revalidate. Historical
stored answers are not rewritten. Global cutover/readiness blockers are unchanged.

## Release and native validation

Released to the authorized server using merged candidates tested before the
final runtime file replacement. Oagnet server subset: 162 passed. Agent server
baseline: 81 passed/8 existing wording failures; merged candidate: 97 passed/the
exact same 8 failures. All 16 new diagnostics tests passed. Independent bridge
compound-judgment edits, planner/orchestrator files, service units and environment
configuration remained unchanged. Both affected services restarted successfully;
Agent readiness and Oagnet vector health passed. Recoverable private backups
were retained; they are not part of this commit.

Native SSE scenarios (repeated successfully): model-wide and explicit-domain
forms of the failing question both return SAFE_FALLBACK with 16 distinct issues,
including the actual shipping amount attribute, physical mapping, missing-code
configuration columns and the statement that SQL was not executed. An unaffected
authorized model's sales-quantity query returns COMPLETED with a table. Its
public stages remain intent, planning, parsing, execution, validation, insight,
final output in that order (multiple internal execution events map to one public
stage). No transport error occurred. Catalog configuration was deliberately not
changed; these failure cases validate diagnosis, not successful business queries.
