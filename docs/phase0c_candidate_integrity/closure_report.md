# Phase 0C candidate and clarification identity closure

Stage result: **PASS — candidate/prompt identity preservation**.
This bounded P0 closure does not authorize V1 replacement or enter Phase 0C-2.

Development: `E:/YouoAgent/DataAnalysis_Agent`; version repository: `E:/yy`.
Baseline: `dfdcd1b76ab9f8dbdbfb8d88777abeabb7215f36` (PR #10).
Branch: `phase0c-candidate-integrity-20260908-01`.
Draft PR base: `phase0c-semantic-choice-20260908-01`.
The commit containing this report is the final stage commit. Its Git tree and
the post-commit verification receipt identify the published source exactly.

## Audit and decision

All **785** tracked files matched development sources and committed Git blobs
before modification. The version checkout was clean; the original Oagnet index
matched its import manifest and was preserved.

Reviewed PR #10's closure and regression evidence, the legacy failure inventory,
the current scope/state contract and catalog blocker, then followed candidate
data from adapter errors through SemanticAmbiguity normalization, Pending,
visible options, ordinal/name selection, clarification traces and execution.
The audit inventory covers every `app` Python module and the standalone dataset
store; manual review focuses on these public/state/candidate boundaries. It is
not a claim that every old business heuristic has been proved correct.

This stage targets newly reproducible correctness failures, satisfying the V1
freeze's explicit P0 exception. Runtime changes are limited to the existing
model validator, clarification policy and orchestrator. No V2 extension,
cross-service compensation, Prompt change or Regex change was introduced.

## Proven failures and corrections

| ID | First divergence | Before | After |
| --- | --- | --- | --- |
| CI-01 | SEMANTIC_GROUNDING | Labels `[tax, tax, net]` became `[tax, net]`, but details remained `[tax-ID, tax-ID, net-ID]`; selecting visible option 2 executed tax-ID | Normalize labels and details together; option 2 retains net-ID |
| CI-02 | INTENT_ASL_CONTRACT | Sales and orders both offered actual/budget labels; after answering sales, orders was suppressed as already asked | Hash the displayed target, semantic role and candidate identity; the distinct orders question can be asked |
| CI-03 | INTENT_ASL_CONTRACT | A nonblocking note was displayed and selected while the decision trace referred to a later blocking question | Rendering, trace identity and selection consistently use the blocking choice |

CI-01 has a direct proof and a full two-turn `handle` proof. CI-02 has a full
three-turn proof. All three initial proof nodes independently failed twice
before runtime edits. CI-03 was found during review and independently failed
twice before its display fix, after the first two fixes. These eight failed
process runs establish **three defects**, not eight unrelated failures.

Exact duplicate label/detail pairs collapse together. Label-only compatibility
remains; absent trailing details are represented by empty positional entries.
Conflicting duplicate labels, extra details and malformed detail collections
are rejected. A malformed blocking batch cannot be partially accepted by
silently discarding a still-required choice. The existing SAFE_FALLBACK and
SYSTEM_FAILURE trace handle that case without asking the user to repair it.

Conflicting duplicate metadata remains a catalog contract defect: this code
does not invent display names or decide which conflicting record is correct.
Already persisted extra-detail corruption fails existing state validation;
the Redis store's established invalid-state path rejects it. No bulk state
migration, deletion job or new persistence schema was added.

Repetition keys cover the displayed blocking question only, its phrase/role,
affected slots, semantic model/version and stable candidate identities. Scores,
generated ambiguity IDs, question wording and candidate order do not create a
different business question. Other deferred choices do not contaminate the key.
Trace candidate IDs remain opaque hashes, including for record-only candidates.

Legacy union-of-labels keys are mapped using the *previous stored Pending*
snapshot. The new request cannot reinterpret which old question was shown.
An actual repeat remains suppressed; a distinct target with identical option
labels can advance. Current request authorization and state scope checks remain
unchanged. Only hashes enter the structured trace; no raw business text is added.

## Tests and review

| Suite | Baseline | Final |
| --- | --- | --- |
| DataAnalysis Agent | 1,987 passed / 27 failed | **2,016 passed / 27 failed** |
| Oagnet | 343 passed / 10 failed | **343 passed / 10 failed** |
| SQL Translator | 200 passed | **200 passed** |
| Existing critical multi-turn/scope/single-domain | 160 passed | **160 passed** |
| Conversation / lineage / semantic choice contracts | 43 / 36 / 32 passed | **43 / 36 / 32 passed** |
| New candidate integrity contract | Reproduced defects | **29 / 29 passed** |

Focused combined regression: **369 passed**. Full regression covers every Agent
test module through the established 14 batches and four newer contract modules,
plus both external services' guarded offline suites. The existing classifier
clock is fixed. Per-node results, independent reproductions and deltas accompany
this report.

Old pass -> new fail: **0**. Missing baseline nodes: **0**.
Old fail -> new pass: **0**. Collection errors: **0**.
New tests: **29**. Old expectation changes: **0**.
Public OpenAPI, ChatRequest, AgentResponse, spreadsheet schemas and dataset
reference fields are equal to the baseline. Input/output and SSE framing retain
their existing formats; the changed candidate hash values are opaque identifiers.

Reviewed all changed hunks for pairing/order, model serialization idempotency,
invalid/mixed batches, real execution IDs, deferred questions, semantic versus
transport changes, legacy Pending compatibility, nonblocking choices and trace
privacy. Review found CI-03 and included its fix before full regression.

Real external model calls: **0**. Production external writes: **0**.
Index publications/rebuilds: **0**. Prompt/Regex changes: **0 / 0**.
Production V2 routing changes: **NO**. Cross-service patches: **NO**.
No dependency installation, deployment or automatic merge was performed.

## Remaining gates

The 27 Agent and 10 Oagnet existing failures retain their prior stale-test,
legacy-debt, environment or owner/evidence classifications. This stage does not
claim that all repository tests pass or choose unsupported business semantics.
The earlier configuration-hygiene P1 remains outside this P0 correction.

The highest overall blocker remains **BLOCKED_CATALOG_PUBLICATION** from PR #7:
governed owned-domain projections and catalog identifiers/rules need deployment
and publication evidence from an identified, approved environment. A historical
local snapshot is not current MySQL/Milvus evidence. No operational approval is
inferred from autonomous source-development authority.

**V1 replacement is not ready.** The existing orchestrator still serves the
production graph; no production V2 import or completed Gold/model/shadow/canary
evaluation exists. The dependent next milestone remains catalog-publication
acceptance followed by those evaluation gates. Obtain user confirmation when
the evidence supports replacement, before any routing switch.
