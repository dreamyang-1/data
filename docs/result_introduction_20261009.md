# Question description at the start of results

PROVEN: ordinary query output can start directly with a table, while analytical
and combined results start with report sections. None consistently introduces
the question being answered before the result.

The final presentation now begins with the completed question's scope followed
by `查询结果如下：` or `分析结果如下：`. Common request prefixes, terminal
question punctuation and terminal `是多少` / `是什么` / `有哪些` are removed
for declarative wording. Analytical intent is passed from the existing request;
wording such as rankings also selects the analytical label for combined reports.
This formatting never changes parameters, metric definitions or result cells.

The completed question is used for contextual follow-ups. Query tables, reports,
combined deliverables, empty successful queries and bounded previews are covered.
Existing failure/clarification branches continue to explain the missing result.
There is no extra model call and no API/SSE schema or node-order change.

Exact manifest: `app/presentation/summary.py`, `app/analysis/interpretation.py`,
`app/services/orchestrator.py`, `tests/test_final_output_preferences.py`,
`tests/test_task_dag.py`, this file.
Unrelated development and server changes are preserved. Only the bounded patch
is ported to the functional branch, whose baseline may differ from the deployed
integration checkout.

Verification: the initial affected suite passed 91 cases; final affected output,
root-report, API/node-order, critical and binding-notice suites pass 182 cases in
the deployment checkout and 185 in the functional checkout. Two new integration
cases exercise the requested sales-quantity and highest-sales product wording;
existing chart, follow-up, root and empty-result cases also assert the prefix.
The expanded integration suite has the same seven failures before and after
the repair (236 baseline passes, 238 final passes).

The functional suite including the combined-task opening contract passes 186
cases. STALE_TEST: one old combined-task assertion required a report heading at
character zero; it now requires the requested question description immediately
before the same heading. The original report-content assertions remain intact.

The full baseline run has 4,619 passes, 98 failures and one collection error
(`test_no_default_time.py`, missing legacy `extraction_user_prompt`). The initial
post-change full run has 4,619 passes, 100 failures and the same collection error.
All 98 baseline failures are retained. One additional failure is the requested
opening-contract assertion, corrected and passing above. The other is
`test_surface_only_choice_restores_full_task_before_planning`: with identical
PYTHONHASHSEED=0, both original-source and changed-source isolated runs reproduce
the same `NoneType.plan` error before result generation. This is a baseline
variation, not repaired or silently omitted. No unexplained new failure remains;
this is not a full-suite pass. Original source is loaded in memory from Git,
without replacing development files or copying environment credentials.

Deployment updates only the three runtime modules by checked patch, after
confirming their remote baselines. Exact-file backups and configuration/service
hash protection are retained. The first remote harness run found absent remote
test files before collecting any tests; the patch was restored and the existing
service restarted. Required test modules and fixtures were then staged in an
isolated backup subdirectory. The second run against actual remote application
sources passes 183 cases. DataAnalysis alone was restarted: active, NRestarts=0,
READY with all readiness profiles true, unchanged runtime mode and protected
configuration. Oagnet/SQL, semantic assets and indexes were not modified.

Native SSE verification on authorized model 81/domain 205 completes with no
stream error in 43.4 seconds. The final answer's first line is exactly
`2025年上海市所有医院的销售订单总数量查询结果如下：`. Intent, planning,
ASL parsing, SQL execution, reliability and insight events precede final output.
An earlier probe using legacy model 113 returned a catalog-incomplete fallback
before planning; it is not counted as a successful query, and no success intro
was fabricated for that fallback. Only validation metadata and question wording
are recorded, not business rows or credentials.

Current stage is the bounded result-presentation change. Catalog/Evaluation/
Shadow gaps and V1 replacement readiness are unchanged; no V2 cutover or
automatic PR merge is included.

## Remove the redundant final analysis explanation

The user requested removal of the paragraph below the result table reporting
`排名对象为空或重复，SQL必须按唯一对象聚合`. PROVEN: the optional
analysis engine rejects duplicate display names and the final renderer appends
that diagnostic even though the original query table and chart remain useful.
Suppress only this diagnostic in the final explanation; do not change query
execution, analysis safeguards, progress, reliability/status or diagnostic
evidence. Preserve the question introduction, table and chart.

A new real-engine regression uses equal product names with different codes,
requests TOP_N and a bar chart, and verifies the introduction, both code rows,
SVG and reliability diagnostic. No existing assertions were weakened.
The targeted presentation, critical, API and warning-preservation suite passes
192 tests, including truncation, insufficient forecast data and unavailable
profile-field warnings.

The first broad-removal candidate introduced three regressions in those warning
categories compared with the frozen baseline, so it was rejected before release.
The implementation was narrowed to the screenshot's exact duplicate-ranking
diagnostic. Corrected complete offline regression: baseline 4,727 passes / 19
failures; final 4,728 passes / the identical 19 failures. No collection errors,
removed cases, old-pass to new-fail or old-fail to new-pass transitions. One
new regression passes. These retained baseline failures are not a full-suite
pass. Two additional Windows integration runs encounter the existing 30 ms
telemetry-delivery timeout (186 passes / one failure); the same case passes
both isolated reruns, the 192-test functional run, complete offline regression
and server run. No timing assertion or progress code was changed.

The exact three-file follow-up manifest is `app/services/orchestrator.py`,
`tests/test_final_output_preferences.py` and this report. Their development
and integration SHA-256 values match. Code review confirms a single runtime
conditional change, with no neighboring behavior or test expectations removed.

The remote baseline matched the prior committed integration source. A checked
one-hunk patch and exact-file backup were used; isolated tests against actual
remote application code pass 187 cases. Only DataAnalysis was restarted and is
active/READY with all readiness profiles true, no restarts and the same runtime
mode. Protected configuration, service unit and adjacent presentation sources
are unchanged. A real model-81/domain-205 native SSE request for highest-sales
product analysis with a bar chart completes in 51.9 seconds with no stream
errors, the expected question introduction, result table and chart, and no
duplicate-ranking explanation. PARTIAL_SUCCESS is intentionally retained:
display suppression does not claim the optional analysis succeeded. The first
smoke harness incorrectly required COMPLETED; its status assertion was corrected
to the unchanged partial-success contract, then the real request was rerun and
passed. Query, planning, parsing, execution, reliability and insight event order
remain intact. No business rows or credentials are included in this report.
