# Phase 0C explicit region list closure

**Bounded source/offline gate: PASS. Overall Phase 0C remains BLOCKED_CATALOG_PUBLICATION. V1 replacement: NOT_READY.**

## Baseline and target selection

Development workspace: `E:/YouoAgent/DataAnalysis_Agent`. Version repository:
`E:/yy`. Baseline: `80533c309a69851542dd182de92fff70b1e39e4c`, PR #16,
`phase0c-product-clear-20260908-01`. The baseline PR was verified open/draft
at that exact SHA. All 843 tracked source files matched the checkout and
committed Git blobs; the checkout was clean and the original Oagnet index was
unchanged. This stage uses `phase0c-region-list-20260908-01`, stacked on PR #16.
The final commit is the commit containing this report; its exact SHA and Draft
PR are reported in the release response, avoiding a circular manifest hash.

PR #16 left region list and mutation observations as PC-A01. Initial probes
showed that even the starting request `上海和北京销售额` lost Shanghai. Therefore
this stage closes complete, explicit lists of existing known regions before
claiming general region ADD/REMOVE support. It does not treat every probe as an
independent production bug. V1's freeze exception is supported by repeated P0
proofs of lost constraints and fabricated constraints at execution boundaries.

The audit inventories all 100 Python files / 1,305 functions in `app` and the
standalone dataset store. Manual review covered API/scope/state/V1 routing,
Rule and Hybrid extraction, semantic subject fallback, QuestionRewriter list
handling, admission/verified-frame reuse, semantic mention sanitation, clear
barriers, Intent-ASL and the real HTTP adapter. This is a complete source
inventory with targeted call-chain review, not a claim that every historical
heuristic or deployed catalog record has been certified.

## Proven failures and changes

| ID | First divergence | Before | After |
| --- | --- | --- | --- |
| RL-01 | RULE_PARSE | Ordinary region lists become the last region's EQ predicate | Complete same-field lists retain one existing IN predicate |
| RL-02 | RULE_PARSE | `查询上海和北京销售额` fabricates 商品名称=和北京; bare wording also introduces an implicit fragment | Recognize the complete structural subject before either partial-trimming fallback |
| RL-03 | SEMANTIC_GROUNDING | The admitted region filter changes, but superseded region mentions survive the sanitizer's inherited-evidence allowance | Replacement/CLEAR removes previous region evidence absent from retained filters |

Eight real execution/HTTP variants each failed twice independently before
runtime edits. A ninth proof injected a valid IN frame to isolate the
unchanged sanitizer; it failed twice before that boundary was fixed. A separate
baseline-checkout/current-source provenance comparison also confirms the
sanitizer defect for replacement and CLEAR. All nine proof variants now pass.
These are three failure signatures, not eighteen bugs.

Only `app/intent/classifier.py` changes at runtime:

- Reuse the existing common-region dictionary, choosing the longest known
  alias and consuming the complete list. Known canonical names and the existing
  地区 suffix are accepted. The list retains input order and deduplicates aliases.
- Positive connectors are `和`, `与`, `及`, `以及`, `、`. Lists must resolve to
  the same existing geographic field family. Their IN predicate does not add
  comparison grouping. Existing explicit comparison behavior remains intact.
- The full structural-subject check runs before single-region prefix trimming.
  Complete supported time expressions may precede or follow the region list;
  the existing validated temporal grammar remains authoritative.
- Existing typed entity spans remain protected. Negative/corrective phrases,
  mixed city/province fields and partial lists containing unknown outer
  members do not gain positive union semantics from this guard.
- After admitted replacement or CLEAR, filter-owned previous region literals
  are removed from semantic mentions unless a retained filter still owns the
  same literal. Product constraints survive region edits. Later time edits
  cannot revive removed region evidence.

No Prompt or Regex was added or changed. The new deterministic scanner's
scope and contrasts are in `tests/test_region_list_contract.py`. Examples:
`江苏和浙江`, `上海市与北京市`, and `上海、北京、天津` are positive lists;
`上海换成北京`, `上海不要北京`, `上海和江苏`, `上海和新城`, typed institution/product
names and incomplete outer lists are excluded. No new business aliases,
physical fields, permission rules or public input/output fields were invented.

