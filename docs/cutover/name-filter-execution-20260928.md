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

- Oagnet targeted binding/ownership/shared-scope suites: **89 passed**.
- Oagnet full: baseline **1087 passed** → **1106 passed**; 19 new tests,
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

Initial `abbf343` deployment passed 148 remote tests and the full live dealer-list
query: actual SQL used the product name predicate, retained key JOINs and returned
data with the required stage ordering. An additional synthetic-catalog/real-model
probe exposed a specific regression: the model selected the correct hospital
owner but returned both `keep: true` and `bind_owner: true`; the generic keep branch
discarded that owner. The two-file deployment was rolled back immediately and the
service returned UP. No config or unrelated service file was changed.

Follow-up fix gives a valid explicit owner decision precedence over generic keep;
an invalid owner selection cannot silently fall back to keep. Two regression tests
cover this response. All suites above passed again. A real-model, in-memory probe
then passed leading-zero product code, alphanumeric product name and explicit
hospital-region ownership cases (no business database mutation).

Final code release: `3dfd8c3` (including `abbf343`). Drift-guarded exact-file
redeployment completed, remote **150 tests passed**, Oagnet restarted at
2026-09-28 16:24:18 CST (PID 296262); HTTP health UP, vector health true and
configuration hashes unchanged. Backup release ID:
`recent-3dfd8c3-20260928-162413`. Other services were not restarted or overwritten.

Post-restart full dealer-list smoke completed successfully in about 46 seconds:
actual SQL filters on the canonical product name, uses product-code JOINs, returns
nonempty results, has no invented time range/clarification and preserves required
stage ordering. No name→code enumeration or display-only substitution was used.
Post-restart real-model synthetic probes for leading-zero code, alphanumeric name
and hospital-location ownership all passed without module injection.

Only the two Oagnet runtime files above are in this release's deployment manifest.
The previous, separate ASL display-label commit is not implicitly included.

This does not introduce fuzzy SQL LIKE matching or change the database collation.
Names are exact equality/set predicates after catalog matching. The model still
selects directory bindings; this change is not a guarantee of perfect matching for
every future phrase. Catalog relationship/data quality remain relevant to results.

Current stage: bounded V1 behavior fix, not V2 cutover. Existing catalog/evaluation/
shadow readiness gaps and V2 approval requirements are unchanged.
