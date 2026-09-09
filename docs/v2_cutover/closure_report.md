# V2 replacement readiness — catalog metric boundary and time observation

Current Stage: CATALOG_METRIC_BOUNDARY_AND_TEMPORAL_OBSERVATION.
Baseline `9d680ae096b9deafe42767d0d7952e0525be0c0a` / Draft PR #48. Branch `cutover-compound-mention-20260909t064400z`.
Final commit: commit containing this report. V1 Replacement Readiness: **NOT_READY**.
Acceptance groups remain 8 P0 / 4 P1; these are not independent-bug counts.

## Verified behavior

An exact full metric name in the current scoped catalog can recover a split
subject-plus-measure span before stage-two candidate selection. Recovery requires
one metric identity, contiguous current mentions and consistent operation/clause/
negation evidence. Full-term cross-role, name/code/alias collisions, independent
modifiers, overlapping proposals and repeated terms preserve the original facts.
The measure ID and operation survive; no canonical binding is selected by this
step. The original guard against dropping state bindings is unchanged.

The unchanged S81-014 targeted run used 6 qwen3.7-max calls. The raw parser again
split 订单笔数 into 订单 and 笔数. Catalog recovery restored the whole metric before
the draft. All three turns planned: 2025 order count, clear time, add quantity.
The time anchor is the catalog created_date attribute, grain NONE, and the cleared
range stayed absent on the next turn. Three complete result/state recordings
replay exactly without model calls. This is direct execution evidence of the fix.

The existing transition observer now measures actual time ranges and grouping
grain separately. It validates previous artifact scope/digest and never reads
Gold labels. All three listed S81-014 targeted axes pass. Unmeasured broader
wrong-inheritance and other safety gates remain NOT_EVALUATED.

## Whole corpus and regression

Unchanged 20-case transition Gold, prompt/schema, model, clock and frozen catalog:
{'OK': 6, 'FAILED': 10, 'NOT_RUN': 4}. Full run 50 model calls, plus 6 targeted calls;
56 total this stage. Status OK means an observable plan, not full business
acceptance. Sample regressions and newly observed time axes are retained in
compound_mentions/comparison.json; changes outside the targeted trace are not
claimed as causal quality improvements. Pending/Dataset cases retain fixed
denominators and NOT_RUN where their entry fixtures are absent.

Focused 225 passed, 34 new tests. Agent 2998 passed / 27 historical failures,
Oagnet 663 / 8, SQL Translator 381 / 0. Critical 160/160; clarification trace
89/89; old-pass -> new-fail 0; removed tests 0; collection errors 0. Four new
observer fixtures initially missed an imported dependency; fixture wiring was
corrected before final checks. No old test assertions changed.

Source SQL 0; production writes 0. No prompt, regex, public API/SSE, model/key or
V1 routing changes. Original Oagnet user-owned staged state preserved. Private
raw captures and source observations remain outside Git; only curated receipts
and existing evaluation outputs are published.

## Remaining shortest path

Cutover P0: semantic quality and whole-plan/safety acceptance still incomplete.
Catalog Gap: source-value names/aliases and existing shape-specific identity,
grain and relation endpoints. Current source records show province short_name
alongside province_name, but no new alias mapping or source lookup was asserted.
Evaluation Gap: full deterministic temporal component ownership, region axes,
Pending/Dataset entries, whole-plan/Oracle/holdout labels and formal model comparison.
Shadow Gap: offline acceptance has not passed. Native publication and Redis
recovery remain production gates and were not re-audited.

Next: close deterministic temporal component edits and the proven scoped source
value resolution gap, then measure the unchanged corpus. Continue internal work;
do not request production replacement approval until all required gates pass.
