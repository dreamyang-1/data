# Raw-turn V2 recognition: source integration

Date: 2026-09-08. Baseline: `47608b0a8db4a20b54973d717bf4176e621d1b2a`
(Draft PR #21). Branch: `cutover-v2-recognition-20260908-01`.

**The tested source integration passes; V-01 remains partially open and V1
replacement readiness remains NOT_READY.** This is progress toward the original
cutover objective, not a replacement for the catalog, evaluation or shadow gates.

## Proven gap and implemented path

Before this delivery, CurrentTurnParser only validated a supplied parse, and
TurnResolver accepted a supplied TaskPatch/semantic resolution. There was no
entry from a raw current request to those artifacts. First divergence is
`STRUCTURED_INTENT`: the V2 extraction/selection invocation was absent.

`RawTurnPlanner.run()` now accepts the existing ChatRequest, TrustedIdentity and
optional internal scoped state/plan envelopes. It invokes the configured intent
model transport twice: current-turn surface extraction, then interpretation of
offered catalog handles and slot edits. It requires no caller-supplied parse,
TaskPatch, canonical binding or planned payload.

Code validates spans, current mention provenance, candidate handles, slot roles
and operation evidence. TaskPatch.compile and the existing deterministic reducer
produce materialized semantics. Payload construction then derives the plan from
those semantics, rejecting silent loss. Scope-bound prior plan identity preserves
ranking/query shape across follow-up; historical task handles must refer to
current-scope restored tasks. Bound mention references include the source turn.

The same ScopedPlanSession requires full catalog finish before returning a
SHADOW_ONLY plan and internal next-state/plan envelopes. No production state
store, SQL executor, HTTP endpoint or user-response route is added. Public
ChatRequest/ChatResponse/SSE formats, V1 behavior and model defaults are unchanged.

## Evidence and limits

| Service | Baseline passed / failed | Final passed / failed |
| --- | --- | --- |
| DataAnalysis Agent | 2348 / 27 | 2383 / 27 |
| Oagnet | 555 / 8 | 555 / 8 |
| SQL Translator | 200 / 0 | 200 / 0 |

All 96 Agent modules were run in the established offline batches (2380/27).
After final slot-role review, 35 new regressions and 256 existing affected V2
contracts pass (291 total); the final result map overlays that focused run,
including three additional tests. Critical Suite remains 160/160. Existing public clarification
trace checks remain 89/89. Old pass to new fail, missing prior nodes and collection
errors are all zero. No old expectations were changed. Node-level comparison,
raw hashes and source review are in `test_delta.json` and `source_review.json`.

New tests exercise raw requests through HTTP MockTransport, the actual Oagnet
publication/pin code with fake stores, and the actual V2 reducer/compiler. They
cover multi-turn ADD/REPLACE/REMOVE, CLEAR followed by another edit, new-task
isolation, historical return, field lineage without metrics, preserved ranking,
Dataset display LIMIT, rejected local global-ranking requests, current evidence,
foreign handles, scope/plan mismatch, duplicate messages and bounded provider
failures/retries. Tests supply recorded model responses, so they establish
orchestration and guard behavior, **not model accuracy or V1/V2 quality metrics**.

This delivery made zero live catalog reads, real model/embedding calls, index
publications, production writes or deployment operations. Oagnet and SQL source
are unchanged.

V-01 remains open for typed Pending-answer/clarification creation, complete
field-level filter operations, partial temporal edits, governed default display
and relationships, remaining analytical payload construction and actual model
recognition quality. A live Pending without explicit new-task evidence returns
a bounded unsupported-answer reason; it is not silently converted into a new
task. Multi-field filter clears/replacements that could drop unrelated fields
remain closed. These are visible gaps, not accepted cutover behavior.

Catalog candidate membership is distinct from semantic correctness. Existing
SemanticResolution fields report exact-name evidence and structural membership;
they are not calibrated probabilities. Logical executability means the typed
payload is constructible; the existing legacy adapter still reports unsupported
execution where appropriate. Dataset LIMIT constructs a local plan only and
does not claim that source results were executed or complete.

## Prompt decision

Two new bounded prompts use version `v2-current-recognition-v1`. Legacy prompts
and Regex rules are unchanged. Deterministic code cannot supply general natural
language surface interpretation or synonym/role selection by itself; existing
V1 StructuredIntent output is a different contract and lacks V2 mention spans
and registry-slot evidence. Therefore the new model calls supply interpretations
only; current scope, record identity, slot reduction and state acceptance stay
in code. The existing model name, endpoint, thinking setting, temperature zero
and retry configuration are reused. No default model migration is performed.

Before: a raw request could not enter V2 without fixture stage outputs. After:
mocked-provider raw requests compile and continue through scoped state. Contrast
tests reject changed ADD/REPLACE evidence, dropped edits, invented canonical IDs,
foreign handles, missing historical plans and global operations on a local
Dataset route. No claim about real model prompt improvement is made until the
catalog-gated benchmark runs.

## Cutover readiness and next path

Current Stage: Phase0C catalog/scope and recognition integration, before Gold.
Cutover Blocker P0: 8 open. Cutover Blocker P1: 4 open.
Catalog Blocker: C-01 through C-04 remain open; current 81 / 205 read-only capture
exists, but isolated native publication/marker acceptance is still pending.
Evaluation Gap: governed Gold, deterministic V1/V2 metrics and real-model
benchmark are not yet accepted. Shadow Gap: no real traffic comparison.
V1 Replacement Readiness: **NOT_READY**.

Next shortest blocking path: complete the remaining V-01 semantics through this
entry while the concrete PR #20 catalog publication authorization is pending.
After approval, verify a fresh 81 / 205 publication and deployed trust; close
catalog/scope/critical state gates before Gold, model benchmark and real shadow.
The 81 / 205 scope choice introduces no request default and grants no index-write
permission. Formal V1 replacement still requires the final explicit approval.

## Release

Only explicit manifest paths are synchronized from the development workspace to
`E:/yy`, with normalized bytes and SHA-256 checks. The original Oagnet checkout
and staged user diff remain preserved. Implementation/evidence commits and
rollback details are in `git_commit_manifest.json` and `rollback_manifest.json`.
Final release verification must match every tracked source file and committed
blob and leave the version repository clean. The Draft PR stacks on PR #21;
there is no merge or production cutover.
