# Chart business-metric grounding and visible values

## Scope and root cause

- PROVEN: the explicit-object comparison analyzer returns `metric_columns` and
  `metric_summaries`; the chart builder previously consumed only `metric_column`.
  Its fallback selected the first numeric SQL column, including province codes.
- PROVEN: SVG bars/points exposed their values only in tooltips. Horizontal bars
  discarded the sign, scatter plots used ordinal X coordinates, and bar rendering
  silently stopped at 30 rows despite a chart specification containing up to 200.
- The fix is limited to deterministic chart selection, SVG rendering and the MCP
  projection of that same chart. SQL, ASL, authorization and dataset processing
  are not changed.

## Behavior and review

- Consume single/multiple/per-metric analyzer evidence. Codes, IDs, keys and ranks
  are dimensions/identifiers, not measures; numeric counters remain valid.
  Respect the confirmed object label. If measure evidence is absent, accept a
  unique eligible numeric column; do not guess among multiple business columns
  or substitute another field for a declared but missing metric.
- Multiple confirmed metrics generate separate specifications. Existing public
  `ChartSpec`, API/SSE shapes, 200-point bound and three-chart display cap remain.
- Bar, line and scatter values are visible text retaining source precision;
  pie legends show exact totals as well as percentages. Negative bar directions
  and numeric scatter distances are preserved. Large bar charts use an adaptive
  horizontal layout and render every point in the bounded specification.
- A pie with negative values or more than eight categories becomes a bar chart;
  a directly supplied invalid pie is not rendered. No silent positive-only pie
  or invented “other” category. Null/nonfinite/bool values are never plotted.
- SVG text remains escaped/inert. Local and MCP numeric parsing treats percentages
  consistently. MCP gets only selected bounded fields, never scope or full data.
  Label flags are added only if declared by the discovered tool schema; tools
  without them keep their existing argument contract. Third-party renderer
  appearance remains tool-dependent; no unsupported style arguments are invented.
- Direct multi-Y instructions are not silently reduced to their first field;
  deterministic generation uses one metric per specification instead.
- REVIEW: no change to correct SQL/ASL bindings, entity ownership, result cleanup,
  query limits, multi-series line grouping or MCP-first/fallback sequencing.
  The seven frozen user-visible stages remain covered by the Critical Suite.

## Verification and test delta

- Clean canonical baseline: 4,619 passed, no collection failures.
- 54 new offline cases cover the reported order-count comparison plus three
  analogous questions through AnalysisEngine, chart building, SVG and MCP data
  projection; all intents, fact shapes, numeric/text codes, legitimate counters,
  multi-metric and missing-metric cases, exact Decimal labels, zero/negative values,
  percent/nonfinite values, all 200 bars, scatter geometry, pies (including one
  nonzero category with zero-value legend entries) and escaping.
- Local targeted chart/analysis plus Critical Suite: 502 passed.
- Visual QA: headless browser rendering inspected for the original comparison,
  line, negative horizontal bars, pie and numeric scatter.
- Initial remote before/after verification both encountered the same three
  fixture errors: `SynthesisStub` did not accept the server's existing optional
  `semantic_model_id` synthesis parameter. Confirmed against the running source;
  the test stub now accepts that optional parameter. Assertions are unchanged;
  no production synthesis/orchestration source is overwritten or bypassed.
- Final full regression: 4,673 passed; collection errors 0. Baseline 4,619 → final
  4,673; old-pass → new-fail 0, old-fail → new-pass 0, new cases 54. No assertion
  is removed or relaxed (`STALE_TEST` assertion changes: none).
- Final staged remote verification: baseline 448 passed; candidate 502 passed.
  The same frozen contract tests are used on both sides, with the optional
  synthesis test parameter compatibility described above. No new failure.
- Deployment verified: both production module hashes match the tested candidate;
  remote module drift was absent. Backup created, affected service restarted with
  a new worker, `/ready` returns `READY`, public OpenAPI schema unchanged, protected
  configuration and unrelated Python source unchanged. Installed-module smoke
  verifies business-metric selection, visible values, bar geometry and MCP payload.

## Explicit change manifest

- `app/analysis/visualization.py`
- `app/services/extension_dispatcher.py`
- `tests/test_chart_business_metrics.py` (new)
- `tests/test_analysis_orchestration.py` (test fixture compatibility only)
- `docs/cutover/chart_business_metrics_closure.md` (this report)

Only the first two production modules require deployment/restart. Remote hashes
and differences are checked, backups created, configuration and unrelated source
protected, public schema compared, and health/new worker checked after restart.
No credentials, logs, generated previews or backups enter Git.

## Stage / remaining boundaries

Current Stage: `V2_CONTEXT_V1_EXECUTION` unchanged. This is a scoped chart repair,
not a V2 cutover readiness declaration. Catalog/Evaluation/Shadow gaps and existing
V1 replacement blockers are not resolved by this patch. No index rebuild, semantic
database mutation, production cutover or automatic PR merge is performed.
This scoped repair is verified and deployed. Git closure uses an explicit-file
commit on the existing feature branch and an update to the existing Draft PR;
the PR remains unmerged. New queries generate corrected charts; stored historical
answers are not rewritten. Broader V2 readiness work remains out of scope.
