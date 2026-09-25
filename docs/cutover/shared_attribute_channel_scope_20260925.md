# Shared-attribute channel query correction

## Scope and evidence

PROVEN: the reported extraction/ASL lost the shared applicable-department
requirement and constrained historical sales to the target products instead.
Read-only source checks confirmed an empty intersection for those exact products,
not an absence of hospital business or of shared product-department metadata.
PROVEN: the maintained description conflated product-department bridges with
dealer-product links and dealer profiles. The invalid description rows are removed;
no catalog publication or source-data mutation is included in this release.

## Contract

- Existing extraction keys and six public stages remain unchanged. The extraction
  prompt preserves relationship intent in entities/output requirements, without
  inventing a department filter or group-by.
- Contextual binding review may select a finite, vector-authorized shared-attribute
  path. No hospital, product, brand, department code or question keyword is hardcoded.
- Optional ASL `related_filters` moves target predicates into a separate target
  set. Outer hospital/location predicates, metric, grouping, sorting, limit and
  explicit time remain unchanged. Direct product-sales questions retain flat filters.
- SQL compiles the shared-property restriction with an IN semi-join, preserving fact
  multiplicity even with duplicate bridge rows or multiple shared attributes.
- All relation entities, fields, edges and target predicates are rechecked against
  the scoped catalog; related sources participate in the single-source boundary.
- The displayed and final binding notices describe an inferred product-property
  association, not verified sales into clinical departments. Metric scope is the
  qualifying related-product sales in the retained outer scope, not all dealer sales.

## Tests and review

- Oagnet full offline suite: 1039 passed.
- SQL translator full suite: 491 passed.
- DataAnalysis full suite: 4325 passed, the same 11 pre-existing MCP runner failures
  as the previous release; no new failures or collection errors.
- Critical API/pending/composite/task-planning/display suite: 195 passed.
- Read-only isolated model probe selected the new scope from live catalog evidence.
  It exposed an owner-normalization ordering conflict; target predicates are now
  excluded from owner rebinding before being moved. Regression test added.
- Review protects ordinary direct queries, finite option selection, missing vector
  evidence, invalid identifiers, unknown entities and duplicate-bridge aggregation.
- Live replay exposed two integration differences: recalled relation neighbors may
  be absent from the top-k entity list, and published bridge edges may exist only
  on the reverse entity. Target modifiers now use recalled relation evidence and
  SQL uses the complete scoped relationship snapshot. Positive/negative tests added.
- The existing read-only validator rejected generated subqueries. It now recognizes
  only the bounded compiler envelope; arbitrary/extra subqueries and all mutation,
  source and semantic-scope checks remain in force. Interface-level tests cover it.
- A subsequent live replay reached SQL execution but hit the request timeout.
  EXPLAIN proved nested EXISTS used dependent subqueries per fact; the equivalent
  uncorrelated membership query uses materialization and completed a scoped count
  probe in under one second. Duplicate-safe aggregation tests remain passing.
- Remote pre-restart tests: SQL 68 passed (plus 3 subtests), Oagnet 29 passed,
  DataAnalysis 114 passed. All three services restarted with healthy responses;
  deployment verified unchanged configuration files and healthy vector access.

## Known boundary

The current hospital-to-department publication needs separate semantic-owner
maintenance. This change does not infer actual clinical transaction departments
from product applicability or silently repair that database relationship.
Current stage is bounded V1 defect repair; V2 readiness, catalog/evaluation/shadow
gaps and production routing are unchanged. No V2 replacement readiness is claimed.

## Deployment and acceptance

- Code release 25ee32a deployed with per-file drift checks and recoverable backup.
  All three services restarted on 2026-09-25 at 18:52 CST, health and vector checks
  passed; environment/configuration files were unchanged.
- The original question was replayed through the production stream with an isolated
  conversation: COMPLETED in 70.6 seconds, 10 rows returned, no error code.
- Assertions passed for retained related scope, target brand absent from outer
  sales filters, materializable membership SQL, descending top-10 ranking, no
  default time range, scope disclosure in final output, and all six public stages
  in order. No raw result rows are stored in this report.
- Current bounded query defect has no remaining observed blocker. The separate
  hospital/department publication issue and existing MCP test failures remain
  outside this change; no semantic database write or index rebuild was performed.

## Change manifest

- DataAnalysis: structured extraction prompt and semantic-description corrections;
  presentation/intent_recognition and adapters/asl_notices preserve scope disclosure;
  tests/test_related_scope_display protects the display contract.
- Oagnet: related_scope derives finite shared-property options; query_binding_review
  selects them contextually; agent includes nested fields in vector validation;
  tests/test_related_scope protects direct queries and predicate preservation.
- SQL: related_scope_sql compiles membership semi-joins; sql_translator_prod wires
  compilation and bounded read-only recognition; semantic_scope includes nested
  sources; test_related_scope_sql covers fan-out, reverse edges and rejected SQL.
- Code commits: 19b592c (scope support), af24d01 (relation integration),
  1510804 (read-only integration), 25ee32a (materializable membership).
