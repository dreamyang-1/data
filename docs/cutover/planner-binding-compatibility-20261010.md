# Planner API compatibility and equivalent slot bindings

## Cause and boundary

- PROVEN: the local planner exposes `plan`, `deduplicate` and `execution_layers`
  on an instance. The independently updated deployment exposes
  `route_and_plan` / `plan_with_judgment` and module-level DAG helpers. A legacy
  class import/call is not a compatible release boundary.
- PROVEN: models 120 and 121 publish both the hospital-grade dimension and its
  hospital attribute, with the same physical column and attribute identity.
  Exact-name binding counted the two representations as different meanings.
  Existing ownership checks addressed different entities, not this duplicate.
- PROVEN: model 81 has a different hospital-grade mapping (hospital code).
  It is not equivalent to the grade attribute. This release does not rewrite
  semantic data or excuse conflicting mappings.

## Changes and review

- Add an internal planner adapter for both interfaces. Reuse an existing
  judgment, retain the router's final judgment and extraction, pass the current
  model/context unchanged, and use the existing DAG implementation.
- Remove the orchestrator's dependency on the removed legacy planner export.
  Do not replace deployed planning, dependencies, model classes or prompts.
- Collapse a dimension/column collision only with consistent published field,
  entity and attribute identity. Keep distinct entities, metrics, conflicting
  mappings, hierarchy, grouping grain and special-rule dimensions ambiguous.
  Apply at shared exact-name and alias binding, covering grouping and sorting;
  existing filtering/projection contracts remain protected by regression.
- STALE_TEST: the preplanned calculation-routing test imported another test
  module and its removed legacy planner. Replace only that fixture with the
  already established two-layer plan; all calculation/constraint assertions
  remain unchanged. No production calculation behavior changed.
- The release also includes the previously verified completed-root-question
  validation changes from `303b525`, merged onto the independent deployment.

## Verification

| Environment / suite | Baseline | Candidate |
| --- | --- | --- |
| Local Agent full | 4811 pass / 95 fail / 1 collection error | 4818 pass / same 95 fail / same 1 error / 1 skip |
| Local Oagnet full | 1623 pass / 25 fail | 1647 pass / same 25 fail |
| Deployed-runtime Agent snapshot full | 4467 pass / 111 fail / 13 collection errors | 4496 pass / 107 fail / 12 collection errors |
| Deployed-runtime Oagnet snapshot full | 1621 pass / 28 fail | 1645 pass / same 28 fail |

- Local Critical: 382 pass, 1 skip (native new-router module is absent locally).
  Oagnet targeted: 357 pass. The actual deployment snapshot verifies the new
  router, rather than treating the local skip as verification.
- Deployment-snapshot Critical: Agent 229 pass; Oagnet 357 pass. No failures or
  collection errors in either Critical suite. Zero newly failing or removed
  cases in either full comparison. One failed collection module recovered;
  its former collection-error identity is not a removed behavioral test.
- Local Oagnet full runs exclude the previously established Windows native
  Chroma concurrency crash; Linux full runs do not exclude it. All offline
  vector stores are isolated temporary test stores, not production fallbacks.
- Read-only real Milvus recall/binding: original brand/grade question, dimension
  alias, explicit hospital parent and alias+sorting variant all pass. No input
  extraction mutation, vector writes, index rebuild or semantic DB writes.
- Native original question: COMPLETED, four grade groups, no ambiguity block,
  all seven public nodes in order. Its metric is normalized to the published
  tax-inclusive total; literal question comparison must account for this
  existing canonical-metric normalization.
- Additional native average-order question: PARTIAL_SUCCESS; the calculation
  child failed. Root validation still uses the complete question, retains
  failure/coverage limits and precedes insight. This is NOT evidence of a
  successful average calculation; that separate limitation is out of scope.

## Release

Exact deployment manifest: Agent `app/planning/compat.py`,
`app/services/orchestrator.py`, `app/analysis/synthesis.py`,
`app/presentation/reliability.py`; Oagnet `structured_binding.py`.
The deployed orchestrator preserves independent planning/display/pending edits;
it is a reviewed merge, not a whole-file local overwrite. Before/after hashes,
isolated reports, native responses and backup receipts are retained privately.
Only the Agent and Oagnet services were restarted; readiness/vector health
passed, workers changed and protected runtime/configuration hashes matched.
SQL service, Java backend, semantic/index data and V1/V2 routing were not changed.

Current Stage: scoped V1 compatibility repair, not V2 replacement readiness.
Known full-suite failures remain under the user's no-new-failure release waiver.
Other worktree changes are excluded from this change manifest and deployment.