Review caught two gaps in the draft implementation: CLEAR also needed to
remove inherited region evidence, and scanning only the first/last known
region could truncate an unknown outer list member. Both were corrected and
covered before release. The latter five contrast failures are development
regressions caught during review, not additional baseline production bugs.

## Validation

| Suite | Baseline | Final |
| --- | --- | --- |
| DataAnalysis Agent | 2,203 passed / 27 failed | **2,258 passed / 27 failed** |
| Oagnet | 343 passed / 10 failed | **343 passed / 10 failed** |
| SQL Translator | 200 passed | **200 passed** |
| Existing critical multi-turn/scope/single-domain | 160 passed | **160/160** |
| New region list contract | Repeated defects | **55/55** |

Focused combined suite: **501 passed**. Full Agent regression covers all
**94 test modules** using the established 14 serial offline batches plus ten
newer contract modules. After the final partial-list review guard, both the
focused suite and all Agent modules were rerun. Source/test SHA-256 snapshots
before and after the final run match the published source. The current-turn
Oagnet and SQL full results were retained because neither service's sources
nor dependencies changed during that review.

Old pass -> new fail: **0**. Missing baseline nodes: **0**.
Old fail -> new pass: **0**. Collection errors: **0**.
Old expectation changes: **0**. New tests: **55**.
All **89 observed public clarification responses** retained reason traces.
Classifier time is fixed to 2026-09-07; no dependency was installed.

Rule and Hybrid journeys use the real deterministic QuestionRewriter and
orchestrator with offline adapters; the synthetic model emits a valid sales
metric signal. The actual HTTP adapter is stopped at its offline ASL transport
and its emitted Intent-ASL payload is asserted. These tests prove Agent
contract preservation, not deployed catalog grounding, live model behavior or
the numerical accuracy of external SQL execution.

OpenAPI, ChatRequest, AgentResponse, spreadsheet schemas and dataset-reference
fields match baseline. API, domain models, orchestrator, admission, clear-guard
module, dependency injection, structured-model Prompt/client and HTTP adapter
are byte-identical to baseline. Public response/SSE framing is unchanged.
There are zero production imports of semantic V2; routing remains unchanged.

Current authorization remains authoritative: model-wide and a single explicit
domain retain their exact scope; model/domain/database/knowledge-base and
conversation mismatches cannot inherit a previous task. Business geographic
filters do not create or expand authorized business domains.

Real model calls: **0**. Production external writes: **0**.
Index publications/rebuilds: **0**. Cross-service source patches: **NO**.
Deployment: **NO**. Automatic merge: **NO**.

## Release and remaining blockers

One independent implementation/evidence commit is published on the new branch
with a stacked Draft PR. `change_manifest.json` records individually synced
paths and source/published SHA-256 hashes. Environment files, credentials,
logs, runtime data, caches and backups are excluded. All unrelated tracked
files and the original Oagnet index must remain unchanged. A reviewed revert
and explicit manifest synchronization provide rollback; no production rollback
is required.

The existing 27 Agent / 10 Oagnet failures retain their historical
classifications. This bounded gate closes the three failures above; it does
not declare all Legacy P0 candidates resolved. Region ADD/REMOVE, exclusions,
mixed-field geographic OR, unknown geographic literals, alternate 产品/商品
grouping and broader model/compound-role behavior retain their evidence needs.
For unsupported list syntax the earlier singleton fallback may still occur;
the new scanner does not certify that behavior. No convenient test expectation
was substituted for a missing business or catalog rule.

Highest overall blocker remains **BLOCKED_CATALOG_PUBLICATION** from PR #7:
owned-domain projections/identifiers and deployed publication evidence are
still missing. No approved operational target or deployment authority is
inferred from continuing source development. Gold, evaluator, benchmark and
canary acceptance remain incomplete. Existing configuration hygiene P1 also
remains recorded in earlier reports.

**V1 replacement is not ready.** Keep the current routing and obtain user
confirmation after replacement evidence passes, before any switch.
