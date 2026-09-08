# V2 scoped filter and partial temporal edits

Date: 2026-09-08. Baseline: `fc7c5fc2fe63c9dd4284a88d6c268a10b6b72c52`
(Draft PR #23). Branch: `cutover-v2-slot-edits-20260908-01`.

**Source-path acceptance PASS; V1 replacement readiness NOT_READY.** This delivery
closes the raw planner's inability to express local filter and temporal edits.
V-01 remains open for the explicitly listed semantic and actual quality gaps.

## Proven divergence and implementation

The previous model draft exposed only whole structured slots. The frozen reducer
rejects ADD/REMOVE on a filter tree, and a replacement could omit unrelated
conditions. Whole TimeSpec replacement required the model to reproduce old
components. A missing time range could not represent all-time grouping. First
divergence: STRUCTURED_INTENT representation followed by SLOT_MERGE. Review also
proved that a scalar payload could retain a non-NONE time grain while its result
contract required at most one row; the raw materializer now rejects that conflict.

The internal v3 draft offers FilterEditDraft and TemporalEditDraft. Filter handles
bind exact restored task ID/version, tree position and node contents. A handle
from another task or old version cannot be reused. The model receives transient
labels and values for interpretation, with canonical authority IDs removed.
Current parse markers and mention evidence must cover each operation. These
checks prove reference membership and operation consistency, not model accuracy.

ADD without a target adds a new current-bound predicate as an AND with the whole
tree. Targeted ADD extends positive EQ/IN values; REPLACE changes the selected
predicate value and preserves its field; REMOVE checks exact typed membership
or removes the selected subtree. CLEAR removes only the selected subtree. Edits
apply simultaneously against original paths, preserving untouched siblings and
AND/OR/NOT placement. Ancestor/child and duplicate-target edits reject instead
of depending on order. Negative/range membership arithmetic remains unsupported.

Code lowers the result to existing atomic REPLACE/CLEAR operations. The original
user ADD/REPLACE/REMOVE/CLEAR remains in StructuredEditTrace, separately from the
lowered operation; an evaluator must use that distinction. The trace contains
operation, target handle/component, mention IDs and before/after hashes, without
raw question/filter values. The existing reducer retains clear barriers and
invalidates old datasets after semantic changes. Partial clear remains absent
in the current materialized tree; no historical field merge restores it.

Temporal RANGE/GRAIN/ANCHOR edits preserve other components. CLEAR RANGE produces
`range: null`, `source: USER_EXPLICIT_UNBOUNDED`, retaining anchor and grain. It
does not invent earliest/latest dates or a default period. Subsequent grain and
metric edits keep the range unbounded until a new explicit range is supplied.
The shared TimeSpec type adds this opt-in representation; legacy 0.2.1 final
LogicalPlan and PlanEnvelope validation reject it, and frozen 0.2.1 schema files
are unchanged. The authorized 0.2.2 schema is regenerated. Public HTTP/SSE and
legacy request formats are unchanged. New tasks without a TimeSpec still need
an explicit anchor; dependent comparison-period edits fail closed pending their
own typed construction. No unverified comparison is guessed.

## Verification and review

42 new regressions pass: operation contrasts; compound/nested filters; exact
member removal; all-filter barriers; stale, historical and cross-task handles;
overlapping edits; bound-reference/evidence forgery; independent range/grain/
anchor edits; all-time continuation; legacy unbounded rejection; whole-slot
bypass rejection; dataset invalidation; comparison dependency and scalar/grain
conflicts. Two initial new assertions were corrected to compare timezone-aware
instants because the existing StrictModel normalizes dates to UTC. No old test
expectations were changed.

| Service | Baseline passed / failed | Final passed / failed |
| --- | --- | --- |
| DataAnalysis Agent | 2410 / 27 | 2452 / 27 |
| Oagnet | 555 / 8 | 555 / 8 |
| SQL Translator | 200 / 0 | 200 / 0 |

The full offline suite covered all 98 Agent modules and returned 2450/27. After
review added the two-line scalar/grain guard and two regressions, all 434 affected
V2/Phase2.5/Phase2.5.1 tests passed. Final counts are the exact node union, with no
double counting. Critical Suite: 160/160; existing public clarification trace:
89/89. Collection errors, missing prior nodes and old pass to new fail are zero.
Exact reports, hashes and node deltas are in test_delta.json.

All model HTTP responses and catalog data are declared synthetic fixtures.
Actual pin acceptance, reducer, state mutation and compiler execute offline.
This does not measure real recognition accuracy or native deployed publication.
No real model/embedding calls, live catalog reads, index writes, production
state writes, deployment or traffic changes occurred. Oagnet/SQL source and
production defaults are unchanged. Scope remains current-request authority;
trusted-state digests are not signatures for accepting arbitrary client data.

## Prompt and schema review

Only the V2 draft prompt changes, version `v2-current-recognition-v3`; the parse
prompt and all V1 prompts remain unchanged. Deterministic code now supplies and
validates structural editing primitives, so the model must select these existing
types rather than reconstruct history. Natural-language target/component
interpretation still needs the configured recognition client; new prompt text
only describes that interface. Before: only whole-slot edits. After: explicit
typed local edits; contrasts reject wrong operations, invalid targets, authority
IDs, missing members and whole-slot bypass. Regex additions: zero. Source and
schema are the same Pydantic contracts; v1/v2 draft snapshots remain historical.

## Cutover readiness

Current Stage: Phase0C catalog/scope and raw V2 semantic coverage.
Cutover Blocker P0: 8 open. Cutover Blocker P1: 4 open.
Catalog Blocker: actual isolated 81 / 205 publication/native marker acceptance
remains pending the existing operational approval request. The selected scope
is a validation target, not a request default or index publication approval.
Evaluation Gap: governed Gold and actual V1/V2/model comparison remain unaccepted.
Shadow Gap: no actual plan-only traffic comparison.
V1 Replacement Readiness: **NOT_READY**.

Next shortest blocking path: governed default projection/relationship coverage,
dependent temporal comparisons and remaining analytical/decision construction
through the current raw entry; actual catalog publication after approval and
fresh source/target validation. Then the required Gold/evaluation, real-model
benchmark, shadow and controlled canary gates. No V1 replacement is authorized
by this delivery. Ask for final replacement approval only after all gates pass.

## Release and rollback

Explicit files are synchronized from development to E:/yy with SHA-256 and
normalized-byte verification; unrelated tracked files and original Oagnet
staged changes are preserved. Source and evidence commits stack on Draft PR #23.
No merge occurs. See change_manifest.json, git_commit_manifest.json,
workspace_git_hash_verification.json and rollback_manifest.json. Post-commit
verification checks every tracked development file against its committed blob
and requires a clean version repository.
