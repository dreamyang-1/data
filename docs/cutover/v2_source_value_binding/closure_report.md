# V2 source-value binding and restore contract

Baseline: `ff389dc4cd3fe4e0dabe1c665e3672b80b445330` (Draft PR #28).
Branch: `cutover-v2-source-value-binding-20260908-01`. Date: 2026-09-09.

**Source implementation acceptance: PASS. V1 replacement readiness: NOT_READY.**

The first divergence was `SEMANTIC_GROUNDING`: Raw V2 could bind published
static catalog candidates, but not mutable entity names from their governed
business source. PR #28 established the pinned source lookup; this change
connects that contract to Raw recognition, filter operations and state reuse.

The backend remains the authorization authority. The selected validation scope
is **model 81 / domain [205]**, never a request default. Current upstream scope
and conversation identity bind every artifact. This stage used fake model HTTP
and fake business connections only. It performed no actual business-source
reads, model calls, embeddings, index publication or production writes. The
actual 89-field mapping evidence is the metadata-only PR #28 capture in
`../v2_entity_value_sources/catalog_source_evidence.json`; it is not a new live
capture or proof of actual business values.

## Current-source evidence

The model can request exact lookup using current value mentions and offered
field handles, or reference the exact existing filter being edited. Oagnet
enforces the existing governed search policy for implicit field hypotheses.
Display-main attributes do not enable search. Explicit field references allow
exact identifier lookup without enabling broad implicit search. Malformed
policy flags fail closed. No new regular expressions were added.

`SourceValueBindingEvidence` carries the current field, canonical value, source,
mapping, observation and context identity. Values remain existing `EntityValueRef`
filter values; they are not falsely declared static catalog members. The Agent
checks receipt scope/pin, published attribute identity, exact field/source
mapping and integrity. The field requires current `CatalogBindingEvidence`.
The existing `DatabaseLoadTool` resolves the platform database selection before
constraining source lookup. LLM output cannot supply a receipt or expand scope.

A complete unique value can be bound. Proven normalization or field collisions
produce bounded complete filter choices. Each option includes its field and
the result of the original filter operation. Multiple ambiguous requests retain
all valid combinations within the explicit option budget. Incomplete results,
missing values and excessive combinations fail with system reason codes;
truncation never proves uniqueness.

`ADD`, `REPLACE`, `REMOVE` and `CLEAR` use the existing reducer and preserve
Boolean structure. A Pending answer must actually apply the chosen full filter;
the reducer's selected-option guard remains active. Field and metric ambiguity
can coexist and are each asked once. Clarification traces distinguish verified
source collisions from catalog synonym collisions.

## Current use versus archived history

Restoring history first verifies context and catalog membership. Archived
values are retained only as historical identities until selected for current
use. `NEW_TASK`, `CLEAR`, and exact removal of a current filter member do not
require an obsolete value to remain in the business source. Removal evidence
cannot authorize adding or querying that deleted value.

Continuing a task, returning to a selected historical task, accepting a Pending
choice, or restoring cache/DAG/semantic-binding artifacts requires current
source evidence. An unselected task or Pending option does not force an
unrelated business-value query. Per-session observation reuse is limited to the
live pin; pin acceptance repeats the underlying observations. Sealing archived
conversation/task history still requires the associated current catalog field.

The observation contract is not an atomic business snapshot or a guarantee
that values cannot change after acceptance. Downstream execution must retain
its own validity boundary. The current adapter continues to report that these
plans cannot safely execute through the legacy SQL path.

## Compatibility and verification

| Service | Baseline passed / failed | Final passed / failed | New passing tests |
| --- | --- | --- | --- |
| DataAnalysis Agent | 2573 / 27 | 2615 / 27 | 42 |
| Oagnet | 625 / 8 | 633 / 8 | 8 |
| SQL Translator | 200 / 0 | 200 / 0 | 0 |

Final complete runs cover all **102 Agent test modules**. Critical: **160/160**.
Public clarification trace: **89/89**. New tests: **50**. Collection errors: **0**.
Old-pass to new-fail: **0**. Old-fail to new-pass: **0**. Remaining 27 Agent and
8 Oagnet failures are unchanged from the baseline, with node IDs in
`test_delta.json`. Final totals do not sum repeated test runs.

The first full run exposed six frozen Phase 2.5.1 replay/idempotency regressions:
adding an unused option field changed serialized mutation hashes. Serialization
now omits only the absent extension, preserving old option wire values and
Pending identities. All frozen fixtures and their assertions remain unchanged.
The repaired replay/source subset passed 108 tests; the final source/relation
subset passed 81 tests, followed by complete three-service regression.
Intermediate failures and corrections are retained in `test_delta.json`.

Public HTTP request/response and SSE formats, V1 behavior, production routing
and default model are unchanged. The internal draft is now v7, and recognition
value schema v5; previous exports remain available. Authorized plan 0.2.2 adds
the source evidence union. Frozen 0.2.1 schemas remain unchanged. The current
schema equality test follows the new exports; no business expectation was
changed to reduce failures.

Prompt version: `v2-current-recognition-v7`. The single task prompt addition
describes the typed source request seam because the recognizer must name current
mentions and offered fields. Lookup, policy enforcement, collision handling and
acceptance remain deterministic. Parser prompt changes: **0**. Regex changes:
**0**. Mock tests cover positive, negative and contrast cases; real-model
accuracy is not inferred from them. Review is a self-review, not an independent
review; details and limitations are in `source_review.json`.

## Cutover readiness

Current Stage: V2 current-source binding, filter choices and restore.
Cutover Blocker P0: **8 open**. Cutover Blocker P1: **4 open**.
Catalog Blocker: native isolated publication/activation remains absent; actual
value-query/runtime source identity remains unverified.
Evaluation Gap: curated Gold and deterministic V1/V2 metrics, then real-model
benchmark, remain gated by catalog readiness.
Shadow Gap: actual plan-only shadow has not started.
V1 Replacement Readiness: **NOT_READY**.

Exact value evidence proves the selected field/value observation. It does not
prove that model-selected field hypotheses cover every possible business
meaning, nor that a model will always select this seam instead of an existing
literal filter. Approximate matching and wrong silent auto-accept rates require
Gold and real recognition evidence. C-04/V-01 therefore remain open.

Next shortest blocking path: resolve the pending isolated-publication approval,
regenerate its stale candidate from current catalog facts, verify native full
read-back and durable activation, and obtain authorized actual source evidence.
Then close catalog/scope/state gates before Gold/model evaluation and actual
shadow. This stage does not merge, deploy, replace V1 or claim production
readiness. Original Oagnet staged changes are preserved; explicit manifests
and final Git/blob verification govern publication of source changes.
