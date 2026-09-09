# V2 replacement readiness — governed initial time contract

Current Stage: GOVERNED_INITIAL_TIME_CONTRACT. Baseline `c19e65544664395200d814300e27d330006ea9a1` / Draft PR #47.
Branch `cutover-governed-time-20260909t062000z`. Final commit: commit containing this report.
V1 Replacement Readiness: **NOT_READY**. Acceptance groups remain 8 P0 / 4 P1.

## Change and measured scope

For an initial TimeSpec with a current TIME_RANGE mention, the program now computes
dates from the existing business calendar and an injected aware clock, requiring
the full expression to match. Existing V1 callers retain their default substring
behavior and clock seam. Internal UTC serialization is unchanged. TIME_RANGE does
not imply grouping; explicit TIME_GRAIN has a separate finite calendar-unit map.

Explicit time-field choices retain their scoped binding checks. Without such a
choice, every selected metric must declare time_caliber.time_anchor, resolve to
one same-domain attribute and agree on that anchor. No guessed field, catalog
mutation or system-default range is introduced. Unknown special calendars/rules,
comparison/default extras and foreign/invented handles remain rejected.

This closes the recorded S81-014 timezone and catalog time-anchor defects, but
does not make the unchanged case pass: its split metric still creates a redundant
subject and V2_PAYLOAD_WOULD_DROP_BINDING correctly rejects it. A controlled output
Oracle removing only that subject assignment produces the expected scalar time
plan, retaining all original time values as non-authoritative hypotheses. Later
model outputs were recorded, not regenerated; this is not model accuracy evidence.

## Validation and review

Focused 239 passed; 50 new tests. Agent 2964 passed / 27 preexisting failures,
Oagnet 663 / 8, SQL Translator final repeat 381 / 0. Critical 160/160, clarification
trace 89/89. No old assertions changed, removed tests or collection errors.
Final old-pass -> new-fail 0. SQL's first full run had a loopback connection reset
in one unchanged test; two independent reruns and a whole-suite rerun passed.
The initial failure is retained in governed_time/sql_repeat_evidence.json; its
precise socket timing remains unproven. No SQL source/test patch was made.

New time tests initially compared UTC serialization to a local textual date and
used a shortened quarter unsupported by the existing full-expression grammar.
Their fixtures now assert identical aware instants and the supported full form;
the shortened form has an explicit rejection test. Review also prevented dropping
extra fiscal/default/comparison instructions during normalization.

Real model calls 0; source SQL 0; production writes 0. No prompt or regex pattern
change. Existing regexes use fullmatch only through the new V2 opt-in. Model/key,
public API/SSE and V1 production routing unchanged; native publication and Redis
were not changed. Source capture remains private.

## Remaining shortest path

Cutover P0: semantic whole-plan quality and safety acceptance still open.
Catalog Gap: exact compound metric resolution and source value canonicalization;
existing identity/grain/relationship limits remain query-shape-specific.
Evaluation Gap: temporal component ownership, time/region observation axes,
Pending/Dataset entry coverage, whole-plan/Oracle/holdout labels and model comparison.
Shadow Gap: offline acceptance not passed. Native publication and Redis recovery
only constrain their production gates, not all offline work.

Next: correct the proven atomic catalog metric segmentation failure, then finish
temporal edit ownership and measure unchanged Gold. Do not disable the guard
against silently dropping bindings, relax labels, or claim production readiness.
