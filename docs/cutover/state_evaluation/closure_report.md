# Multi-turn Gold and component evaluation

Baseline: `66c11a1bfa414ba63b4592cb5ff3713492a5b324` / Draft PR #39.
Branch: `cutover-state-evaluator-20260909t024300z`.
Final commit: the commit containing this report.

Current Stage: **MULTITURN_STATE_GOLD_AND_COMPONENT_EVALUATOR**.
V1 Replacement Readiness remains **NOT_READY**, with **8 P0 / 4 P1** production
acceptance groups open. This delivery closes the missing transition-observation
tooling portion of E-01, not full evaluation acceptance. V1 production routing,
public request/response/SSE formats and production model defaults are unchanged.

## Added evidence

The unchanged 100 axis records are supplemented by 20 reviewed multi-turn
records in [transition_gold.jsonl](transition_gold.jsonl). Labels cover metric
and grouping sets, region state, time preservation/clear, prior/new/historical
target, affected-slot operations, Pending detachment, lineage and three Dataset
route contrasts. Histories and the fixed clock are explicit. The synthetic
Dataset rows are not source business data. The 120 records overlap business
families; no independent-failure count or COMPLETE Gold claim is made.

Scope: model **81 / [205]**.
Catalog version: `3f9589b95d376ebdf9683425dcf6c646f2394b278718b033527bd5e08b64fe21`.
Frozen catalog hash: `b72b9d421365935f9b5bc2a3a2ec07c2f277bae7e0928c00ea64ae04ba0eb05e`.
Transition Gold hash: `f9307933e72e2c85fe189dcdad6855edfa09dce85926e761539beee2f621f825`.

The deterministic evaluator preserves fixed denominators, per-axis differences
and all seven safety categories. Missing safety observations remain
NOT_EVALUATED; a supplied safety violation cannot be discarded because the case
did not label it. No overall accuracy or production PASS is manufactured.

Actual V1 observations execute only the classifier/admission/merge and Dataset
service boundaries, with sockets denied and a fixed classifier date. They do not
include the production model, rewriter, historical selector or grounding. The
V2 adapter is verified against real scoped planner state and Pending ASK/ANSWER
outputs using scripted integration controls. Those controls are not a live
81/205 benchmark. See [source review](source_review.md).

## Measured V1 service results

| Labeled axis | Passed / denominator | Interpretation |
| --- | ---: | --- |
| Turn relation | 9 / 9 | Coarse relation, not all dialogue acts |
| Target task | 15 / 17 | Historical selector was not executed |
| Metric set | 15 / 16 | Exact governed names; no inferred canonical IDs |
| Metric operation | 3 / 3 | ADD, REPLACE and REMOVE separately observed |
| Dimension set | 2 / 3 | A grouping ADD loses the previous grouping at this seam |
| Dimension operation | 1 / 3 | Observed trace discrepancy retained |
| Region set | 5 / 7 | ADD/REMOVE differences require full-path follow-up |
| Region operation | 1 / 3 | A trace label can differ while resulting state is correct |
| Time preservation/clear | 2 / 3 | Time-clear case retains a range at this seam |
| Dataset route | 3 / 3 | Display limit / truncated global rank rejection / complete local rank |
| Canonical metric set | Unobserved (1 label) | No grounding occurs in the V1 rule seam |

Pending hijack has one observed service check without a violation; unsafe
truncated ranking has three. Wrong inheritance has 17 required but unobserved
full safety checks. Cross-scope reuse, scope expansion and silent auto-accept are
unobserved here. Existing scope regression results remain separate evidence.

These numbers do not answer full V1 accuracy or whether V2 is better. In
particular, a rule-only discrepancy may be resolved later by the existing
orchestrator/model. [Observed gaps](observed_gaps.json) preserve the exact output
and require full-path evidence before production-bug classification. No frozen
V1 logic or old expectations were changed to improve these measurements.

Every requested evaluation metric is accounted for in the
[coverage matrix](evaluation_coverage_matrix.csv). Full TaskPatch/semantic plan,
Mention F1, canonical binding, clarification precision/recall/repetition, safety
rates and paired full V1/V2 measurements remain incomplete. The comparison
status is **NOT_COMPARABLE_YET**, with no fabricated V2 score or accuracy delta.

## Regression and delivery

Thirty new evaluator/adapter regressions pass. Full offline results:

| Service | Baseline passed / failed | Final passed / failed |
| --- | ---: | ---: |
| DataAnalysis Agent | 2769 / 27 | 2799 / 27 |
| Oagnet | 663 / 8 | 663 / 8 |
| SQL Translator | 381 / 0 | 381 / 0 |

Critical **160/160**, clarification reason coverage **89/89**, collection errors
**0**, removed tests **0**, old pass to new fail **0**. Existing failures and node
outcomes are unchanged. Exact new nodeids and receipt hashes are in
[test delta](test_delta.json). No cross-service source patch was made.

Offline evaluations/tests made **0 real model calls** and **0 production writes**.
Separately, at the user's request, **one** qwen3.6-plus request was retried at
2026-09-09T02:46:37Z using unchanged settings. It again returned **HTTP401 /
invalid_api_key**; see [retry receipt](model_retry.json). No credential or raw
provider error body is stored, and no further automatic retry was issued.

## Remaining shortest path

Continue pairing complete V1/V2 observations with reviewed state/plan and
clarification labels. Restore the existing configured provider access for live
work. The user's requested benchmark candidates remain **qwen3.6-plus,
qwen3.8-max and deepseek-v4-pro**, subject to actual official interface/account
verification; earlier extra qwen3.7-plus probes do not replace that requirement.
No production model change is made by this candidate inventory.

Catalog identity gaps stay scoped to affected strict query shapes; Redis
production recovery and native publication remain Canary/Cutover requirements.
Neither blocks offline evaluation. Relevant evaluation must pass before isolated
plan-only shadow; no real shadow/canary has run. **READY_FOR_USER_APPROVAL has not
been reached**, and formal V1 replacement still requires explicit user approval.
