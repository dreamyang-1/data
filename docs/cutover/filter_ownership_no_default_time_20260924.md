# Contextual filter ownership and removal of implicit time windows

Current stage: bounded V1 maintenance; no V2 cutover or semantic publication.

## Evidence and root causes

- PROVEN: shared province-name ASL leaves the business owner unspecified; the
  translator's valid shortest relation path may use dealer location rather than
  the hospital population. Grouping by dealer is requested and must remain.
- PROVEN: current scoped metadata has hospital/dealer location FKs and published
  dictionary relations. A read-only dictionary lookup maps the example name to
  its real code. No relation, region code or physical column is fabricated.
- PROVEN: the selected cooperation metric publishes a date anchor but no default
  time special rule. An anchor does not authorize a current-year predicate.
- User's subsequent decision explicitly removes system-default time windows.
  This supersedes the earlier generic DSL-default-time behavior. Fixed metric
  formulas, explicit periods and confirmed conversational periods are preserved.

## Changes and boundaries

Oagnet reviews normalized shared-dictionary predicates against the completed
question, original structured extraction, selected metric definition and current
scoped relations. The model selects a finite relation candidate; parameterized
read-only source lookup supplies the real dictionary keys. Existing ASL FK
predicates encode ownership without a new API field or SQL shortest-path guess.
Multiple dictionary keys preserve the label's set semantics through IN/NOT IN.
No metric, grouping, subject, order, row limit or scope can change in this review.
Unavailable review/lookup retains the original predicate, not a fabricated ID;
this nonblocking fallback cannot guarantee business ownership during outages.

The review distinguishes rolling user periods from calendar periods and rejects
new default periods sourced from old catalog prose. The generation prompt and
legacy normalizer also stop applying implicit time defaults.

DataAnalysis removes default-year creation for ordinary queries, active-sales
wording and report children. The historical source-watermark retry hook no longer
creates a twelve-month window. Time-free trend contracts allow a time bucket
without requiring a bounded date predicate. Clarification preserves an explicit
new date when the previous time slot was empty. Forecast training history is no
longer invented; missing training history is still a genuine required input.
The maintained semantic description no longer instructs a twelve-month default.
No semantic database, index, permissions, production configs or SQL runtime code
are changed. User stage names/order remain unchanged.

## Validation

- Oagnet mock full: 1010 baseline -> 1031 final passed.
- Oagnet strict offline: 998 passed / 12 failures -> 1019 passed / same 12
  missing dependency-double failures. No collection errors or new failing IDs.
- Oagnet focused scope/temporal/grain/review suite: 213 passed before addition of
  the final optional-time contract case; that case passes in both full runs.
- DataAnalysis full: 4307 passed / 11 old MCP failures -> 4317 passed / same 11.
  No collection errors or new failing IDs.
- DataAnalysis critical: 492 passed, including frozen API stage order, pending
  selection, explicit dates, composite tasks and report children.
- STALE_TEST: old automatic-year expectations updated under the explicit new
  user policy in intent, clarification, forecast, watermark and DAG tests.
  Existing explicit-date clarification tests were not weakened; their real
  regression was fixed in the hybrid merge and they pass unchanged.
- Live local changed Oagnet + deployed SQL translation: example retains dealer
  grouping, selects hospital-owned province FK, has time_context=null, and SQL
  joins hospital without a calendar-year WHERE. Business SQL not executed in
  this probe. Deployment/end-to-end verification to be recorded after release.

## Exact manifest

Runtime:
- Oagnet/agent.py
- Oagnet/prompt_build.py
- Oagnet/mysql_tool.py
- Oagnet/query_binding_review.py
- Oagnet/asl_contract.py
- app/intent/classifier.py
- app/intent/structured.py
- app/services/orchestrator.py
- app/services/intent_asl_contract.py
- 语义描述文件.md

Tests:
- Oagnet/tests/test_query_binding_review.py
- Oagnet/tests/test_dsl_rule_precedence.py
- tests/test_no_default_time.py
- tests/test_intent.py
- tests/test_clarification_flow.py
- tests/test_clarification_quality.py
- tests/test_forecast_readiness.py
- tests/test_source_watermark_orchestration.py
- tests/test_task_dag.py

Review: no keyword-only hospital rule; opposite owner and dual-owner examples
covered; source lookup protects model/domain/data-source identity and literals;
all existing final ASL field grounding remains in force. Default-time removal
does not alter fixed metric definitions. V1 replacement readiness unchanged;
catalog publication and evaluation/shadow gaps remain outside this change.
