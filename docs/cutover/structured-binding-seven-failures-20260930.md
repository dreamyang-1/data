# Remote structured binding regression closure

## Root cause and scope

PROVEN: the seven failures accepted for the previous time-only release were
tests against independently changed remote `structured_binding.py`, not seven
failed production SQL queries. The fresh remote source hash matched the prior
time release. The canonical local binder had not incorporated those independent
changes. This release reconciles that one file; it does not overwrite the
remote prompt builder, environment, semantic catalog, indexes or other services.

The structured parameters remain the only query-shape authority. The model may
select authorized catalog entities/fields/values, not extract the raw question
again or change time, aggregation, operators or requested filters.

## Classification of the original seven

| Test | Finding and disposition |
| --- | --- |
| `test_unbound_filter_is_not_hidden_by_subject_recovery` | STALE_TEST: a missing model binding is recoverable when the catalog has a unique matching field/value. Keep the negative assertion using genuinely missing catalog evidence; add positive recovery before subject resolution. |
| `test_missing_core_slot_is_explicit_not_silently_dropped[filters]` | STALE_TEST: same recoverable model omission. An unresolved source filter must still block; it must never disappear. The missing-metric case is unchanged. |
| `test_no_added_dimension_or_metric` | STALE_TEST: discarded model-only dimensions need not trigger a user clarification. Assert an empty final dimension list and an audit repair, not a spurious user parameter request. |
| `test_partial_display_keeps_existing_contract_but_missing_filter_blocks` | STALE_TEST: keep partial-display behavior and genuine filter-failure protection; allow catalog-grounded repair of an omitted binding. |
| `test_logical_group_identity_label_does_not_create_an_extra_group[hospital.amount-False]` | BUG: remote code promoted a display attribute into GROUP BY. Restore the grouping contract; a label already projected by its identity dimension remains valid. |
| `test_metric_subject_uses_published_source_dependency_not_model_choice` | BUG: remote binder omitted the published metric-source subject correction. Restore scoped `source_dependency.bind_entity` selection so involved-entity order cannot change the execution anchor. |
| `test_category_field_and_value_must_bind_as_a_pair` | BUG: an additional string-equivalence check rejected a valid scoped field/value selection. Remove the competing lexical veto; retain field, candidate-ID, cardinality and literal-code checks. |

The last issue also broke twelve name-filter execution variants in the broader
suite (short product names mapped to standard names, with =/!=/IN/NOT IN and
relationship review). Two scalar-query tests had the same stale expectation
that ignoring an unrequested model dimension/display field must cause a question.
These assertions were aligned with the user-approved structured-only contract;
the final ASL still cannot acquire those unrequested fields.

## Additional reproduction and repair in the same path

The declared-field fallback collected only values it could match. A partly
matched IN list could therefore lose an unknown or null member and execute a
narrower query. New tests reproduced this on the unmodified server. The fallback
now requires one catalog match for every input member, preserving order and
cardinality, and does not pre-delete nulls. Fully bound multi-value filters still
work; incomplete filters remain explicitly unresolved.

Preserved remote behavior has regression coverage: enum labels, logical-to-
physical filter binding, deterministic literal-date filters, metric HAVING,
catalog recovery and the previously deployed time compiler. No business-specific
name regex or extra question-analysis pass was introduced. Semantic name choices
remain model-assisted and can still be wrong semantically; being in the catalog
is not proof that every natural-language synonym is correct. Exact catalog
matches take priority and codes/model numbers cannot be replaced by similar text.

## Verification and test delta

- Reproduced remote original full suite: **1,175 passed / 21 failed**, no
  collection errors. An initial staging run also had a missing config-source
  path; fixing the test harness path removed that artifact without modifying
  production configuration or weakening the config test.
- New 28-case recovery suite against the unchanged server: **18 passed / 10
  failed**; five failures cover new audit evidence, the others reproduce subject
  and partial-filter faults. Against the candidate: **28 passed**.
- Full Oagnet suite, local and remote staged candidate: **1,224 passed**, zero
  failures/errors. Original cases: 21 fail-to-pass (including six explicitly
  documented stale assertions), zero pass-to-fail; 28 new passing cases.
- SQL Translator full offline suite: **512 passed**. No translator changes.
- DataAnalysis full regression: **4,529 passed**, including critical multiround,
  scope, API/SSE and final-output cases. Tests use a clean validation worktree,
  excluding the paused full-result-return changes.
- Installed-code release verification is recorded below after deployment.

## Change manifest and review

- Runtime: `Oagnet/structured_binding.py` only. Relative to the inspected remote
  source this is a narrow patch; the larger Git diff includes existing remote
  binding recovery, enum and filter support being reconciled into the repository.
- Tests: `test_structured_binding.py`, `test_scalar_metric_grain.py`, new
  `test_binding_recovery_contract.py`.
- Current stage: maintenance within existing execution routing, not a V2 cutover.
  No authorization, API/SSE, node order, database/vector publication or route
  changes; no inference of business authorization from model/catalog history.
- Review checked missing vs recoverable parameters, field/value pairing, exact
  literals, no new grouping, scoped subjects and paused-change exclusion.
- Deploy only after zero-failure installed-code tests; no reuse of the previous
  seven-failure release exemption. Back up the target, compare remote hashes,
  protect other code/configuration, and roll back on test or health failure.

## Deployment evidence

- Feature commit `7a05da3` pushed to the existing feature branch/Draft PR;
  no automatic merge. The paused three-file full-result-return work remains
  uncommitted and hash-identical, excluded from this release.
- Release `binding-seven-20260930-223727` backed up and replaced only
  `structured_binding.py`. Installed-code full suite: **1,224 passed**,
  zero failures; the prior seven-failure exemption was not used.
- Oagnet restarted on September 30, 2026, at **22:43:31 CST**, PID 2924398
  changed to 3073619; active, HTTP 200 / UP. DataAnalysis remained healthy,
  HTTP 200 / READY, without a restart. Configuration and other runtime modules
  were verified hash-identical.
- Installed binder SHA-256:
  `ae04441e42adae2f4dcd2cbca0bf8a5d8b5d0adc3b3d2be8a094f2b23906ae01`.
- Post-restart read-only API probes using the configured model/catalog passed
  for `2026年第一季度` and `2025年第四季度`: exact quarter boundaries and the
  business-date anchor agree between ASL and translated SQL, with no invented
  temporal grouping. These probes generated ASL/SQL but did not execute SQL
  or create a formal conversation. They do not assert a business result count.
