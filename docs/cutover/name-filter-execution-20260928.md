# Name/code predicate execution — 2026-09-28

## Scope and evidence

PROVEN: structured binding already produces canonical name predicates. The first
divergence is `Oagnet/query_binding_review.py`: selecting any relationship option
previously enumerated dictionary keys and replaced the name predicate. SQL
Translator already accepts name predicates and connects tables using catalog keys.

User-approved behavior: product names filter on matched standard names; product
codes filter on the supplied codes. Same-name products remain a set, not a randomly
selected identifier. Public request/response fields, URLs, ASL schema and SSE stages
are unchanged. No business database, semantic configuration or vector index write.

## Changes

- `Oagnet/structured_binding.py`: clarify field meaning, not digit/letter appearance,
  determines name vs code binding. Existing literal validation stays unchanged.
- `Oagnet/query_binding_review.py`: ordinary predicates are preserved. Selecting a
  JOIN edge alone no longer triggers key enumeration. Explicit business-owner
  disambiguation requires `bind_owner: true` in the internal model decision (not
  an external API field). Already-bound relationship keys are not reinterpreted.
- Shared geography dictionaries can still resolve hospital/dealer ownership to
  the appropriate foreign key. This is an intentional exception, not a blanket
  conversion of all names to IDs. Shared-attribute target scopes remain unchanged.
- SQL Translator runtime did not need modification. JOINs, entity grouping and
  distinct entity counting continue using catalog identity, never name JOINs.

## Verification

- Oagnet targeted binding/ownership/shared-scope suites: **87 passed**.
- Oagnet full: baseline **1087 passed** → **1104 passed**; 17 new tests,
  no old-pass → new-fail, no collection errors.
- SQL name execution / role JOIN / shared-scope suites: **24 passed**,
  including 8 new tests. In-memory database confirms 12 same-name products are all
  matched, unrelated names excluded; alphanumeric names, apostrophes, multiple
  names and leading-zero codes covered.
- DataAnalysis Critical Suite: **229 passed**. No DataAnalysis runtime change in
  this release; its known 11 unrelated MCP baseline failures are not modified.
- Existing ownership tests now declare the internal `bind_owner` decision
  explicitly. Assertions preserving hospital/dealer roles remain unchanged.

## Release and limits

Pending remote drift check, deployment, restart and live read-only smoke. Only the
two Oagnet runtime files above are in this release's deployment manifest. The
previous, separate ASL display-label commit is not implicitly included.

This does not introduce fuzzy SQL LIKE matching or change the database collation.
Names are exact equality/set predicates after catalog matching. The model still
selects directory bindings; this change is not a guarantee of perfect matching for
every future phrase. Catalog relationship/data quality remain relevant to results.

Current stage: bounded V1 behavior fix, not V2 cutover. Existing catalog/evaluation/
shadow readiness gaps and V2 approval requirements are unchanged.
