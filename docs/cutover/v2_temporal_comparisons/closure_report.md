# Scoped V2 temporal comparison source closure

Baseline `2e5b5a6fb82d896eacc859af99a1b5eece067f11` (Draft PR #26).
Branch `cutover-v2-temporal-comparisons-20260908-01`. Date: 2026-09-08.

**Source acceptance PASS; V1 replacement readiness NOT_READY.**

## What changed and why

The prior ComparisonSpec allowed independently supplied current/baseline dates
and output refs. Time component changes only rejected comparison dependencies;
result compilation could deduplicate a comparison metric with its ordinary
measure without preserving current, baseline and derived roles. These are
proven scoped V2 contract gaps, not Legacy parser fixes.

TemporalComparisonSpec now retains a typed period rule and calculation. The
model chooses an explicit rule from current-turn evidence; code derives dates
and result roles. PREVIOUS_YEAR shifts exact natural-calendar boundaries one
year back. PREVIOUS_PERIOD names QUERY_GRAIN, CURRENT_WINDOW or a fixed unit.
QUERY_GRAIN follows an explicitly changed bucket grain. Fixed units remain
fixed and reject incompatible grouping changes. CURRENT_WINDOW requires an
aggregate window: whole calendar-month spans keep month boundaries; other
natural windows shift their local wall-clock span. EXPLICIT keeps the stated
baseline when the current range changes. Comparison units and query ranges
are separate; no historical/model value expands authorization.

Month/day replacement never clamps nonexistent dates. Newly calculated DST
gaps/folds reject without a policy; an endpoint already explicitly supplied as
an instant is retained. Unsupported fiscal/rolling, leap-day and week-year
policy cases remain explicit gaps in temporal_policy_gaps.json. They are not
counted as accepted cutover coverage or converted into generic metric questions.

Time, metric and comparison updates are reduced atomically. The final scoped
plan reconstructs periods and output metrics from the current TimeSpec, measures
and declared rule. Forged or stale derived fields reject. A Pending metric
choice can retain its comparison request and complete outputs after the exact
answer. CLEAR comparison preserves the underlying query and creates a barrier;
later unrelated edits do not revive it. CLEAR time alone does not imply canceling
the comparison; both removals require current evidence. Explicit re-enabling
comparison removes the barrier. New tasks inherit neither old time nor comparison.

Results now require CURRENT, BASELINE and DERIVED outputs per metric. Their
IDs and JSON fields remain distinct. ABS_DIFF is current minus baseline;
GROWTH_RATE is (current-baseline)/abs(baseline), as a fraction, matching current
AnalysisEngine and ResultValidator semantics. Zero baseline has no growth rate;
missing values remain missing. Arithmetic proof rejects fabricated zeros,
percent-versus-fraction mistakes and nonfinite values. Time/dimension keys must
be unique; projection IDs cannot hide required grouping or period outputs.
These checks prove consistency of supplied values, not that SQL queried the
right periods or paired the correct source buckets. Actual ASL/SQL and execution
binding evidence remain open in V-03/S-01.

## Compatibility and review

Public HTTP/SSE, ChatRequest/response formats, V1 routing and production model
defaults are unchanged. This is the opt-in scoped 0.2.2 path, still SHADOW_ONLY.
Frozen 0.2.1 schemas remain untouched and its final guard rejects the new rule
and output variants. Older scoped time comparisons without a declared period
rule are rejected and must be rebuilt from current evidence; code does not
guess their intended dependency from already materialized dates.

Prompt version v2-current-recognition-v6 describes the new typed edit seam and
dependency distinctions. Deterministic code supplies dates and output roles;
prompt rules do not repair missing calendar policies. Regex changes: zero.
The current schema equality test moves from draft v5/value v3 to explicit
draft v6/value v4. Historical schemas and business expectations are unchanged.

Self-review corrected scoped-state variable shadowing during CLEAR, an initial
test fixture publication-ID reuse, already-specified DST endpoint handling,
and output-ID/grouping collisions. No independent reviewer or model quality
evaluation is claimed. Mock utterances align with their requested rules; the
tests exercise actual HTTP/model seams with scripted responses.

## Validation and current gates

| Service | Baseline passed / failed | Final passed / failed |
| --- | --- | --- |
| DataAnalysis Agent | 2523 / 27 | 2573 / 27 |
| Oagnet | 560 / 8 | 560 / 8 |
| SQL Translator | 200 / 0 | 200 / 0 |

50 new tests pass. Full Agent regression covered 101 modules and returned
2572/27; final output-collision review plus its contrast passed all 555 affected
tests across 11 complete modules. Replacing those module results yields the
reported 2573/27 unique nodes. Critical: 160/160. Public clarification trace:
89/89. New failures, missing prior nodes and collection errors: zero. Full
Oagnet/SQL regressions ran offline without source changes.

Tests cover YOY and period comparisons, fixed versus dependent grain, exact
custom periods, metric/range changes, scalar and series CLEAR/re-enable, Pending,
new tasks, tampered derived facts, leap/DST boundaries, missing policy rejection,
positive/negative/zero baselines, null/nonfinite rates, duplicate buckets,
output collisions and frozen/scoped schema separation.

Scope remains user-selected 81/[205]. This stage uses offline catalog fixtures;
the latest actual capture remains the prior stage's recorded 81/205 evidence.
No fresh native publication or real query execution is implied. Real model and
embedding calls, native catalog writes, production external writes, cross-service
patches, deployment, merge and traffic changes: zero.

Current Stage: scoped temporal source readiness before Gold. Open Cutover P0=8,
P1=4. Highest blocker: actual isolated catalog publication/deployed trust, still
awaiting the previously requested operational authorization; its publication
candidate must be refreshed first. Calendar policy and execution gaps remain
explicit. Next shortest source path is external entity-value retrieval and
remaining semantic/decision coverage, followed by actual ASL/SQL path/period
bindings. Gold/model evaluation stays gated on catalog/state closure, then
actual plan-only shadow and rollback evidence. V1 replacement is NOT_READY.
