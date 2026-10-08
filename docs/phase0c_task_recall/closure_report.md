# Phase 0C explicit historical task recall closure

Stage result: **PASS — explicit historical task selection preservation**.
This bounded P0 stage does not enter Phase 0C-2 or authorize V1 replacement.

Development: `E:/YouoAgent/DataAnalysis_Agent`; version repository: `E:/yy`.
Baseline: `8bbf35167b15492b37e1ed141abbefef18c90204` (PR #11).
Branch: `phase0c-task-recall-20260908-01`.
Draft PR base: `phase0c-candidate-integrity-20260908-01`.
The commit containing this report is the final stage commit. Its Git tree and
the post-commit verification receipt identify the published bytes.

## Audit and stage decision

Before edits, all **794** tracked files matched development sources and committed
Git blobs. The version checkout was clean and the separate original Oagnet index
matched its import manifest.

Reviewed the preceding closure/test delta, historical failure inventory, current
scope/state contract and catalog publication blocker. Followed historical recall
through recent task storage, lexical selection, verified-last-request fallback,
Pending admission, scope checks, rewriting, slot protection and result reuse.
The source inventory covers all `app` modules and the standalone dataset store;
manual review focuses on the public/state/history boundaries. It does not prove
every legacy business heuristic.

This audit established four repeatable P0 paths in ordinary historical recovery.
They qualify for the explicit V1 freeze exception. Runtime changes remain in
the existing working-memory selector and orchestrator; no event store, task CAS,
identity infrastructure or V2 integration was introduced.

## Proven failures and fixes

| ID | First divergence | Before | After |
| --- | --- | --- | --- |
| TR-01 | SESSION_STATE | Recalling July Shanghai order count selected the old frame, then the latest verified August Jiangsu sales frame replaced it | The explicitly recalled frame retains its region, period and metric |
| TR-02 | SESSION_STATE | A tied or missing historical selection fell through to the active task and still queried | An unresolved leading historical reference returns SAFE_FALLBACK before query execution |
| TR-03 | SESSION_STATE | The word “刚才” selected the latest frame before checking the explicitly named metric | Match the named target first; explicit recency only chooses within matching tasks, or handles a bare latest reference |
| TR-04 | PENDING_ADMISSION | An unrelated Pending prevented an explicit return to a known historical task | A unique scope-compatible recall leaves that Pending using its existing expected version; a race stops safely |

The first three proofs independently failed twice before runtime edits. Fixtures
assert the initial Shanghai region and explicit period before testing recovery.
TR-04 was discovered during review and failed twice before its own fix, after
the first three corrections. These are **four failures**, not eight bugs.

The selector reads the reference clause before a punctuation-separated edit, so
“回到之前的订单量，改成含税销售总额” chooses the order-count branch. The existing
bounded lexical scorer and 12-frame retention remain. A weak match or a tie
without explicit recency is not promoted to a latest-task guess.

Strong resolution is required for a leading historical cue. Generic slot
continuations and embedded references such as “把刚才结果导出成Excel” preserve
their existing flow. Complete standalone queries remain new tasks. No new Regex
or Prompt text was added: the existing recall/recency patterns are reused, with
explicit leading-cue checks, phrase fillers and punctuation boundaries. Positive
and contrast cases cover these lexical boundaries.

Current model/domain/database/knowledge/conversation boundaries still determine
whether a restored frame can be used. A selected foreign-scope frame cannot
re-enter through latest-request fallback. Missing or ambiguous historical targets
preserve an unrelated Pending. A concurrent Pending version update survives and
prevents this turn from executing. The last verified frame remains the default
for ordinary elliptical follow-ups after failed provisional parsing.

Unresolved historical references currently stop with the existing fallback
shape. This stage does not add a historical-task chooser, infer an unavailable
branch, extend retention, or turn weak matches into user-visible candidate facts.

## Validation and review

| Suite | Baseline | Final |
| --- | --- | --- |
| DataAnalysis Agent | 2,016 passed / 27 failed | **2,042 passed / 27 failed** |
| Oagnet | 343 passed / 10 failed | **343 passed / 10 failed** |
| SQL Translator | 200 passed | **200 passed** |
| Existing critical multi-turn/scope/single-domain | 160 passed | **160 passed** |
| Conversation / lineage / semantic choice / candidate contracts | 43 / 36 / 32 / 29 passed | **43 / 36 / 32 / 29 passed** |
| New task recall contract | Reproduced defects | **26 / 26 passed** |

Focused combined regression: **424 passed**. Full validation covers every Agent
module through 14 established offline batches and five newer contract modules,
plus the two external services' guarded offline suites. The existing classifier
clock is controlled. Per-node results and deltas accompany this report.

Final old pass -> new fail: **0**. Missing baseline nodes: **0**.
Old fail -> new pass: **0**. Collection errors: **0**.
Old expectation changes: **0**. New tests: **26**.
OpenAPI, ChatRequest, AgentResponse, spreadsheet schemas and dataset-reference
fields match the baseline. API handlers and SSE framing are unchanged.

The first full run found one introduced regression in exporting a previous
result: the new guard treated an embedded reference as a leading historical
request. The guard was narrowed; export and history tests were rerun together,
then all three full suites were rerun. Final counts above are from that second
full run, not a mixture of unfinished results.

Review also covered full four-turn recovery, latest/tied/unmatched contrasts,
Pending interruption and version races, scope changes, Redis frame serialization
and preservation of existing result reuse. The installed FakeRedis lacks Lua
EVAL, so its four-turn case exercises real frame persistence through `_handle`,
bypassing the unrelated response-cache Lua. The in-memory case exercises the
public `handle` path. No new dependency was installed to bypass this limitation.

Real external model calls: **0**. Production external writes: **0**.
Index publications/rebuilds: **0**. Production V2 routing changes: **NO**.
Cross-service source patches: **NO**. No deployment or automatic merge occurred.

## Remaining findings and gates

The 27 Agent / 10 Oagnet existing failures retain their earlier evidence/debt/
environment/owner classifications. They are not hidden by expectation edits.
The previously recorded configuration-hygiene P1 remains outside this stage.

Audit observation TR-A01: in a rule-only synthetic probe, compact wording such
as `查询2026年7月上海订单量` can yield a product filter containing the date/region.
That parser behavior is observed; its outcome with the deployed governed
rewriter/catalog remains **UNKNOWN_NEEDS_EVIDENCE**. It is recorded for a separate
grounding audit. The finalized recall proofs use wording whose initial parsed
region is explicitly asserted, so this separate issue cannot supply their proof.

The highest overall blocker remains **BLOCKED_CATALOG_PUBLICATION** from PR #7:
governed owned-domain projections and identifiers/rules need evidence from an
identified approved deployment/publication environment. A historical local
snapshot is not proof of current MySQL/Milvus publication. No operations approval
is inferred from autonomous source-development authority.

**V1 replacement is not ready.** The production graph still calls the existing
orchestrator; no production V2 import or completed Gold/model/shadow/canary
evaluation exists. Catalog-publication acceptance remains the dependent next
milestone. Obtain user confirmation when the evaluation evidence supports
replacement, before any routing switch.
