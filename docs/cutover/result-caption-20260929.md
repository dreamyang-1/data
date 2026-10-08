# Result captions follow the user's requested deliverables

## Cause (PROVEN)

`render_root_report` used the child task question as the table caption. A derived
ratio task retains its numerator column, so its ratio-only question described
only one of the two delivered metrics. This is a presentation issue, not a
location-filter or semantic-model change.

## Minimal change

- The existing combined model call returns optional task-ID/title metadata based
  on the completed root question, not headers or the final calculation step.
- Latest user decision supersedes the separate-results proposal: keep merged
  results; title each merged result from ALL explicitly requested goals it
  answers. Ratio-only requests do not acquire an extra count requirement just
  because an auxiliary column exists. Preserve scope and avoid header-derived
  titles or metric-name keyword rules.
- Renderer uses selected result titles without rewriting tables. Missing title
  metadata uses the original independent question or a neutral calculation
  caption, never blocking data output.
- Both the unpublished metric-title completion experiment and the subsequent
  separate-results proposal were withdrawn. No computation metadata change remains.
- SQL, formulas, rows, result selection, single-result layout, API/SSE contracts,
  authorization and semantic configuration are unchanged. Selection instructions
  retain merged deliverables instead of repeating input tables. No extra model call
  or blocking validation is introduced.

## Verification

- Initial header-based draft was rejected by the user and has been removed,
  including its computation metadata change; it was never deployed.
- Its full run had one API title-context regression (4462 passed, 1 failed).
  The original assertion is retained; the corrected implementation preserves
  independent task context instead of replacing it with headers.
- Tests cover identical data for different requested deliverables, malformed or
  missing optional titles, original independent scope, and model context/parser.
- Corrected targeted suite: 217 passed. Full regression: 4467 passed,
  8 existing pytest report warnings, zero failures or collection errors.
  Baseline: 4458 passed; nine new cases, zero old-pass to new-fail.
- First deployed revision passed remote tests/health, but two read-only live
  replays exposed a missing numerator in the model-authored title. That revision
  is not treated as final acceptance. The user then chose separate results;
  final separate-result verification follows. Targeted tests: 218 passed.
- Final separate-result full suite: 4468 passed, 8 existing report warnings,
  no failures or collection errors (10 added cases vs baseline 4458).
- Live model with synthetic evidence: count+coverage selects both independent
  results/titles; coverage-only selects only coverage. Both passed before release.
- The separate-results commit was interrupted BEFORE deployment; remote source
  still matched 4b24b6f. The waiting deployment process was stopped. Those tests
  describe the withdrawn proposal, not the final release policy.
- Restored merged policy: 218 targeted tests and 4468 full regression tests
  passed (8 existing report warnings); final prompt clarification rerun: 58 passed.
  Live-model synthetic checks passed for both count+coverage (one result and
  both goals in its title) and coverage-only (no invented extra title goal).

## Final deployment acceptance

- Runtime commit: `b73e56a`. Guarded deployment backed up the changed file and
  preserved configuration hashes. Remote suite: 379 passed, one existing
  framework deprecation warning.
- DataAnalysis restarted 2026-09-29 15:07:32 CST and reported READY. SQL/Oagnet
  remained healthy and were not restarted; vector health passed.
- Original combined question replay completed in 81.1 seconds: two tables
  retained (regional total and merged dealer count/coverage), correct seven-stage
  ordering, one root insight, final artifact/link present. All checks passed.
- Captions: `上海市区域医院总数` and
  `上海市各经销商的已合作医院数及区域医院覆盖率`.
- No query, metric formula, semantic data, configuration, or routing change.

## Review

Explicit change manifest: `app/presentation/root_report.py`,
`app/analysis/synthesis.py`, `tests/test_root_goal_report.py`, this document.
No credentials, environment files, business rows or configuration are included.
V2 cutover status is unchanged; this release is the requested presentation fix.
