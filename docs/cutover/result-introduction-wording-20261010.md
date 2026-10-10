# Natural final-answer introductions

## Proven cause and scope

The presentation helper removed a leading `分析` from `分析下...`, leaving a
sentence fragment. It also appended `分析结果如下` to complete user requests,
including chart instructions and unanswered interrogatives. Both highlighted
sentences are reproducible directly from `result_introduction`; this is not an
ASL, SQL or model-generated data-summary failure.

Replace only that shared helper's question-splicing behavior with a short
`分析结果如下：` or `查询结果如下：` lead. Preserve its existing analysis-mode
selection. Completed-question context and the factual short summary remain
with the original report/synthesis consumers. No extra model call, business
fact generation or attempt to rewrite arbitrary user language is introduced.

## Manifest and review

- Runtime: `app/presentation/summary.py`, only `result_introduction`.
- Tests: `tests/test_result_introduction_wording.py`,
  `tests/test_final_output_preferences.py`, `tests/test_task_dag.py`.
- This report is documentation only.
- Shared-helper consumers cover single results, composite reports, previews
  and successful empty results without changing their surrounding branches.
- AST review confirms `brief_summary`, imports and all other module behavior
  are unchanged. No orchestrator, planner, chart, query, Scope, pending,
  failure, API/SSE or public-node behavior is changed.
- Independent server planner/orchestrator/legacy-guard edits are preserved
  and included in both isolated test versions; none is shipped by this task.

## Validation and test delta

- Local focused tests: **153 passed**. Local Critical Suite: **211 passed**.
- Server Critical Suite: **211 passed**, no failure or collection error.
- Full isolated server baseline: **4722 passed / 93 failed / 1 collection
  error**. Candidate: **4741 passed / 93 failed / 1 collection error**.
- **Zero old-pass to new-fail**, zero old-fail to new-pass, **19 added case
  identities**, zero removed identities and zero new collection errors.
- STALE_TEST: seven pre-existing opening-prefix assertions explicitly migrate
  from awkward question-plus-suffix output to the user's requested fluent
  wording. Summary facts, completed-question selection, table values, chart
  types/metrics, diagnostics and empty-result assertions remain protected.
  Composite completed-question context is additionally asserted.
- The new tests cover both reported questions, colloquial requests, multiple
  clauses/chart instructions, alternate chart types, ranking and comparison,
  plain queries, time follow-ups, whitespace, empty strings, explicit analysis
  mode and preservation of report context/summary/table/chart.
- Full regression is **not all green**. The 93 failures and one existing
  collection error remain under the user's verified-old-failures/no-new-
  failures deployment waiver; this wording repair does not resolve them.

## Rollout

One exact runtime file deployed after baseline/candidate comparison and target
hash checks. Backup retained; only the Agent restarted. Fresh worker/readiness,
deployed source hash, protected non-target source and configuration checks all
passed. Development-to-canonical synchronization uses an explicit file list
and SHA-256 comparison, without environment files or raw business evidence.

Native ingress replay of the product-ranking/chart question passes the lead,
table/chart presence and all seven public-node ordering checks. Its existing
`PARTIAL_SUCCESS` status is retained; this is a presentation/chain check, not
an independent business-answer gold or a claim that all warnings are resolved.

The salesperson/channel original question also passes on a separate retry:
`COMPLETED`, natural lead, returned table and all seven public nodes in order.
Its first concurrent attempt returned `REQUEST_TIMEOUT` after 120 seconds at
ASL generation, before this helper is invoked. That failed attempt is retained
as evidence, not counted as a pass. The successful retry took approximately
52 seconds; this task does not claim to fix the separate transient timeout.

## Readiness

Current Stage: scoped V1 presentation maintenance. No new current-task blocker;
unrelated full-regression failures remain. Catalog/Evaluation/Shadow gaps and
V1 Replacement Readiness are unchanged; no V2 cutover or automatic PR merge.
