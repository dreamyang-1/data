# Final-ASL metric display and evidence

## Root cause and scope

PROVEN: `query_surface` deliberately clears `execution.metrics` on its copied
request while submitting the complete ASL to SQL. The shared SQL progress
renderer read that empty request list, so it displayed `指标绑定=[]` even when
the submitted ASL selected a metric. The legacy request list is not the
execution source of truth and must not be repopulated to hide a display issue.

The same shared boundary serves conventional requests and surface requests,
including composite children and executed follow-ups. Result completion binds
analysis/follow-up metadata from the returned ASL already; no changes to that
existing binding, request provenance or state transitions are needed here.

## Changes and audit

- `app/presentation/execution_trace.py`: pure, non-mutating reader for selected
  ASL metric names/codes. No generated IDs or catalog/network operations.
- `app/adapters/http.py`: display `已选指标（来自最终ASL）` with actual selected
  names and codes, or explicit no-metric text. Do not fall back to a stale or
  empty upstream list. SQL payload, formulas, filters and execution unchanged.
- `app/services/orchestrator.py`: semantic metric execution evidence iterates
  the actual ASL selection, rather than suppressing it for an empty request or
  including a stale request-only metric. Existing real matching metric IDs are
  preserved; otherwise the existing ASL code is used, not a manufactured ID.
- `app/presentation/reliability.py`: ASL-backed evidence describes selection
  and SQL use, includes its code, and explicitly disclaims a full independent
  business-definition audit. Other evidence providers retain their behavior.
- No authorization, semantic metadata, model, public schema, stage order,
  query execution or metric-formula change. No V2 cutover.

## Verification

- Baseline full offline: 4298 passed, 11 existing MCP-runner failures.
- Final full offline: 4307 passed, the same 11 MCP-runner failures. No new
  failures, no old-pass to new-fail transitions, no collection errors.
- Critical/targeted: 380 passed, including HTTP execution, surface handoff,
  orchestrator, composite, pending choices, API and SSE order tests.
- Nine new regression cases cover empty/stale upstream metrics, single/multiple
  ASL metrics, metricless output, unchanged SQL/ASL/request, actual metric
  evidence, no fabricated IDs, and no mutation of the supplied ASL.
- STALE_TEST: one presentation assertion previously demanded a full
  `已完成口径绑定` statement for ASL-only evidence. Updated to the requested
  executed-metric disclosure; no failing runtime gate was disabled.

Self-review: caller request remains untouched in surface execution; no metric
binding is created for display; existing scoped follow-up binding behavior is
protected by the orchestrator tests. High-confidence execution evidence is
not a certification of the underlying coverage formula or geography.

## Exact manifest

Four runtime files listed above, plus:

- `tests/test_executed_metric_display.py`
- `tests/test_reliability_presentation.py`
- `docs/cutover/executed_metric_display_20260924.md`

Current stage: bounded V1 display/evidence repair. Semantic-model ownership and
V2 replacement readiness remain independent; no claim of V2 readiness.
