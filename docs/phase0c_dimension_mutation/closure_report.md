# Phase 0C explicit dimension mutation closure

Stage result: **PASS — explicit dimension-only ADD / REMOVE / CLEAR**.
Existing REPLACE behavior is retained and covered as a contrast.
This stage does not authorize V1 replacement or enter Gold/model evaluation.

Development: `E:/YouoAgent/DataAnalysis_Agent`; Git repository: `E:/yy`.
Baseline: `098c3ed9489de925b44eb4f814b98c686683acc7` (PR #13).
Branch: `phase0c-dimension-mutation-20260908-01`.
Draft PR base: `phase0c-structural-scope-20260908-01`.
The commit containing this report identifies the final published tree; the
post-commit verification receipt compares every tracked blob with development.

## Audit and stage decision

Before edits, **813 tracked files and Git blobs matched**, the checkout was
clean, and the original Oagnet index matched its preserved import manifest.
Reviewed PR #13's closure and remaining evidence, historical failure/debt
classifications, current scope/state contract, and the catalog-publication
blocker. Inventoried all **100** Agent app/standalone-store sources by AST;
manual review covered public I/O, current-request scope, production V1 routing,
raw-turn parsing, admission, verified-frame selection, Legacy merge, semantic
rebinding, operation provenance, and the Intent-ASL/HTTP boundary. This source
inventory does not prove every legacy heuristic or deployed catalog record.

Offline journeys found dimension edits that lost their operation before query
execution. Final proof fixtures first assert a correctly parsed base task:
`查询销售额，按渠道和商品分组，2026年7月，地区上海`. Its metric, two dimensions,
Shanghai filter, entity mentions and July period are verified before each edit.
Other parser observations therefore cannot substitute for the three proofs.

## Proven failures

| ID | Current turn | First divergence | Before | Final |
| --- | --- | --- | --- | --- |
| DM-01 | 不要渠道 | TURN_ADMISSION | Raw parser sees 渠道, but admission omits its removal and records INHERIT; the verified frame restores it | REMOVE retains 商品 and removes 渠道 |
| DM-02 | 去掉所有维度 | TURN_ADMISSION | Admission drops the meaningful empty operation and records INHERIT; the verified dimensions survive | CLEAR yields an explicit empty grouping set |
| DM-03 | 再加医院维度 | TURN_ADMISSION | No additive dimension fact/dependency is recorded; a new incomplete task asks for a metric | ADD keeps 商品/渠道 and adds 医院, preserving the active metric, period and region |

Each proof independently failed twice before runtime edits. There are **three
deduplicated signatures**, distinguished by dialogue act and erroneous behavior,
not six bugs. They qualify for the explicit V1 freeze exception.

The baseline-versus-final provenance replay is included in `failure_evidence.json`.
It identifies the first divergence precisely: Legacy merge already removes or
clears dimensions correctly in the first two cases, but admission describes the
turn as inheriting them. The later orchestrator clone then restores the verified
frame. Fixing only that intermediate Legacy merger would leave the defect.

## Final change and review

Runtime changes are confined to `app/services/turn_admission.py`:

- Recognize complete explicit dimension-only edit commands as current-turn
  operations. A CLEAR carries an empty list; absence of a dimension slot is
  still distinct from clearing it.
- Preserve ADD/REMOVE/CLEAR in existing facts, operation traces and the merge
  boundary. ADD and REMOVE use the admitted before-frame, so an already-lossy
  intermediate merger cannot remove unrelated dimensions.
- Retain operation intent across current-scope semantic label rebinding. A
  candidate cardinality mismatch keeps the explicit target literals for
  downstream catalog resolution/rejection; it cannot expand mutation targets.
- Use existing semantic-change invalidation to discard the old ASL/result
  reference. The next verified frame persists the resulting dimension set.

No new task store, reducer architecture, state schema, permission system or
production V2 integration was introduced. The classifier, structured-model
Prompt/client, orchestrator and API handlers are byte-identical to baseline.
Unknown labels, compound edits and full metric queries are excluded from the
new guard; their existing parsing/clarification paths remain responsible.

Review caught two ways an intermediate merger could still lose members: ADD
already replaced the list, and removing 医院等级 could also remove the substring
医院. Taking the admitted before-frame and applying exact complete-name set
operations fixes both. Reapplying the operation is idempotent. Complete-label
matching distinguishes adding 医院等级 from explicitly adding 医院和医院等级.

Single-turn tests and real multi-turn orchestrator tests cover all four
operations, later time changes, clear-then-add, complete new tasks, semantic
renaming, repeated application, invalidation and model/domain/database/knowledge/
conversation mismatches. A successful offline Hybrid model plus the actual
QuestionRewriter exercises the added dependency path. The real HTTP adapter
emits the final required groupings and is stopped at its offline ASL transport;
no generated production result is claimed.

## Regex and semantic limits

Prompt modifications: **0**. One bounded addition grammar has two Regex sites:
whole-command recognition and extraction from its matched name span. The latter
uses escaped current dimension labels ordered longest-first. Existing whitespace
normalization and the classifier's negative-slot grammar are reused.

The addition grammar requires an addition verb, one or more parsed/currently
mentioned labels joined by 和/与/及/、, and a final 维度 or 分组 marker. It cannot
invent a catalog name or match a partial prefix of a complete new query. Positive
cases include `再加医院维度`, `加上医院分组`, `同时增加医院维度` and complete nested
labels. Contrasts include `查询新增医院数量`, `再加TDC-3产品`, unknown labels and
`增加医院维度的销售额`. Negative commands must also be consumed completely;
compound strings such as `不要渠道，按医院分组` cannot silently become one REMOVE.

Physical field binding and permission remain governed by the current authorized
catalog. This stage does not guess the interpretation of unmarked `再按...`,
multi-action edits, filter-value additions or unsupported dimension aliases.

## Verification

| Suite | Baseline | Final |
| --- | --- | --- |
| DataAnalysis Agent | 2,068 passed / 27 failed | **2,108 passed / 27 failed** |
| Oagnet | 343 passed / 10 failed | **343 passed / 10 failed** |
| SQL Translator | 200 passed | **200 passed** |
| Existing critical multi-turn/scope/single-domain | 160 passed | **160 passed** |
| New dimension mutation contract | Reproduced defects | **40 / 40 passed** |

Focused combined tests: **374 passed**. The full Agent run covers all **91 test
modules**, using 14 established serial offline batches and seven newer contract
modules. Both other services completed their guarded offline suites. Existing
clock seams are fixed to 2026-09-07; no dependency was installed.

Old pass -> new fail: **0**. Missing baseline nodes: **0**.
Old fail -> new pass: **0**. Collection errors: **0**.
Old expectation changes: **0**. New tests: **40**.
All **74** observed public clarification responses retained reason traces.
OpenAPI, ChatRequest, AgentResponse, spreadsheet schemas and dataset-reference
fields match baseline. Public response/SSE formats and authorization semantics
remain unchanged. Production still calls the existing V1 orchestrator, with
zero production imports of the V2 foundation.

Real external model calls: **0**. Production external writes: **0**.
Index publications/rebuilds: **0**. Production V2 routing changes: **NO**.
Cross-service source patches: **NO**. No deployment or automatic merge.

## Remaining evidence and readiness

The existing 27 Agent / 10 Oagnet failures retain their prior classifications;
no expectation changes hide them. The prior configuration-hygiene P1 and PR #13
broader date/entity decomposition observations remain outside this change.

New audit observations are explicitly **UNKNOWN_NEEDS_EVIDENCE**: DM-A01 covers
control words appearing in entity mentions after metric edits in mock journeys;
DM-A02 covers remaining filter-family edits and unmarked regrouping. Their
governed-boundary behavior requires a separate proof before a frozen V1 change.
This stage certifies explicit dimension edits, not all natural-language updates.

Highest overall blocker remains **BLOCKED_CATALOG_PUBLICATION** from PR #7.
Owned-domain projections, governed identifiers and an approved deployment/
publication target still need operational evidence. No production access,
deployment or index-write authority is inferred from this source task.

**V1 replacement is not ready.** Gold/model/shadow/canary acceptance is not
complete. Obtain user confirmation when replacement evidence passes, before
any production routing switch.
