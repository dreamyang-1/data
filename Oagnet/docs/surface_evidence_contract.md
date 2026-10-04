# Advisory surface evidence

`POST /agent/query` accepts optional `surface_evidence.mentions`, each containing
literal `text` (1–300 characters) and optional `role_hint` (up to 80 characters).
At most 50 mentions are accepted. Unknown evidence/mention keys are rejected.

This is unbound model evidence, not a confirmed Intent-ASL contract. It cannot
declare catalog IDs, confirmed bindings, or semantic scope. Generation receives
the evidence as reference data; vector retrieval continues to use the query or
explicit retrieval_query unchanged. Existing scope and ASL validators remain.

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
