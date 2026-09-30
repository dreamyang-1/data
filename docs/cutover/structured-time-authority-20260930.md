# Structured time authority repair

## Evidence and first divergence

- PROVEN: the inspected production conversation has two first-quarter requests
  stopping at ASL binding with `未匹配到授权的时间字段`. Its structured time is
  `2026年第一季度`, and the upstream understood interval is already January 1
  through March 31. Fourth-quarter requests in that conversation completed.
- PROVEN: read-only recall against the current scoped server vector catalog
  returned both a first-order date and an order date but no business-time
  dimension for that structured quarter input. The time-dimension recall trigger
  returned false. It recognized selected natural-language relative phrases,
  rather than the presence of the structured time slot.
- PROVEN: the binder accepted model-supplied date ranges, relative amounts and
  calendar modes. Its grain repair required a recognized model grain; omitted,
  malformed and unrequested grains could still be lost or become clarification.
- UNKNOWN: the failed historical model's exact raw time object is not retained in
  the inspected response cache. Do not claim a particular hallucinated field.

## Changes and boundaries

- `Oagnet/structured_time.py`: compile only the planner's declared time slot;
  calendar quarters, years, months, days, explicit ranges, half-years, rolling
  periods and calendar-relative periods use deterministic date arithmetic.
  Unsupported/ambiguous expressions require explicit bounds, not invented dates.
- `Oagnet/structured_binding.py`: remove model date/grain write authority from
  the prompt and assembly. Model time fields other than a catalog anchor are
  ignored. Aggregation grain comes from the declared grouping only; date detail
  columns and ordinary dimensions never acquire a model-generated grain.
  Published time-dimension mappings can supply their authorized physical fields.
- Anchor binding uses explicit declarations, selected metric time caliber,
  selected time dimensions and reachable published time fields. It does not pick
  an arbitrary alphabetically first date or a field merely containing `created`.
  Genuine catalog/meaning gaps explain what is missing without asking for the
  already known interval again. Catalog field disambiguation remains a model
  capability; dates, operators, limits and sorting direction do not.
- `Oagnet/prompt_build.py`: structured time slots trigger same-scope time-dimension
  recall regardless of spelling/period; recalled temporal candidates are not
  discarded by unrelated top-k labels. No scope expansion or index rebuild.
- `Oagnet/query_binding_review.py`: delete unused model-controlled `_time_value`.
  Existing filter ownership review is unchanged.
- No API/SSE schema, node order, runtime routing, business SQL translator, result
  pagination, semantic database or vector data was changed.

## Validation and delta

- New time-contract tests: 86. Against old binding/recall, 57 fail and 29 pass;
  after the repair all 86 pass. Covers missing/wrong/malformed model time,
  Q1/Q4 follow-ups, range versus grouping, month/quarter/year/week units, leap
  dates, month ends, absent/default time, authorization and other parameter values.
- Oagnet focused contracts: 230 passed. SQL time/snapshot/hardening: 130 passed.
- DataAnalysis critical/API/task DAG: 259 passed; full suite: 4,504 passed.
- Oagnet full baseline: 1,108 passed, 2 failed. Final: 1,194 passed, the same 2
  failed; no collection errors or unresolved new regressions.
- STALE_TEST: the missing-anchor fixture now actually lacks an authorized time
  field, instead of expecting a bad model field to block a uniquely bindable
  declared range. The relative-time integration test fixes today's date rather
  than trusting the model's historical hardcoded start/end. Assertions remain.
- Read-only server candidate probe uses the real scoped vector catalog, model
  and SQL translation service for both requested quarters. Both ASLs bind
  `sales_order.created_date`, retain the single name projection, and translate
  into the correct half-open quarter boundaries. This checks ASL/SQL time
  alignment, not business-data execution or all join semantics.
- Production has additional independent edits in the same binder. A temporary
  candidate preserves those edits and replaces only the time path for the probe;
  it also removes the old relative-time/first-date fallback. No active service
  file was replaced and no service was restarted.

## Release blocker and remaining scope

Publishing is stopped under the repository's test-failure rule. Two pre-existing
`test_surface_literal_constraints.py::test_explicit_codes_letters_models_keep_field_and_literal`
cases for product specification literals still fail on the unchanged baseline.
They target a legacy literal-preparation helper and are not silently waived or
changed to make the suite green. Resolve that blocker separately before pushing,
deploying or restarting this repair. No production cutover is involved.

Paused full-result-return work in the HTTP adapter and SQL export files is not
included. Temporary server probes/cached diagnostics are not release artifacts.
