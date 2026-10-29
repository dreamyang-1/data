# Final answer layout — 2026-09-29

## Requested contract

Use the supplied reference's three sections for analytical final answers:

1. 概况总结: current analysis question/range, existing conclusion, result table.
2. 关键发现: existing facts and interpretation, followed by the existing chart.
3. 业务提示: priorities, limitations and execution-related disclosures.

PROVEN: `AnswerPlan.render()` produced five adjacent labelled prose fragments;
the orchestrator appended the table and then put the chart after all notes.
This caused the final answer's layout to differ from the requested reference.

## Minimal implementation and boundaries

- Add `AnswerPlan.render_report()` exclusively for the final answer. Retain
  `render()` for evidence and insight-model inputs without changing their content.
- Pass the existing table/chart markup and notes into this layout. Preserve
  rendered numbers and metric units; do not calculate new totals (especially
  for ratios), change SQL/ASL, or regenerate charts with different data.
- Keep ranking tables already embedded in deterministic summaries. Display
  analysis result tables even if no explicit dimension was requested.
- Put factual interpretation under findings and recommendations under business
  tips, avoiding repeated advice in both sections. Preserve warnings and missing
  field disclosures. Chart absence does not prevent report rendering.
- Ordinary detail lists, empty-result handling, attachment contracts, model
  insight prose and seven-stage ordering remain unchanged.

## Change manifest

- `app/analysis/interpretation.py`
- `app/services/orchestrator.py`
- `tests/test_insight_interpretation.py`
- `tests/test_analysis_orchestration.py`
- This report.

All files are synchronized to the canonical Git repository using the explicit
manifest and SHA-256 checks. No configuration, credential or business-row files
are included. Remote preflight snapshot found the runtime files consistent with
the preceding deployment; unrelated stale server tests are not imported.

## Validation

Baseline: `3ef67fb`, 4435 passed, no failures or collection errors.

- Targeted analysis/interpretation/synthesis/list-cleanup/ASL-stage and critical
  scope regressions: **202 passed**.
- Four new cases cover inline charts, remote chart images, missing charts,
  embedded ranking tables, unchanged input/evidence rendering and non-additive
  metrics. Existing orchestration test now verifies table/chart section order.
- STALE_TEST: the old “注意事项” label assertion is updated to “3、业务提示”; its
  warning-content assertion remains, so required limitations cannot disappear.
- Full regression: **4439 passed, 0 failed**, no collection errors (284.89s).
  Four new passing cases; no old-pass to new-fail. Eight unchanged JUnit reporting
  warnings. No tests skipped to obtain this result.
- Remote offline regression: **362 passed**, one existing dependency deprecation
  warning. Runtime commit `2b3db12` pushed to the existing feature branch.

## Deployment

- Release `answer-layout-2b3db12-20260929-114618`: all four changed source/test
  files passed snapshot/hash guards and were backed up before atomic replacement.
- DataAnalysis restarted at **2026-09-29 11:46:43 CST**, PID **78938**, active and
  READY (HTTP 200). SQL Translator, Oagnet and vector health checks also passed;
  those services were not restarted. Configuration hashes are unchanged.
- Initial Git push had a TLS handshake transport failure; ordinary retry succeeded
  without weakening certificate validation or changing configuration.
- Post-restart live smoke used the screenshot's trend question. COMPLETED in
  57.0s: seven-stage order retained, SQL completed, exactly one set of three
  headings, table inside overview, chart inside findings, no old inline labels
  and no chart in the insight node. Checks passed; no raw business rows recorded.
- No remaining blocker for this presentation task. Repository and development
  manifest hashes match. Broader V2 readiness is not assessed by this release.

Current scope is a final-answer presentation change, not V2 replacement. No
semantic-catalog edits, index rebuilds, permission changes, cutover or PR merge.
