# Grounded V2 clarification and Pending resume

Date: 2026-09-08. Baseline: `d798dc6f7a9342f3090ed418439d2fa0672a6c54`
(Draft PR #22). Branch: `cutover-v2-pending-20260908-01`.

**Source-path acceptance PASS; V1 replacement readiness NOT_READY.** This closes
the raw V2 path's rejection of supported canonical-option Pending answers and
adds grounded clarification creation. V-01 remains open for its other semantic
coverage and actual model-quality requirements; no Phase0C closure is claimed.

## Proven divergence and change

PR #22 returned `V2_PENDING_ANSWER_EVIDENCE_REQUIRED` for every live Pending
without an explicit topic shift. It also treated all unresolved model mentions
as system failures, with no path to create a valid ambiguity question. The first
divergence was `PENDING_ADMISSION` / `SEMANTIC_GROUNDING` in the raw V2 entry.

The new path accepts ambiguity proposals only when the current mention and
slot-operation evidence agree and every matching candidate is present. Candidate
meaning must be supported by exact names/codes or governed catalog aliases;
there must be multiple distinct readable labels. An exact unique name or unique
governed alias is repaired deterministically without a question. Unproven
ambiguity, missing catalog facts, indistinguishable labels and omitted matching
options stay system/catalog failures. Synonyms are read from the pinned record;
entity aliases use the existing entity_alias field. No separators are guessed.

Unambiguous edits are reduced into a PROVISIONAL task. Deferred canonical choices
are stored using existing PendingBlocker/ClarificationOption/ConversationState
types. A scope-bound resume descriptor retains the original payload kind and
SET/ADD/REPLACE operation. Its identity binds task, operation and immutable
options, preventing a restored descriptor from relabeling an ADD or query kind.

Answers must match a whole visible option name/ID or the existing V1 bounded
ordinal grammar. Substring matches are excluded. Explicit new requests bypass
old Pending; unrecognized answers do not select an option or ask the same
question again. The selected value goes through deterministic TaskPatch and the
existing StateMutation option/readiness checks. Multiple blockers are confirmed
one at a time; the same task is compiled after all are resolved. ADD retains
prior values. Scope/version/option mismatch prevents restore.

Every emitted raw V2 question carries the existing ClarificationDecisionTrace
with reason, slot, options, pending/task references and bounded evidence codes.
The first-ask claim is checked against the actual previous state. A previously
asked blocker cannot be labeled as new. Stored Pending.question/last_question
match the emitted question. Long option sets remain in structured options and
candidate_answers rather than being truncated into the question text. The trace
does not persist the full raw request. Public HTTP/SSE serialization is unchanged;
these are internal plan-only outcomes awaiting the existing response adapter.

`ScopedPlanSession.accept_catalog()` now serves both plans and clarification
artifacts. It requires complete source/inventory/activation acceptance before
either can leave the session; this does not bypass the executed-result guard.
Failure at finish cannot return a question or updated state envelope.

## Verification

27 new regressions pass. They exercise governed ambiguity creation, exact-name
repair, six answer forms, multi-blocker completion, preserved ADD, new-task
isolation, rejected partial/unrecognized answers, cross-scope restore, modified
resume kind/version/operation, missing catalog evidence, hidden candidates,
indistinguishable labels, finish failure and repeat-question trace integrity.
The affected new/old V2 suite passes 318 tests. Full suite results and exact node
deltas are recorded in `test_delta.json` and `final_DataAnalysis_Agent.json`.

| Service | Baseline passed / failed | Final passed / failed |
| --- | --- | --- |
| DataAnalysis Agent | 2383 / 27 | 2410 / 27 |
| Oagnet | 555 / 8 | 555 / 8 |
| SQL Translator | 200 / 0 | 200 / 0 |

Full Agent coverage includes all 97 test modules. Critical Suite: 160/160;
existing public clarification trace coverage: 89/89. Old pass to new fail,
missing prior nodes and collection errors are zero.

The model responses and catalog aliases are synthetic, declared test fixtures.
The tests execute the actual HTTP client boundary, pin/publication code,
reducer, Pending state transition and compiler with offline transports/stores.
This proves source orchestration and guard behavior, not actual model accuracy,
published catalog coverage or production traffic behavior. Catalog pin and state
digests identify trusted service material; they are not signatures and do not
authorize accepting arbitrary client/model envelopes.

This delivery performs no live catalog reads, real model/embedding calls,
index publication, external production writes, service deployment or routing
change. Oagnet and SQL Translator source, V1 behaviors, public API/SSE fields and
production model defaults are unchanged. No old test expectations are changed.

## Prompt and grammar review

Recognition prompt version advances to `v2-current-recognition-v2`; only the
semantic-edit prompt changes behavior. It exposes typed ambiguity proposals
instead of requiring unresolved mentions to remain terminal. Code verifies all
proposals against current surface and governed catalog facts; the model cannot
create scope, canonical identities or Pending state. General language
interpretation still requires the existing model client; this prompt change
does not substitute for deterministic admission/reduction or alter V1 prompts.

Before: no supported clarification/resume path. After: declared alias collisions
create bounded questions and whole-option responses resume the original task.
Contrasts reject absent aliases, hidden matching candidates, new requests that
contain an option as a substring, and falsified repeat/operation evidence.
No Regex is added. The ordinal recognizer follows the already implemented V1
whole-answer grammar, with name/ID, prefix, ordinal and one-to-ten bounds checked
as strings. This is answer syntax, not a new business-semantic keyword rule.

## Cutover readiness

Current Stage: Phase0C catalog/scope and raw V2 semantic coverage.
Cutover Blocker P0: 8 open. Cutover Blocker P1: 4 open.
Catalog Blocker: four remain open; 81 / 205 read-only capture exists, but actual
isolated publication/native marker acceptance is pending operational approval.
Evaluation Gap: governed Gold and actual V1/V2 model-quality measurements remain
unaccepted. Shadow Gap: no actual plan-only traffic comparison.
V1 Replacement Readiness: **NOT_READY**.

Next shortest blocking path: complete field-level/partial temporal edits,
governed defaults/relationships and remaining payload mapping through the same
raw-input entry. Canonical metrics/dimensions/subject choices are covered here;
arbitrary typed-value decisions and compound/dependent same-slot ambiguities are
not silently generalized. Actual semantic margin/alias quality still needs
catalog-gated evaluation. After the pending catalog publication approval,
revalidate the selected 81 / 205 target and native release/trust evidence before
Gold, real-model benchmark and shadow. Formal V1 replacement remains subject to
the final explicit user approval.

## Release and rollback

Development changes are synchronized to `E:/yy` using explicit file manifests,
normalized bytes and SHA-256 checks. Original Oagnet HEAD and staged user changes
remain preserved. Implementation/evidence commits and rollback instructions are
recorded in the accompanying manifests. Final verification requires every tracked
development file to match its committed blob and the version repository to be
clean. The Draft PR stacks on PR #22; no merge or cutover is performed.
