# Distinct chart categories and readable monetary bars

PROVEN: chart generation projected product names and amounts but omitted the
returned product code. Same-name/different-code rows then reached the MCP with
identical categories and different values, permitting overlapping bars/labels.
The same renderer sources were verified on the deployed server before repair.

The bounded repair is presentation-only:
- BAR/PIE projections disambiguate repeated names using one corresponding,
  nonempty returned identity field. Name/code stems must match; unrelated or
  ambiguous identifiers are not guessed. The original rows, order and values
  remain unchanged, with no sums or deduplication.
- Unresolved duplicate categories are not plotted. Explicit requests explain
  that the table was retained without aggregation; both local and MCP renderers
  reject unresolved BAR/PIE collisions.
- Long bar labels select horizontal layout. Same-name identity suffixes remain
  visible after business-name shortening; hover text retains the full name.
- Large monetary bar labels use two-decimal 万/亿 notation. The underlying
  metric, table and exact hover amount retain original precision. Counts and
  percentages preserve their existing label representation.
- When a supplied MCP cannot guarantee the required long-name/money layout,
  the existing local SVG renderer is used. Ordinary supported MCP calls retain
  their original data and argument contract. No new tool-schema options are
  invented.

Exact manifest: `app/analysis/visualization.py`,
`app/services/extension_dispatcher.py`,
`tests/test_chart_category_identity.py`, this report.
No SQL, semantic assets, analytical calculations, orchestrator, progress/SSE
contract, configuration or unrelated service is changed.

Verification:
- Canonical affected chart/presentation/critical/API suite: 292 passed.
- Bounded PR port on its divergent ASL-reliability branch: 289 passed; its new
  identity tests also pass separately (34). Full canonical counts below are
  not a full-regression claim for that divergent PR base.
- New tests cover ten Chinese/English business name/code combinations through
  requested and automatic charts, missing/repeated/unrelated/ambiguous codes,
  unchanged unique labels, renderer safeguards, long labels, exact monetary
  hovers, negative/zero/large amounts and preserved count labels.
- Complete offline comparison, fixed business clock/hash seed and denied
  network: baseline 4,646 passed / 98 failed / one collection error; final
  4,680 passed / identical 98 failed / the same collection error. All 34 added
  cases pass; zero removed cases or pass/fail transitions. The pre-existing
  `test_no_default_time.py` collection error is retained, not hidden; both runs
  use `--continue-on-collection-errors` to execute all other collected cases.
  This is not reported as a full-suite pass. Reproduced baseline failures are
  retained under the existing user-approved no-new-failure policy.
- Before commit, screenshot-shaped fixture labels/codes/amounts were replaced
  with synthetic test values; no runtime code changed and the 292-case affected
  suite and PR's 34 new cases were rerun successfully. No business records are
  included in the commit.
- Browser visual QA shows ten distinct bars, ten value labels and visible
  identity suffixes for all four duplicate-name records, without overlapping
  values. Browser skill was used to verify the rendered SVG rather than only
  its XML structure.

Deployment: checked patches update only the two runtime chart modules after
confirming their exact committed remote baselines. Backups and isolated test
staging are retained. Actual remote application-code tests pass 292 cases;
DataAnalysis alone is restarted, active/READY with all readiness profiles true.
Protected configuration, service unit, orchestrator and independently changing
server planner remain unchanged; no unrelated files were overwritten.

Two real native SSE requests for the ten highest-sales products return one
horizontal BAR chart with ten rows, ten distinct categories, ten bars and ten
value labels, with no stream errors (57.2s and 56.6s). The strengthened second
verification matches every chart value back to its product/name-code table row
and matches every exact SVG hover amount to the unchanged chart data. The
question introduction and result table remain present. Original PARTIAL_SUCCESS
is retained: this repair does not claim optional ranking analysis now succeeds.
Planning precedes parsing, execution, validation and insight events. The final
synthetic fixture file also passes 34 tests against actual server sources.

Current stage is bounded V1 chart
presentation maintenance. Catalog/Evaluation/Shadow gaps and V1 replacement
readiness are unchanged; no V2 cutover or automatic PR merge is included.
