# Phase 0C semantic choice preservation closure

Stage result: **PASS — semantic choice member/state preservation**.
This is a bounded continuation from the dataset-lineage stage, not entry into
Phase 0C-2 Gold or approval to replace V1.

Development: `E:/YouoAgent/DataAnalysis_Agent`.
Version repository: `E:/yy`.
Baseline: `89643b95a10a782dd096b388bd28db9cf383c0eb` (PR #9).
Branch: `phase0c-semantic-choice-20260908-01`.
Draft PR base: `phase0c-dataset-lineage-20260908-01`.
The commit containing this report is the final stage commit; the commit's Git
tree and the post-commit verification receipt identify the published bytes.

## Audit and stage selection

Before edits, all **776** tracked files matched the development sources and
committed Git blobs, with zero unexpected drift and a clean version checkout.
The separate original Oagnet index was verified and left intact.

Reviewed the preceding closure/test delta, original failure inventory, current
trust/scope contract, catalog publication blocker, API entry and scope checks,
Pending admission/merge, catalog candidate application, clarification trace/gate,
state transitions and dataset/V2 boundaries. The accompanying AST inventory
covers all `app` Python modules and the standalone dataset store. This is an
inventory plus focused call-chain review, not proof of every legacy heuristic.

The 27 existing Agent failures include tests whose obsolete mandatory-time
setup prevents them from reaching the Pending transition they claim to test.
Their expectations were not changed. Fresh tests instead obtain real semantic
Pending through the current `handle` path and a deterministic catalog adapter.
The adapter retains ambiguity during the existing bounded semantic retry;
an initial once-only-error fixture was corrected before recording the proofs.

This exposed independently reproducible P0s in admitted query preservation.
They qualify for the documented exception to the V1 multi-turn freeze. The
stage changes one runtime module; it does not expand the V2 architecture.

## Proven defects and corrections

| ID | First divergence | Before | After |
| --- | --- | --- | --- |
| SC-01 | SLOT_MERGE | Selecting the sales-amount candidate replaced `[sales amount, order count]` with one metric | Replace the uniquely identified member in place; retain other metrics and metadata |
| SC-02 | SLOT_MERGE | Selecting the region candidate replaced `[region, channel]` with one grouping | Retain channel and the existing grouping order |
| SC-03 | INTENT_ASL_CONTRACT | After one of two semantic answers, structural missing-slot recalculation dropped the remaining semantic blocker and executed | Advance the scoped Pending clarification before planning/retrieval |

SC-01 has both direct and full two-turn reproductions; SC-02 has direct and
full-flow regressions. Each initial direct/flow proof failed in two independent
offline processes before code changes. SC-03 was discovered during contrast
review and failed twice independently after the member fix, before its own
state-transition fix. These are **three defects**, not eight distinct bugs.

Candidate targeting uses the catalog phrase and exact existing input/canonical
name/metric ID, or exact grouping name. It does not infer the replaced member
from list order, loose substrings or the LLM. Legacy missing-phrase ambiguities
remain compatible for empty/singleton slots. Missing, conflicting or multiply
matching targets on populated collections preserve Pending and return the
existing SAFE_FALLBACK shape before another retrieval. They do not generate a
repeat question. The catalog must provide target evidence to enable that case.

The selected member is replaced without mutating the stored input object.
Filters, time, unaffected metric IDs/versions and other execution slots retain
their admitted values. Label-only dimension options now apply to their target.
Independent remaining choices use existing clarification items, state versions
and reason traces, with the current AuthorizedSemanticScope rebound before save.

No public field, schema, endpoint or SSE envelope was added. Prompt changes: 0.
Regex changes: 0. Cross-service source changes: 0. Production V2 routing: unchanged.

## Validation and review

| Suite | Baseline | Final |
| --- | --- | --- |
| DataAnalysis Agent | 1,955 passed / 27 failed | **1,987 passed / 27 failed** |
| Oagnet | 343 passed / 10 failed | **343 passed / 10 failed** |
| SQL Translator | 200 passed | **200 passed** |
| Existing critical multi-turn/scope/single-domain | 160 passed | **160 passed** |
| Conversation trust / dataset lineage | 43 / 36 passed | **43 / 36 passed** |
| New semantic choice suite | Reproduced P0s | **32 / 32 passed** |

Focused combined regression: **340 passed**. Full Agent validation covers every
test module in the established 14 offline batches plus the three newer contract
modules. All three services use their guarded offline runners; the existing
classifier clock is fixed. Exact node results and deltas accompany this report.

Old pass -> new fail: **0**. Missing baseline tests: **0**.
Old fail -> new pass: **0**. Collection errors: **0**.
Old test expectation changes: **0**. New regression tests: **32**.
OpenAPI, ChatRequest, AgentResponse, spreadsheet request/response schemas and
DatasetReference fields are equal to the baseline.

Review covered mutation order, input immutability, canonical ID targeting,
empty/singleton legacy compatibility, duplicate/missing target rejection,
multiple independent choices, three-turn time follow-up, terminal dependency
failure clearing, new-task interruption and current-model scope changes.
It found and corrected the downstream retry on unresolved targets and SC-03;
the final full regression ran after those corrections.

Real external model calls: **0**. Production external writes: **0**.
Index rebuilds/publications: **0**. No dependency installation, deployment,
automatic merge, unrelated repository mutation or identity-system changes.

## Remaining work and V1 decision

The highest overall blocker remains **BLOCKED_CATALOG_PUBLICATION** from PR #7:
owned-domain projections and governed identifiers/rules need deployment and
publication evidence from an identified approved environment. The historical
local snapshot does not establish live MySQL/Milvus readiness. No operational
approval or production target is inferred from autonomous development authority.

The 27 Agent / 10 Oagnet legacy failures retain their existing evidence/debt/
environment/owner classifications; this stage neither masks them nor declares
the full repository green. The new unknown-target case is explicitly rejected
pending catalog evidence. The earlier configuration-hygiene P1 also remains.

**V1 replacement is not ready.** Production still uses the existing orchestrator;
there is no production V2 import or completed Gold/model/shadow/canary evidence.
The next dependent milestone remains controlled catalog-publication acceptance,
followed by the already defined evaluation gates. Obtain the user's confirmation
when those gates support replacement, before any routing switch.
