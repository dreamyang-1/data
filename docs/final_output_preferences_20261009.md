# Final summaries and explicit chart preferences

## First divergence and bounded repair

PROVEN: single-query synthesis receives the completed question and complete
available query data, but its final presentation metadata was not consumed.
Ordinary queries could therefore end with a table alone. Root reporting also
used a generic placeholder when optional model metadata was absent despite an
available task summary.

PROVEN: automatic chart selection consumes analytical intent, not the user's
explicit presentation preference. Plain metric/detail requests had no charts;
the renderer did not support trees. One successful MCP image could suppress all
remaining inline charts, and positional URL matching could mislabel a later
successful metric as the first metric.

Eight initial reproductions failed with original sources after correcting a new
test fixture's required timestamp. No existing assertions are changed.

## Implemented behavior and review

- Single and combined synthesis request a separate one-to-three-sentence final
  overview from the completed question and actual data, in the existing model
  call. Detailed insight stays in its node. Single final output consumes that
  overview; absent metadata/model failures use a short deterministic summary.
  Root fallback describes selected deliverables, not every intermediate task.
- Existing no-result, failed-task and clarification terminal behavior remains:
  no invented result, empty analysis template or fake successful summary.
- Explicit bar/column, horizontal bar, line, pie, scatter and grouping-tree
  requests override automatic chart choice. Registered Chinese/English chart
  names and planner output wording are presentation preferences only. Negation
  can disable charts or select a replacement; absent preferences retain existing
  automatic behavior.
- Charts use actual returned fields and business values, never numeric entity
  identifiers as measures. Multiple grouping labels remain visible rather than
  silently collapsing separate business rows. Missing/ambiguous measures,
  missing scatter axes, duplicate line keys, invalid pies and unsupported chart
  types are explained without inventing values or silently substituting a type.
- Tree paths are assembled from returned dimensions, honoring explicit level
  order. This is a result-grouping tree, not proof of organizational ownership.
  Parent totals are not summed; duplicate paths remain unresolved. Escaped SVG
  renders real leaf values, including percentages. Pure name trees need no fake
  numerical measure.
- Root charts use only selected complete available deliverables, including
  dependent computed datasets, not the intermediate table or its 20-row preview.
  Existing 200-point and three-chart display limits are retained and disclosed;
  model analysis and verification still receive all available rows.
- MCP results carry an internal, excluded specification index. Explicit multiple
  charts use successful matching MCP outputs first, then per-chart local SVG
  fallback. A successful second metric is not relabeled as the first metric;
  a server without tree support cannot suppress the local tree.
- Public `ChartSpec.chart_type` adds `TREE` for the user's explicitly requested
  new capability. Existing fields, existing enum values and API/SSE transport
  remain unchanged. SQL, ASL parameters, grouping, query bounds, semantic scope,
  credentials, indexes, routing and the seven user-visible stages are untouched.
- REVIEW: no extra model/network calls for summaries, no semantic re-extraction,
  no aggregating non-additive metrics, and no new independent factual review of
  model prose. The existing synthesis grounding/uncertainty rules still apply.

## Verification

- 49 new offline cases pass, including original behavior reproductions,
  completed-question summary, all supported chart aliases, negative/replacement
  preferences, multiple metrics/types/dimensions, invalid or missing data,
  dependent full-data charts, truncation, SVG escaping and partial MCP success.
- Existing chart regressions plus the new cases: 103 passed.
- Critical suite: 424 passed with four separately reproduced original blockers
  deselected. The
  original blockers are three synthesis stubs missing the already-existing
  optional semantic-model parameter, and an old oversized-result status assertion.
- Visual QA: headless browser screenshots inspected for a synthetic three-level
  grouping tree and a multi-dimension bar chart; business values are visible.
- Full original checkout has a collection error in `test_no_default_time.py`
  (missing legacy `extraction_user_prompt` import). The runnable baseline excludes
  only that file: 4,548 passed / 98 failed. Final runnable suite: 4,598 passed /
  97 failed. Old-pass to new-fail: zero; old-fail to new-pass: one; new cases:
  49 passed; missing cases: zero. The changed outcome is the unmodified
  `test_triage_reason_contract_is_three_tuple`; the original source also passes
  its isolated rerun with the final run's hash seed. This is not attributed to
  this repair. The 97 remaining failures are all present in the original
  baseline, including legacy mock signatures, assertions and context behavior.
  This is not reported as a full-suite pass.
- The functional PR preserves its own integration baseline rather than copying
  unrelated server-mirroring changes into three divergent sources. Its first
  summary/chart/root/synthesis regression suite passes 181 cases. Its full suite
  has 4,719 passed / 3 failed, no collection errors. All three failures reproduce
  unchanged on its original `39e540a` baseline: missing captured diagnostic log
  records in `test_v2_catalog_mentions`, `test_v2_context_v1_execution_cutover`
  and `test_v2_raw_turn_recognition`. No old assertion is relaxed (`STALE_TEST`:
  none). Both checkouts additionally pass the 49 new cases after adding affected
  result-validation-before-insight and chart-only-in-final progress assertions.

## Exact manifest and release boundary

Seven production modules: `app/domain/models.py`, `app/analysis/synthesis.py`,
`app/analysis/visualization.py`, `app/presentation/summary.py`,
`app/presentation/root_report.py`, `app/services/orchestrator.py`,
`app/services/extension_dispatcher.py`.

New tests: `tests/test_final_output_preferences.py`; this document. Nine files
only, no environment, log, cache, generated preview or business export included.

Development and local version sources are synchronized by this exact manifest
and SHA-256 checks. The isolated functional branch preserves its own three
divergent integration sources (`models`, `synthesis`, `orchestrator`); only the
equivalent bounded repair is ported, not the unrelated server mirror. A Draft
PR is prepared without automatic merge.

Default deployment is blocked by the original collection/test failures. No
production source, service or protected configuration has been overwritten or
restarted. Do not claim deployed validation or real-business SQL correctness.

Current Stage: existing V2-context/V1-execution boundary unchanged. This is a
bounded final-presentation repair, not a V2 cutover declaration. Catalog,
Evaluation and Shadow gaps and V1 replacement readiness are unchanged. The next
shortest release path is resolving or explicitly waiving reproduced release
blockers, then checking remote differences and deploying only the affected
DataAnalysis service; no index rebuild, semantic publication or automatic merge.

## Authorized rollout closure — 2026-10-09

The blocked-release paragraph above records the earlier state. The user later
explicitly authorized retaining verified old failures provided no new failures
appear, and limited deployment to verified Agent, Oagnet and SQL updates (no
Java backend). These seven runtime modules were included in the first bounded
owner-binding rollout. Exact-file backups, protected configuration/source
hashes, service restart and health checks passed. The combined affected Agent
suite passes 190 cases, including the 49 new presentation cases. Full regression
and native ingress evidence is recorded in `filter_owner_binding_20261009.md`.
The functional follow-up remains Draft PR #89; no automatic merge, semantic
asset/index writes, authorization expansion or V2 cutover occurred. Additional
unverified development edits appearing during release were not deployed.
