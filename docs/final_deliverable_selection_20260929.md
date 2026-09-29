# Final deliverable selection

## Proven cause

The supplied coverage-rate transcript contains a successful dependent calculation but displays all three task tables. `render_root_report` previously selected every available task when optional model presentation metadata was absent. Computation responses also omitted their persisted dataset ID, while composite export iterated the input tasks. Consequently the visible answer and download could expose intermediate datasets instead of the requested computed deliverable.

## Scoped correction

- Keep the completed root question as the synthesis target; explicitly request a separate final-answer object and select results according to what the user asked for.
- When model selection is absent/invalid, use successful dependency outputs rather than concatenating all inputs. Independent requested outputs remain separate. Failed calculations do not hide their available evidence.
- Use the deterministic calculation summary when the final prose is absent. Suppress hidden input-preview notices when the selected full calculation has its own result-range notice.
- Expose the exact persisted computation artifact. Apply the same selected task IDs to tables, charts, links and download sections. Single selected datasets can be exported without intermediate sheets; existing semantic-scope checks remain intact.
- Keep computation columns in their declared order. No changes to SQL, semantic catalogs, service configuration, permission checks, external request schema or public node order.
- Reinforce the existing prohibition on adding overlapping distinct hospital counts and calling the sum a deduplicated coverage total. The supplied insight contains that unsupported interpretation; it must not be copied into the final summary.

## Manifest and review

Runtime: `app/analysis/synthesis.py`, `app/presentation/root_report.py`, `app/services/orchestrator.py`, `app/services/report_export.py`.

Tests: `tests/test_root_goal_report.py`, `tests/test_report_export.py`.

Baseline d147400: 4449 passing tests. Nine new cases cover missing metadata, explicit multiple outputs, failed calculation fallback, exact computed dataset references, selected-only attachment export and single-artifact scope isolation. No existing assertions were relaxed. The changed computation dataset-ID behavior is justified by the supplied missing-deliverable reproduction.

## Verification and release

Local offline regression: 4458 passed, 8 pre-existing pytest reporting warnings, zero failures or collection errors. Preflight: all four remote runtime files match the expected baseline; no peer changes will be overwritten. Guarded deployment/live verification pending.
