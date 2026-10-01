# CURRENT_PRODUCTION_CONTEXT_PATH

Baseline `899b7b692f7ff06a8660fe99361ba39f8528b1f1`; V1 remains the formal route.

Before: `RawTurnPlanner._run` restored current scope/state/plans, called `v2_current_turn` without task context, gated `_task_context` with parse flags, called `v2_semantic_edits`, then used `TurnResolver.resolve` for relation/target before `_patch` and `apply_task_patch`.

After: same scoped restore → `context_proposal.discover_context` (at most 4, before any parse) → the same `v2_current_turn` call returns `ContextAwareParse` → `accept_proposal` checks native scope, snapshot, membership, task/version and Pending contract → `proposal_resolution` supplies relation/target directly → existing `v2_semantic_edits` receives only the selected task → existing source/explicit-edit guards, TaskPatch and Reducer → `ScopedPlanSession.resolve_turn` rechecks the immutable request-local proposal proof before registering its compiler resolution proof.

`_task_context(..., context_trace=...)` and `semantic_task_schema(..., context_relation=...)` consume the validated selection. The second model cannot replace that target. Lower-level legacy resolver helpers remain available for their original explicit callers and frozen diagnostics; Raw production does not fall back to them when a joint proposal is missing, invalid, ambiguous or unavailable.

Pending summaries may be read from a valid scoped Conversation without a separate resume receipt. Applying an answer additionally requires the native Pending receipt, exact governed option, task/version and option application checks. A full NEW_TASK does not depend on the old resume receipt. No option match or old topic-shift flag overrides the explicit model proposal.

AMBIGUOUS / UNRESOLVED raise `ContextProposalFailure` with a structured context status and existing `TerminalDecision`; no target is guessed, no TaskPatch/Draft is executed, no Pending is fabricated, and state remains unchanged. This closes terminal nonselection, not a new user-facing task-choice UI or Pending protocol. V1/API/SSE are unchanged.

The native logger emits `context_arbitration`: candidate IDs/versions/scopes, model relation/target/status, hard check results, final relation/target/status and override reason. It contains no raw question, prompt, history, SQL, rows or CoT. Current explicit delta and barriers continue to be checked by native patch/reducer guards; the relation trace explicitly does not claim those later checks ran already.
