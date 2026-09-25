# V2 Evaluation Harness Contract

Starting commit: `ec48f654d6bcaa48ed09d41d7aca4fcbaedcd82e`, Draft PR #52.
Stage: `V2 EVALUATION HARNESS CLOSURE`. V1 remains the production route.

The 73/100 parser and 6/20 transition scores are **PRE-HARNESS BASELINE**.
They are different evaluation units and are never pooled into V2 accuracy.
No production semantic source, prompt, schema, public I/O or routing changes are
included in this harness implementation.

The existing `run_raw_transition_benchmark.run` owns sequential execution. Its
opt-in harness adapter uses the same `RawTurnPlanner.run`, scoped publication,
restore, resolver, patch, reducer and compiler. Each successful history result's
actual sealed state and plan become the next turn's inputs. Gold labels and
metadata are excluded through an execution-input allowlist. A failed history
stops the case; it never injects a successful precondition.

`RuntimeObserver` wraps calls only inside the isolated evaluation process and
returns the original results or exceptions. Successful and rejecting recorded
turns verify instrumentation-on/off equality. Raw inputs, model outputs, state,
candidate contexts and native SQL diagnostics are private. Public reports contain
IDs, bounded reason codes, hashes, observation coverage and labeled-axis outcomes.

## State fixtures and current implementation boundaries

Pending preconditions use actual `PendingClarification`, `PendingBlocker`,
`PendingResume`, `TaskState` and `ConversationState` models; the runtime state-event
builder, pending identity function and scoped seal/restore perform construction
and roundtrip checks. Choices bind current catalog records. This represents the
user-declared existing Pending precondition. It does not claim the history text
produced a catalog synonym collision, nor add aliases to the catalog. Tests cover
remaining questions, same-task resume, version, operation and scope mismatch, and
new-topic detachment.

Dataset fixtures roundtrip the actual `DatasetReference` and preserve scope,
columns, counts, snapshot, time, metric metadata, lineage and truncation proof.
Missing ordering/ranking proof stays NOT_PROVEN. The current V2 `DatasetState`
cannot carry that executed-snapshot contract; `ScopedPlanSession.seal(DATASET)`
explicitly refuses creation from plan-only state. This is an exercised
`IMPLEMENTATION_GAP`, not a skipped capability or a fabricated Dataset adapter.
The report distinguishes attempted fixture/capability checks from executed turns.

Two old historical-return Gold records omit the referenced task. One parser Gold
Dataset record omits the Dataset proofs. These are explicit fixture gaps; no
unseen prior questions, scope or execution receipts are invented.

## Observation and evaluation rules

Case and turn denominators are separate. Unlabeled history execution is never a
semantic PASS. Partial labels retain their scope; FULL_PLAN requires the complete
declared axis set. Missing observations remain BLOCKED/NOT_OBSERVED. HTTP, timeout,
schema, semantic, evaluator, fixture, runtime and catalog failures remain distinct.
Every nonpassing case has one first divergence; later mismatches are downstream.

Scope is mandatory even for turn-only labels. Accepted bindings are compared with
native permission proofs and current scope; integer scope IDs versus string
binding identifiers use the explicit runtime representation conversion. Generic
comparison still rejects `true == 1`. Collection order is ignored only on known
set-valued axes; duplicates and ordered output projections remain significant.
PUBLIC_DEV mention labels cover selected spans and allowed roles, not complete
mention precision. Production/model outputs cannot become label authority.

The existing typed `PlanPayload` is observed as the current semantic IR
representation, with the representation named explicitly. Existing
`lower_asl2 -> translate_pinned_catalog` is available as a supplemental diagnostic
on the identical restored accepted plan. RawTurnPlanner does not call that seam;
its result must never be reported as an observed step of the original raw entry.
Neither path executes business SQL.

Replay uses original scoped artifacts and frozen captured model stages. A hard
guard denies model HTTP, database calls, Redis commands and all socket connects,
including attempts made with the model-network ContextVar enabled. Prompt/schema,
scope, pin and source-evidence checks stay exact. Semantic comparison is separately
defined for legal collection ordering; business time, task identity/version,
catalog identity and output projection order cannot be ignored.

## Freeze, split and publication policy

PUBLIC_DEV contains the 100 exposed parser records. The 20 transition records are
also exposed development evidence, with their own denominator. Private validation
and blind holdout are newly composed after prompt freeze from reviewed explicit
metric-operation contracts and frozen catalog facts. Their programmatic origin
is disclosed; they do not claim independent human adjudication or production
representativeness. Concrete blind questions are not opened or run. Exposure
promotes a holdout to development and invalidates its blind status.

Prompt text and production schema are frozen. Candidate universe, catalog,
source-value evidence, code, evaluator and model request configuration have
separate hashes. A frozen all-pinned candidate universe measures selection under
that offer; it does not establish live ranked retrieval quality. Provider seed,
top_p and max_tokens are not sent by the current client and must not be described
as fixed supported settings. No semantic candidate baseline or formal model
comparison is authorized while harness or semantic safety gates remain open.

The current work is a harness candidate. Final closure requires the post-harness
run, artifact parity evidence and remaining-gap review; passing unit tests alone
does not declare `EVALUATION_HARNESS_CLOSED`.

## Review corrections before final replay baseline

The final scorer separates span-boundary failures from role failures when exact spans match. A mention-only discrepancy cannot establish a wrong silently accepted plan: that safety assertion requires an independently labeled plan or state axis. Transport and draft-schema failures retain distinct stages. Common-set comparison refuses differing frozen inputs or missing runtime parity.

Immutable captures created at the initial harness commit remain unchanged. The final evaluator uses the exact same captures and native runtime with sockets and external clients denied. Incomplete typed responses use the existing RecognitionModelClient with an exact MockTransport and the captured finish reason; missing response evidence is rejected rather than fabricated. Capture commit and evaluator commit are reported separately. Supplemental ASL2 lowering and pinned SQL planning are observed outside RawTurnPlanner and are never reported as integrated production execution.
