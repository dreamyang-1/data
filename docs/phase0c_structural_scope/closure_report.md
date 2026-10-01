# Phase 0C compact temporal/geographic scope closure

Stage result: **PASS — SS-01 in the bounded structural-subject grammar**.
This stage does not authorize V1 replacement or enter Gold/model evaluation.

Development: `E:/YouoAgent/DataAnalysis_Agent`; Git repository: `E:/yy`.
Baseline: `b9d6dc9ed46e819b2f34055256be00a95910d207` (PR #12).
Branch: `phase0c-structural-scope-20260908-01`.
Draft PR base: `phase0c-task-recall-20260908-01`.
The commit containing this report identifies the final published tree; the
post-commit receipt verifies every tracked blob against development.

## Audit and decision

Before modification, **803 tracked files and committed blobs matched**, the
Git checkout was clean, and the original Oagnet index matched its preserved
import manifest. Reviewed the preceding closure, per-node baseline, historical
failure/debt report, current scope/state contract and catalog-publication gate.
Inventoried all 100 Agent app/standalone-store Python sources by AST and checked
public schemas and production V1 routing. Manual review followed rule parsing,
structured-model reconciliation, rewriting, semantic filter grounding,
admission/state preservation, Intent-ASL requirements and the HTTP boundary.
This is a source/boundary audit, not proof of every legacy heuristic or deployed
catalog record.

PR #12 observation TR-A01 is now **PROVEN at the executable Agent boundary**:
`查询2026年7月上海订单量` was parsed with
`商品名称 EQ 2026年7月上海`. That fabricated filter also protected its region
substring from the common-region extractor. The independent implicit-subject
rule could additionally send the entire date/region phrase as an entity mention.

The successful structured-model fixture correctly identifies `订单量`, but the
real Hybrid classifier reapplies deterministic rules after model enrichment.
The actual QuestionRewriter, using a scoped synthetic city catalog hit, does not
remove the fabricated requirement. The real orchestrator forwards it to query
execution. Separately, the real HttpDataRetrievalAdapter sends it in the outbound
Intent-ASL contract. Its ASL transport is deliberately stopped offline before
generation or SQL. Thus the owning first divergence is **RULE_PARSE**, not an
Oagnet error to compensate for, and deployed model behavior is not required to
prove the Agent's contract defect.

Two independent proof paths each failed in two fresh processes before runtime
edits. They represent **one deduplicated P0 signature**, SS-01, not four bugs.
Deployed occurrence frequency, actual catalog publication and production query
results remain unmeasured.

## Final implementation and review

Runtime changes are confined to `app/intent/classifier.py`:

- Share the existing province/municipality map between region parsing and the
  subject guard; no region, physical field or authorization rule was added.
- For an unmarked subject, recognize a complete existing time/grouping expression
  before or after one known region, including canonical names and the existing
  region suffix. Validate its time range before suppressing the entity guess.
- Apply the same complete-expression check before implicit entity-mention
  creation, so removing the false filter cannot leave the false downstream
  grounding requirement behind.

The check consumes the entire subject or rejects it. It never turns a partially
recognized remainder into a newly guessed product. Explicit product/device
markers and institution names retain their existing literal boundary for
governed grounding. It does not infer product existence or catalog roles.

The existing Regex expression was moved unchanged into the shared helper.
Regex pattern AST multisets are identical to baseline; **new/changed patterns:
0**. Region composition uses exact prefix/suffix comparisons against existing
names. Its scope is the existing closed Chinese year/month/day, relative
month/quarter/year and grouping grammar; it is not a general date/entity parser.
Prompt changes: **0**. Structured-model source is byte-identical to baseline.

Positive/contrast evidence covers date-before-region and region-before-date,
Shanghai/Jiangsu and canonical municipality/province/autonomous-region names,
relative dates with a fixed clock, explicit product and institution literals,
unknown place names, invalid dates and extra text. A complete compact new task
does not inherit an old product. Region REPLACE and CLEAR survive later turns;
the one-row mock correctly returns PARTIAL_SUCCESS for a monthly trend rather
than inventing a trend conclusion. No old expectation was changed.

The first focused fix removed the fabricated filter but retained the independent
implicit entity mention. The outbound-contract assertions exposed that gap;
both injection sites are covered in the final implementation and tests.

## Verification

| Suite | Baseline | Final |
| --- | --- | --- |
| DataAnalysis Agent | 2,042 passed / 27 failed | **2,068 passed / 27 failed** |
| Oagnet | 343 passed / 10 failed | **343 passed / 10 failed** |
| SQL Translator | 200 passed | **200 passed** |
| Existing critical multi-turn/scope/single-domain | 160 passed | **160 passed** |
| New structural-scope tests | Reproduced defect | **26 / 26 passed** |

Focused combined tests: **344 passed**. Full Agent coverage includes all **90
test modules**, using 14 established serial offline batches plus the six newer
contract modules. The Oagnet and SQL guarded offline suites also completed.
Existing clock seams are fixed to 2026-09-07; no dependency was installed.

Old pass -> new fail: **0**. Missing baseline nodes: **0**.
Old fail -> new pass: **0**. Collection errors: **0**.
New tests: **26**. Old expectation changes: **0**.
All **69** observed public clarification responses retained reason traces.
OpenAPI, ChatRequest, AgentResponse, spreadsheet schemas and dataset-reference
fields match baseline. API handlers, SSE framing and persisted state schemas
are unchanged. Source review confirms the existing V1 graph entry point and
zero production imports of the V2 foundation.

Real external model calls: **0**. Production external writes: **0**.
Index publications/rebuilds: **0**. Production V2 routing changes: **NO**.
Cross-service source patches: **NO**. No deployment or automatic merge.

## Remaining evidence and gates

The existing 27 Agent / 10 Oagnet failures retain their prior classifications;
they were neither suppressed nor relabeled to improve this stage's count.
The configuration-hygiene P1 remains outside this change.

Broader date/entity decomposition, multiple-region wording and invalid-date
handling are not certified by this bounded guard. Synthetic probes still show
the broad legacy subject fallback for `2026年7月新城`,
`2026年13月上海` and `2026年7月上海和江苏`. Their governed interpretation and
deployed outcome remain **UNKNOWN_NEEDS_EVIDENCE**; tests only ensure this helper
does not silently discard unrecognized text. Explicit product/date combinations
retain their literal for the catalog rather than guessing a decomposition.

Highest overall blocker: **BLOCKED_CATALOG_PUBLICATION**, unchanged from PR #7.
The approved deployment/publication target, owned-domain projections and
governed identifiers still need operational evidence. A synthetic catalog and
the historical local snapshot cannot supply it. No operational authority is
inferred from this source-development request.

**V1 replacement is not ready.** Production continues to call V1; Gold, model,
shadow and canary acceptance needed for replacement is not complete. Obtain
user confirmation when replacement evidence passes, before any routing switch.
