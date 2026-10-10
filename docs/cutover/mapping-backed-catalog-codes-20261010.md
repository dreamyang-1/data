# Mapping-backed semantic codes, without UI or metadata changes

## Proven cause and bounded correction

PROVEN: the physical shipping amount column exists (DECIMAL), and the semantic
registry already contains its table/column mapping. Separate entity/attribute
code columns can still be empty. An existing entity-table fallback was applied
by the DSL reader but not by value-source validation; attributes had no matching
fallback. Five declared relations also lacked optional technical codes.

Native verification identified the second first-divergence point: the deployed
HTTP SQL translator independently rejected empty entity codes in its MySQL DSL
bundle. Fixing only Oagnet was insufficient. The actual service working directory
was verified, rather than assuming that the pinned-translator auxiliary copy was
the HTTP runtime. That auxiliary copy is deliberately unchanged.

The read-time contract is now consistent:

- Preserve every existing nonempty declared code.
- Project missing entity codes from validated, configured main-table names.
- Project missing attribute codes from validated, configured mapping columns.
- Oagnet names existing uncoded relations using their governed relation IDs;
  endpoints, physical join keys and cardinality are never inferred or changed.
- Keep technical codes distinct from business record identities. These fallbacks
  do not prove uniqueness of a business entity, authorize a join, or alter a KPI.
- Reject ambiguous derived identities and unresolvable/unsafe mappings. Genuine
  remaining catalog defects retain the existing structured diagnostics.

Oagnet applies the same projection to DSL, attribute discovery, metric entity
dependencies, captured value-source evidence, live lookup revalidation and SQL
source capture. SQL Translator applies it to the MySQL bundle, entity graph,
resolved metric bindings and scoped data-source attachment.

No UI change, metadata write/backfill, business write, index rebuild, permission
change, API/SSE change or V2 cutover was performed. Model/domain ownership and
read-only execution restrictions remain unchanged.

## Verification

- Oagnet focused suite: 198 passed, including 25 new projection cases.
- Canonical full Oagnet baseline: 1625 passed/24 existing failures; final:
  1650 passed/the same 24 failures, no collection errors and no common-test
  outcome changes. One diagnostics case was renamed; its expectation now tests
  genuinely unresolvable missing codes. This STALE_TEST change follows the user's
  explicit mapping-backed contract, not a relaxation of missing physical data.
- Agent critical/API/stage/diagnostics suite: 105 passed. Agent source unchanged.
- SQL Translator canonical full baseline: 647 passed; final: 659 passed,
  including 12 new cases. No existing assertion was changed.
- Server Oagnet candidate: 198 passed. Real scoped catalog generation succeeds
  for the affected model (6 entities, 59 attributes, 5 relations, 59 verified
  value-source mappings) and the established model (14 entities, 89 attributes,
  24 relations, 89 value-source mappings). Empty-domain requests use the existing
  single-domain resolution path; no model-wide scope expansion was introduced.
- Server SQL baseline: 645 passed/5 existing publication-pin fixture failures;
  merged candidate: 657 passed/the same 5 failures, no collection errors. All 12
  new cases pass. These existing failures are not represented as a green suite.
- Native post-release verification already covers shipping amount in explicit
  and empty-domain forms, invoiced receivables, unshipped production amount,
  actual invoiced total, repayment total, shipping date/amount detail, payment
  date/amount detail, the established hospital sales-quantity question, and the
  established product-to-hospital partnership list. In total, 10 native scenarios
  (9 distinct questions, including two authorization-scope forms of shipping
  amount) completed successfully with tables and no query/transport error.
  Each executed SQL, returned a table and preserved the seven public stages.
  A bare repayment metric-name variant returned without executing SQL; it is not
  counted as a passing query. The explicit repayment-total request passed.
  A concurrent service restart window interrupted the partnership-list request
  before connection; its isolated retry passed after readiness recovered. All
  five deployed source hashes remained unchanged across that window.
- The established model's before/after catalog version hashes are identical.
  The affected model still has the original 6 blank entity and 10 blank
  attribute codes, independently confirming that no backfill was performed.

## Release manifest and protection

Oagnet: `mysql_tool.py`, `catalog_value_sources.py`, `catalog_sql_sources.py`,
`tests/test_catalog_mapped_codes.py`,
`tests/test_catalog_configuration_diagnostics.py`.

SQL Translator: `sql_translator_prod.py`, `semantic_scope.py`,
`test_mapping_backed_codes.py`.

Only this manifest and this report are synchronized to the canonical repository,
with per-file SHA-256 comparison. Concurrent unrelated development tests and
source edits are not included. Runtime deployment preserves independent source,
environment files, service units and configuration; it retains recoverable
private backups. Failed preflight candidates do not replace live files. A health
probe targeting the wrong existing port triggered rollback before successful
release with the unit's configured port. All affected services are healthy.

Global V2 replacement readiness is unchanged; this is a bounded current-query
compatibility fix, not production cutover approval.

Oagnet implementation commit: `3ce5871`. SQL Translator and this report are
committed separately on the same feature branch. Draft review never auto-merges.
