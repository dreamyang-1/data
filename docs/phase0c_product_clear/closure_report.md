# Phase 0C product condition clear closure

Stage result: **PASS — product-name condition CLEAR across Legacy turns**.
This closes two verified V1 P0 behaviors. It does not authorize V1 replacement,
production routing changes, or entry into Gold/model evaluation.

Development: `E:/YouoAgent/DataAnalysis_Agent`; version repository: `E:/yy`.
Baseline: `c2fa1411d1909a6380f924ebe1bb4723a2518df8` (PR #15).
Branch: `phase0c-product-clear-20260908-01`.
Draft PR base: `phase0c-metric-edit-grounding-20260908-01`.
The commit containing this report identifies the published tree; post-commit
verification compares all tracked Git blobs with development.

## Audit and stage decision

At entry, **833 tracked files** matched development and their committed blobs
under the established LF publication policy. The Git checkout was clean and the
original Oagnet index matched its preserved import manifest. PR #15 was verified
open/draft at the baseline SHA. Reviewed its closure, failure inventory and the
prior dimension-mutation observations, the current trust/scope contract, and the
catalog-publication blocker from PR #7.

Inventoried all **100 Agent app/standalone-store Python files and 1,304 functions**
by AST. Manual review followed request scope and state admission, raw parsing,
Hybrid enrichment, actual QuestionRewriter, Legacy merge, verified-frame clone,
semantic bindings, restore barriers, canonical execution text and HTTP ASL
construction. Public schemas and production routing were compared with baseline.
This inventory does not prove every legacy heuristic or deployed catalog record.

DM-A02 contained several unresolved filter-edit observations. Product CLEAR was
selected because the existing request explicitly names the condition to remove,
and the defect is independent of cross-field union semantics or catalog aliases.
Region ADD/REMOVE and alternate product grouping labels remain separate evidence
requirements; their exploratory failures are not counted as fixed here.

## Proven failures

The initial task is `查询TDC-3产品销售额，2026年7月，地区上海`. Each proof first
verifies the completed base query's product, Shanghai filter, metric and July
period. This prevents unrelated initial parsing defects from substituting for
the clear-operation evidence.

| Signature | Turn | First divergence | Before | Final |
| --- | --- | --- | --- | --- |
| PC-01 | 不限产品 | TURN_ADMISSION | No dependency or product CLEAR fact; incomplete new task asks for a metric | Follow-up clears product condition and preserves metric/period/region |
| PC-02 | 去掉产品条件 | TURN_ADMISSION | Follow-up is recognized, but product CLEAR is absent; merge and verified restore retain TDC-3 | Product condition, entity evidence and old plan/result reference are removed |

Legacy SLOT_MERGE is a secondary affected stage. Parsing the word 产品 as a
dimension also lets a generic merge replace the requested grouping. Clearing a
condition must not change the grain or projection.

There are **two independent failure signatures in one product-CLEAR contract
family**, with **six replay variants**, not twelve bugs. Rule and successful
offline Hybrid paths both use the actual deterministic QuestionRewriter. The
real HTTP adapter is stopped at an offline ASL transport to check the outgoing
contract. All six variants independently failed twice before runtime edits.
`failure_evidence.json` includes baseline/final provenance at raw parsing,
admission, Legacy merge and final protected slots.

The earliest failure for 不限产品 occurs before planning; its direct pre-fix
adapter contrast has no executable Intent-ASL contract. The other phrase reaches
the boundary with an unwanted product filter. Neither result is represented as
a deployed SQL/model observation.

## Implementation and review

Runtime changes are limited to three Agent files:

- `turn_admission.py` records a deterministic `CLEAR_PRODUCT_FILTER` dependency
  and a product CLEAR operation with its before/after value and reason code.
  Admitted current product filters, including an elliptical literal resolved by
  the existing gate, can reopen the product condition.
- `legacy_guards.py` recognizes complete clear commands and enforces a persisted
  internal assumption marker at the existing restore boundaries. It removes
  product-name predicates, their aliases/entity mentions and semantic bindings,
  and reindexes retained bindings. Shared literals still owned by another
  retained filter remain present. An actual removal invalidates the old ASL and
  dataset reference.
- `classifier.py` treats a complete clear command as a filter-only edit before
  generic dimension merging. It preserves the remaining task shape and unrelated
  pending requirements, then renders execution text from the final slots.

The existing region restore entry point delegates to the product barrier as
well; the orchestrator's existing convergence and pre-readiness calls therefore
enforce it without adding a parallel state pipeline. Region's typed barrier
field remains unchanged. The product marker uses the existing internal
assumptions collection; no request/response field, enum schema, or public format
was added. It contains only a control code, not retained business text.

This operation removes business predicates within the **current upstream grant**.
It cannot change AuthorizedSemanticScope, model, domains, database or knowledge
bases. Governed metric-definition filters are not user condition slots and are
not rewritten here. Product-name roles are identified by the existing semantic
family function or a current binding's canonical label/code; no physical field
mapping or catalog default is invented.

Review caught two concrete errors in the first implementation:

- A direct Legacy merge could replace or remove product grouping while clearing
  a condition. The complete-command merge path now preserves the original grain,
  projection, metric, period and other conditions.
- Reopening with `那TDC-4产品呢？` failed when only raw parser filters could lift
  the marker. The gate's already-admitted lexical product filter is now accepted
  as current explicit evidence. The same prior product can also be requested
  again; CLEAR is not a permanent ban on that value.

Additional review covers idempotent enforcement, IN-valued product filters,
binding-index changes, a product/brand sharing the same literal, late restored
filters, model/domain/database/knowledge/conversation mismatches and positive
MODEL_WIDE/single-domain journeys. Clear-then-region-clear and the reverse order
both preserve the barriers. The actual execution text also excludes TDC-3 after
clearing. Explicit new product queries and following time edits retain their
new condition.

The grouping fixture uses an explicit 产品/渠道 grain. An exploratory query
combining the product role with the alternate 商品 label yielded both labels in
the current mock path; that separate alias/grain observation remains UNKNOWN.
No old expectation was changed to hide it.

## Grammar and scope limits

Prompt changes: **0**. Regex additions/changes: **0**.
The new recognizer checks a whole command after whitespace/trailing punctuation
normalization. It composes existing operation words and 产品/商品 roles:
不限/不限制 plus the role, or 去掉/移除/取消/删除 plus the role and an explicit
条件/筛选/过滤/筛选条件/过滤条件 suffix.

It excludes full analytical questions, noun substrings, dimension operations,
bare `不要产品`, and compound edits such as `去掉产品条件再加TDC-4`. Tests also
keep brand, category and manufacturer predicates separate. This stage closes
product-name condition clearing; it does not infer how unknown catalog fields,
all product attributes, other role families or compound commands should clear.
Unrelated pending metric/semantic requirements are not answered by a product
CLEAR. General pending-choice cancellation remains outside this contract.

## Verification

| Suite | Baseline | Final |
| --- | --- | --- |
| DataAnalysis Agent | 2,159 passed / 27 failed | **2,203 passed / 27 failed** |
| Oagnet | 343 passed / 10 failed | **343 passed / 10 failed** |
| SQL Translator | 200 passed | **200 passed** |
| Existing critical multi-turn/scope/single-domain | 160 passed | **160/160** |
| New product clear suite | Defects reproduced | **44/44** |

Focused combined suite: **446 passed**. Full Agent regression covers all
**93 test modules** in the established 14 serial offline batches plus nine
newer contract modules. Both other services completed their guarded offline
suites. The prior SQL loopback transient remains in PR #15's historical report;
this turn's full SQL run passed without a repeat failure.

Old pass -> new fail: **0**. Missing baseline nodes: **0**.
Old fail -> new pass: **0**. Collection errors: **0**.
Old expectation changes: **0**. New tests: **44**.
All **84 observed public clarification responses** retained reason traces.
Classifier time is fixed to 2026-09-07; no dependency was installed.

OpenAPI, ChatRequest, AgentResponse, spreadsheet schemas and dataset-reference
fields match baseline. Domain models, API, orchestrator, dependency injection,
structured-model Prompt/client and HTTP adapter are byte-identical to baseline.
Public response/SSE framing and production V2 routing remain unchanged.

Real model calls: **0**. Production external writes: **0**.
Index publications/rebuilds: **0**. Cross-service source patches: **NO**.
Deployment: **NO**. Automatic merge: **NO**.

## Release and remaining blockers

One independent implementation/evidence commit is published on the new branch,
with a Draft PR stacked on PR #15. `change_manifest.json` lists individually
synchronized paths and source/published SHA-256 hashes. Environment files,
credentials, logs, runtime results, caches and backups are excluded. The original
Oagnet index is preserved. A reviewed revert and explicit file synchronization
provide rollback; no production or index rollback is needed.

The existing 27 Agent / 10 Oagnet failures retain their earlier classifications.
Configuration hygiene P1, region/multi-region edits, product alias grouping and
broader compound/role behavior remain outside this bounded gate. UNKNOWN evidence
requirements are retained in `failure_evidence.json`; this report does not claim
all Legacy P0 candidates are closed.

Highest overall blocker remains **BLOCKED_CATALOG_PUBLICATION** from PR #7:
governed owned-domain projections/identifiers and an approved operational target
still require deployment/publication evidence. No such authority is inferred
from continuing source development. Gold, evaluator, benchmark and canary
acceptance are incomplete.

**V1 replacement is not ready.** Keep the current routing and obtain user
confirmation after replacement evidence passes, before any switch.
