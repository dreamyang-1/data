# Result captions follow the user's requested deliverables

## Cause (PROVEN)

`render_root_report` used the child task question as the table caption. A derived
ratio task retains its numerator column, so its ratio-only question described
only one of the two delivered metrics. This is a presentation issue, not a
location-filter or semantic-model change.

## Minimal change

- The existing combined model call returns optional task-ID/title metadata based
  on the completed root question, not headers or the final calculation step.
- Final user decision: independently requested counts and ratios are separate
  result blocks with separate business-question titles. A ratio's auxiliary
  count column does not replace the requested count result. A ratio-only
  request still hides calculation inputs. Object/region/period distinctions
  are preserved; no header-derived titles or metric-name keyword logic.
- Renderer uses selected result titles without rewriting tables. Missing title
  metadata uses the original independent question or a neutral calculation
  caption, never blocking data output.
- The unpublished automatic metric-title completion experiment was withdrawn
  after the user chose separate results. No computation metadata change remains.
- SQL, formulas, rows, result selection, single-result layout, API/SSE contracts,
  authorization and semantic configuration are unchanged. Selection instructions
  now preserve each explicitly requested result independently. No extra model call
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

## Review

Explicit change manifest: `app/presentation/root_report.py`,
`app/analysis/synthesis.py`, `tests/test_root_goal_report.py`, this document.
No credentials, environment files, business rows or configuration are included.
V2 cutover status is unchanged; this release is the requested presentation fix.
