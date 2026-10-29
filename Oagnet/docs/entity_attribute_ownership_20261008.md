# Entity / attribute / enum ownership closure

## Proven defect and repair boundary

PROVEN by offline reproduction: when dealer and hospital both declared a
same-named attribute and enum label, the structured binder accepted a model's
hospital field for a parameter explicitly owned by dealer. Catalog records
already contained parent identity; exact-label selection and predicate review
did not consistently retain it. The SQL subject is not the owner of every slot.

The repair follows the active Oagnet structured-extraction execution path.
Pinned V2 value resolution already checks model/domain/entity/attribute identity;
its contract is unchanged. No DataAnalysis or SQL Translator source is changed.

## Behavior

- Recall explicit slot owners and their attributes/values within the current
  authorized model/domain. Keep broad recall for existing cross-field recovery.
- Resolve each display, grouping, filter and sort slot against its own entity.
  Entity code/ID precedes name/alias; colliding aliases are not unique identity.
- Restrict standard-value selection, including model-provided value IDs, to the
  selected attribute's parent. Preserve global value IDs and multi-value cardinality.
- Preserve multiple logical owners of one physical field; do not silently keep
  only the last attribute/enum declaration. Independent attribute records work too.
- Resolve an explicit shared-dictionary owner through published foreign keys.
  Repeated copies of the same executable edge do not create artificial ambiguity.
- Retain source-filter indices when aggregate thresholds become HAVING, so a
  later dictionary predicate is reviewed against the correct structured slot.
- Exact catalog evidence corrects wrong/missing model bindings. Genuine same-name
  ambiguity without entity/value evidence remains a clear clarification, not a guess.
- Existing unique model/spec literal recovery remains available; no new business
  requirements, time grains, metric definitions or authorization are inferred.

## Change manifest and review

`binding_ownership.py`, `structured_binding.py`, `query_binding_review.py`,
`prompt_build.py`, `agent.py`, `tests/test_binding_ownership.py`, this document.

Remote-only short-name recall, projection DISTINCT behavior and the existing
identity-name pattern were preserved by clean three-way merge and synchronized
locally. They are not newly designed behavior in this repair.

Review covered ownership versus execution subject, stable value IDs, shared
physical mappings, standalone attributes, repeated relations, filter/HAVING
index correspondence, invalid model keys and model/domain isolation. Existing
tests were not weakened. Public API/SSE schemas, node order, conversation/Pending
handling and production runtime mode remain unchanged.

## Validation

| Suite | Baseline | Final |
| --- | ---: | ---: |
| Oagnet local full offline | 1,266 passed | 1,312 passed |
| Oagnet server staged full offline | 1,266 passed | 1,312 passed |
| DataAnalysis critical integration / scope / conversation / node-order suite | — | 335 passed |

46 new ownership cases; zero old-pass → new-fail, zero collection errors.
The server has one existing FastAPI/Starlette dependency deprecation warning.
Tests use synthetic catalog data and mocked database/vector clients; these counts
do not assert correctness of untested production business data.

## Release and remaining boundary

Release is restricted to the affected Oagnet service, with explicit-file hashes,
pre-install drift checks, recoverable backups and protected configuration.
Native indexes and semantic database contents are not rewritten.

Current stage remains the existing context/V1 execution runtime, not V2 cutover.
Catalog/evaluation/shadow release gaps are not closed by this binding repair;
V1 replacement readiness is unchanged. If published parent metadata is missing
or conflicting, the next step is a scoped catalog diagnosis, not an inferred
owner or an automatic index rebuild.

The affected service was deployed and restarted on the agreed server. A fresh
active worker and all runtime file hashes were verified; Oagnet vector health
is healthy, DataAnalysis reports READY and SQL Translator reports ok. Public
OpenAPI schema, unrelated source files and protected configuration hashes are
unchanged. Recoverable backups are retained. No vector rebuild was invoked.
