# Advisory surface evidence

`POST /agent/query` accepts optional `surface_evidence.mentions`, each containing
literal `text` (1–300 characters) and optional `role_hint` (up to 80 characters).
At most 50 mentions are accepted. Unknown evidence/mention keys are rejected.

This is unbound model evidence, not a confirmed Intent-ASL contract. It cannot
declare catalog IDs, confirmed bindings, or semantic scope. Generation receives
the evidence as reference data; vector retrieval continues to use the query or
explicit retrieval_query unchanged. Existing scope and ASL validators remain.

## Completed question and structured reference

`completed_question` carries the conversation-completed business question. It
is the primary semantic input for ASL planning; callers do not need to send the
raw original turn separately.

`structured_reference` carries the preceding intent extraction (`entity`,
`metrics`, `dimensions`, `fields`, `filters`, `operators`, and `time_range`). It
is advisory only. Oagnet adds its labels to bounded semantic recall, maps each
semantic field to exactly one record in the current vector scope, and omits
unmatched or non-unique items before they reach the ASL prompt. Omitted items
are reported as `DROP_UNVALIDATED_STRUCTURED_REFERENCE` repairs.

The final ASL is checked again against the vector-authorized metric, entity,
dimension, and physical-field set. Source-catalog fallback may normalize a
literal value, but it cannot authorize a final field absent from vector scope.

### Role-independent entity value recall

An upstream role hint such as product, brand, manufacturer, or specification
does not restrict entity-value retrieval to one predicted attribute. Oagnet
searches entity attribute values across the pinned semantic model/domain,
prioritizes exact canonical mention matches, and keeps a wider hidden candidate
pool for deterministic disambiguation while exposing only a compact reranked
set to generation.

If source/catalog evidence later repairs a value onto a field that similarity
top-k did not initially authorize, Oagnet performs a second exact vector-store
lookup for that `source_field` in the same model/domain. Only an exact scoped
vector record may extend the request-local field authorization; unavailable,
missing, or cross-scope evidence remains fail-closed.

The field is optional for backward compatibility. The Agent caller has not yet
switched to this route: do not claim end-to-end migration or enable deferred
binding until caller post-processing and confirmed-choice propagation are ready.

Validation: 113 focused tests passed, including exact-question API propagation,
unchanged retrieval input, rejected binding/scope keys and scope regressions.

Full offline comparison before the final retrieval-only regression was added:
baseline 709 passed / 8 failed; candidate 714 passed / the same 8 failed.
Two failures need vector-store test doubles; six need mocked catalog database
reads (MYSQL_HOST is intentionally absent offline). No new failing test was
observed. The final retrieval regression passed in the 113-test focused run.
Running pytest without an explicit `tests` path also collects backup files and
causes a duplicate-module collection error; full-suite commands use `tests`.
