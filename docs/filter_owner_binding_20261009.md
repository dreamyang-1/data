# Predicate ownership binding — 2026-10-09

## Scope and first divergence

PROVEN: the department-ranking question produced an unowned shared province
predicate. SQL could legally reach that dictionary through the dealer instead
of the hospital. Query subject, output grouping and predicate ownership are
different concepts; selecting a fact-table subject does not identify whose
location a filter constrains. Whether that wrong route alone explains the
original empty result remains UNKNOWN until a live counterfactual is checked.

The user authorized this fix and deployment of verified Agent, Oagnet and SQL
updates, explicitly retaining verified old failures only if no new failures
appear. The unrelated Java backend is excluded. No semantic writes, vector
rebuild, configuration replacement, authorization expansion or V2 cutover.

## Service changes

- Agent: optional owner on structured filters; preserve it during grounding,
  deduplication, planner-to-canonical handoff, contract completeness checks and
  generated-filter validation. Same literal on different owners stays separate.
- Oagnet: use the declared owner and published relationship deterministically.
  Source-verified dictionary keys produce an owner-specific foreign-key filter
  with a per-predicate receipt. A missing/ambiguous owner is not inferred from
  subject, grouping or shortest path. Preserve direct name filters and existing
  entity-alias behavior; never convert ordinary names to IDs just for joins.
- SQL: existing deterministic translator consumes the explicit physical field.
  No additional natural-language interpretation and no new SQL code required.
- Preserve two server-only differences (semantic-description failure logging
  and a planner docstring) in the local version repository; they are not removed
  or re-deployed.

The release also includes previously verified local final-summary/chart output
updates. SQL's MySQL semantic-store implementation already matches the server.

## Verification and release closure

- Local Agent critical suite: 102 passed.
- Server-isolated Agent critical suite: 102 passed.
- Server-isolated Oagnet critical suite: 528 passed.
- Server-isolated SQL full suite: baseline/candidate 647 passed, no failures.
- Server-isolated Oagnet full suite: baseline 1,508 passed / 11 old failures;
  candidate 1,531 passed / the same 11 failures; no removed cases.
- Server-isolated Agent full comparison: baseline 4,599 passed / 96 old failures
  / one pre-existing collection error; candidate 4,615 passed / exactly the same
  failures and collection error. Zero new failures or removed cases.
- Live original question plus three owner-variation questions: pending.
- Exact-manifest backup, protected-source/configuration checks, restart and
  health verification: pending. Production has not yet changed for this fix.

Tests cover positive/negative and multi-value filters, multiple owners sharing
one literal, missing keys/relationships, ambiguous edges, dictionary-only scope,
legacy ownerless non-geographic fields, alias compatibility, receipt isolation,
planner fast-path handoff, and unchanged time/query shape. Existing tests and
failure expectations were not weakened. Public stage-order regression passes.

Current stage: V1 scoped binding repair and verified-update rollout. V2
replacement readiness is unchanged; this release is not a cutover approval.
