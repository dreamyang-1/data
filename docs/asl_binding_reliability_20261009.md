# ASL binding reliability — 2026-10-09

## Evidence and bounded repair

PROVEN: catalog and relationship binding previously caught every model-call /
JSON error and returned a business ambiguity. The ambiguity renderer then asked
the user to supplement an already complete question. The original historical
exception was swallowed; its exact type remains UNKNOWN, not an asserted timeout.

PROVEN: a real-catalog replay bound the person name and channel enum correctly,
but relationship review treated their multiple table consumers as multiple
business owners. Adding explicit owners made the controlled replay pass. A
table's consumers are not owners of every attribute on that table.

Oagnet now distinguishes dependency failures from missing business information.
Catalog model, relationship model and required dictionary lookup failures carry
a safe code, stage, error type and retry classification. No raw exception,
prompt, reply or credential enters that public diagnostic. The API returns a
failure HTTP response rather than a successful ASL with misleading ambiguity.
The existing capacity/HTTP boundaries retain bounded transient retries;
authentication, invalid-output and unknown deterministic failures are not retried
by the Agent HTTP boundary. Valid fenced/text-block JSON and existing null-list
deterministic recovery remain supported. There is no new model retry loop.

Ordinary predicate ownership is determined from the current authorized catalog's
unique attribute/name/alias ownership, including exact dimension names/aliases
with uniquely governed physical mappings. A second model call or dictionary/FK
conversion is unnecessary for that case. Person, hospital, dealer, product,
manufacturer, department, company and order-enum predicates use the same rule.
Same-named fields on different entities, invalid explicit owners, shared
geography and ambiguous published relationships keep their existing conservative
behavior. Subject/grouping cannot choose an owner. All query-shape parameters and
standard values remain unchanged. Partial multi-value dictionary lookup does not
silently discard unmatched values or broaden the query.

The Agent renders typed failures as system failures, not requests for user
parameters. No new Pending, candidate list or empty analysis template is created.
Its HTTP and presentation changes do not compensate for SQL or semantic errors.

## Exact change manifest

- Oagnet runtime: `binding_errors.py` (new), `structured_binding.py`,
  `query_binding_review.py`, `api.py`.
- Agent runtime: `app/adapters/http.py`,
  `app/services/dependency_error_messages.py`.
- Tests: `Oagnet/tests/test_binding_service_failures.py`,
  `Oagnet/tests/test_direct_predicate_ownership.py`,
  `Oagnet/tests/test_query_binding_review.py`,
  `tests/test_binding_service_errors.py`.
- This document. SQL Translator and the Java backend have no code changes.

One old assertion is explicitly marked STALE_TEST: a relationship-service timeout
must raise a typed service failure instead of creating business clarification.
The original test identity is retained for baseline/candidate comparison. During
full regression an existing null-section recovery case exposed an over-strict
new check; that check was corrected, not the old recovery assertion.

Tests cover transport/format variants, wrapped failures, database failures,
authentication, throttling and bounded retries; safe error envelopes/logging;
all four equality/membership operators across eight attribute owners; aliases,
homonyms, unresolved dimension mappings/owners, shared regions, reordered/split filter origins,
partial IN lookup, unchanged query shape, and public planning-before-parsing
order on service failure.

## Verification and release

Pinned canonical full baseline: `ea66014`. Candidate source is that snapshot plus
only the exact manifest, not an unverified whole-directory working-copy upload.
Concurrent output-introduction/ranking changes are preserved and not part of
this repair. Test and query artifacts remain private, outside version control.

Server-isolated critical suite: Agent 56 passed; Oagnet 615 passed. There are 110
new regressions in total (Agent 24, Oagnet 86). Initial full baseline/candidate:
Agent 4,623/4,647 passed with the same 96 old failures and one old collection
error; Oagnet 1,532/1,608 passed with the same 11 old failures; SQL 647 passed
on both versions. No new failures or removed cases. A first compatibility guard
stopped deployment when an unrelated server planner changed. That planner was
preserved byte-for-byte, overlaid identically on isolated baseline and candidate,
and the complete Agent comparison was rerun with identical results. It was not
copied over or changed by this repair.

An additional real-catalog replay exposed a dimension-label variant not covered
by the initial ordinary-attribute fixtures. The follow-up reuses the existing
governed dimension-to-field resolver and rejects unresolved alternatives. The
original ownerless person/channel extraction then passes real-catalog/model
binding without another relationship-model call; filters, metric and year are
unchanged. Follow-up full comparison against the verified deployed predecessor:
Oagnet 1,608/1,618 passed with the same 11 failures; Agent 4,647 passed with the
same 96 failures and one collection error; SQL 647 passed. No new failures,
collection errors or removed cases. The initial six-file rollout restarted
Agent/Oagnet, and the one-file dimension follow-up restarted Oagnet only. Both
rollouts used exact-file backups and fresh-worker/health/configuration guards.

Canonical functional commits: `19a4289` (Oagnet failure/ownership repair),
`3476de6` (Agent transport/presentation), `6bcc8e6` (dimension-label follow-up).

Final native ingress checks: 4/4 completed, covering the original question,
an explicit three-value channel IN query, the literal "other" channel enum,
and a hospital-name query. Executable name predicates, standard enum codes,
2025 boundaries, selected metrics, SQL execution and public stage order pass.
The original/other scalar sums are effective empty results (one SQL NULL row),
not binding/execution failures. These checks prove binding/execution, not
independent business totals or complete-year coverage. The ownerless legacy
extraction also passes against the deployed Oagnet with one catalog-model call
and no relationship-model call, retaining both predicates and the year.

Post-native read-only audit confirms all six repaired runtime files match the
canonical committed hashes, protected configuration is unchanged and all three
services are healthy. The unrelated server planner continued changing during
this task and its post-native hash differs from the deployment-time snapshot.
It was not overwritten or committed here. The native results do not establish
full-regression equivalence for that independently evolving planner; its current
colloquial-filter behavior is OPEN / NOT VERIFIED. Draft PR #91 is unmerged.

Existing reproduced failures are retained under the user's explicit
no-new-failure waiver; this repair does not claim a full-suite pass. The bounded
PR port's affected tests pass separately (Agent 34, Oagnet 206); the canonical
full counts above are not a full-regression claim for that divergent PR base.

Known separate issue: an exploratory colloquial three-channel grouping question
omitted its channel filter in task planning and returned an additional channel.
This happened before ASL and is not reported as a passing case or fixed by ASL
rewriting parameters. The final multi-value binding test uses explicit predicate
wording. The independently modified server planner is preserved, not silently
replaced. This upstream extraction issue remains open outside the binding fix.

The native verifier also distinguishes the existing empty-result branch from
nonempty analysis: a scalar SUM may return one SQL row containing NULL and be
reported by the Agent as zero effective rows. Such a result must not require
fabricated validation/insight sections. The initial verifier's raw-row-only
assertion was corrected; production node names/order were not changed.

No semantic writes, index rebuild, configuration replacement, new authorization,
Java deployment or V2 cutover is included. Current stage remains V1 maintenance;
Catalog/Evaluation/Shadow gaps and V1 replacement readiness are unchanged.
