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
- SQL compiles the shared-property restriction with nested EXISTS, preserving fact
  multiplicity even with duplicate bridge rows or multiple shared attributes.
- All relation entities, fields, edges and target predicates are rechecked against
  the scoped catalog; related sources participate in the single-source boundary.
- The displayed and final binding notices describe an inferred product-property
  association, not verified sales into clinical departments. Metric scope is the
  qualifying related-product sales in the retained outer scope, not all dealer sales.

## Tests and review

- Oagnet full offline suite: 1038 passed.
- SQL translator full suite: 486 passed.
- DataAnalysis full suite: 4325 passed, the same 11 pre-existing MCP runner failures
  as the previous release; no new failures or collection errors.
- Critical API/pending/composite/task-planning/display suite: 195 passed.
- Read-only isolated model probe selected the new scope from live catalog evidence.
  It exposed an owner-normalization ordering conflict; target predicates are now
  excluded from owner rebinding before being moved. Regression test added.
- Review protects ordinary direct queries, finite option selection, missing vector
  evidence, invalid identifiers, unknown entities and duplicate-bridge aggregation.

## Known boundary

The current hospital-to-department publication needs separate semantic-owner
maintenance. This change does not infer actual clinical transaction departments
from product applicability or silently repair that database relationship.
Current stage is bounded V1 defect repair; V2 readiness, catalog/evaluation/shadow
gaps and production routing are unchanged. Deployment and live replay pending.
