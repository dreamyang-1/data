# V2 Context Reference Completion Validation

## Problem class

A full current question can still depend on one explicit reference to the
previous task. For example, after a successful query scoped to `山西省`, the
question `按月统计该省份的含税销售总额。` is otherwise self-contained but requires
`该省份` to be completed before it reaches V1.

The live failure occurred because the Bridge recognized only terse time edits
and elliptical value replacements. The full referenced sentence fell through
to the generic semantic model, which added an unproved default time range for
`按月`; the existing time-evidence guard correctly rejected it as
`V2_EXPLICIT_TIME_RANGE_EVIDENCE_REQUIRED`.

`FIRST_DIVERGENCE = FULL_SENTENCE_CONTEXT_REFERENCE_NOT_COMPLETED`

## General solution

- Resolve explicit region, product, hospital, and partner reference surfaces
  from the active task only when that semantic family has one evidenced value.
- Read the prior value from either a V1-query-backed partial context task or a
  normal structured V2 TaskState.
- Replace only the reference surface. Preserve every explicit metric, grouping,
  time phrase, entity, and operation in the current sentence.
- Send the completed natural-language question through the unchanged V1
  execution entry with `history=[]`; V1 retains Scope, retrieval, Oagent, ASL,
  SQL, DB, and Dataset ownership.
- Treat the completed full sentence as the new active task. Publish its context
  only after V1 returns real `QUERY_RESULT` evidence. A failed query leaves a
  barrier, so a later turn cannot reactivate an older task.
- If one referenced family has zero or multiple prior values, ask for that
  value instead of guessing.

No product, region, business-domain ID, execution Scope, or query result is
hard-coded.

## Regression evidence

- Focused Bridge tests: 38 passed.
- Affected context, rewrite, Scope, Oagent-contract, and orchestrator tests:
  294 passed, 0 failed, 0 collection errors.
- Full suite: 3709 passed / 106 existing failed / 0 collection errors.
- The 106 failed node IDs exactly match the pre-change full candidate; both
  failed-node sets have SHA-256
  `0b581332a1fb516f10d9f745c135d2e604f6363e4024e922868881021e1f126e`.
  `old-pass -> new-fail = 0`.

## Live request classification

| Input class | Observed example | Result | Decision |
|---|---|---|---|
| Standalone metric + entity filter | `查询一次性脑电传感器的含税销售总额。` | V2 completed question; V1/Oagent/DB returned one row | PASS; unchanged path |
| Metric replacement | `订单笔数是多少？` after a regional sales query | Region retained; completed as regional order count; query returned | PASS; unchanged path |
| Standalone relation/detail topic | `山西省的经销商有哪些？` | V1 fallback returned 19 rows and published current context evidence | PASS; unchanged path |
| Full-sentence contextual reference | `按月统计该省份的含税销售总额。` | Pre-fix V2 time-evidence rejection | Fixed by the shared reference-completion boundary; live rerun pending |
| Grouped aggregate execution | `统计无针输液接头在各省份的含税销售总额。` | completed question and V2 Task are correct; V1/Oagent returned `ASL_GROUPING_DIMENSION_MISSING` | Downstream V1/Oagent capability; no Bridge change |
| Result-dependent chain after failed parent | `排第一的省份里...` → `这些医院...` → `它...` | Parent has no Dataset, so referents have no execution evidence | One cascading upstream failure; do not fabricate state |

## Runtime

The candidate is loaded by DataAnalysis Agent PID 31848 on
`192.168.1.27:8088`. `/live` is UP and `/ready` is READY in
`V2_CONTEXT_V1_EXECUTION` mode. Java/platform, Oagent, and SQL Translator were
not restarted.
