# Runtime / Evaluator Parity

| Artifact | Status | Evidence / limits |
|---|---|---|
| AuthorizedSemanticScope | IDENTICAL_CONTRACT | ScopedPlanSession request scope and fingerprint; All executed turns use strict 81/[205]; current request owns authority. |
| CurrentTurnParseInput | IDENTICAL_CONTRACT | CurrentTurnParser.parse and exact model request bytes; Observer returns original value/error; current question only, no labels in model context. |
| PreviousTask / TaskSemanticState | IDENTICAL_CONTRACT | RawTurnPlanner state restore; prior next_state equals next before.state; Actual state chain; a failed history prevents later turns. History text is not a replacement for state. |
| RecentTasks | IDENTICAL_CONTRACT | Captured plans passed to RawTurnPlanner; Exact previously accepted scoped plan artifacts; no evaluator history reducer. |
| HistoricalTasks | IDENTICAL_CONTRACT | RawTurnPlanner._task_context; Real current task map and offered opaque handles. G81-076/G81-088 preconditions are absent and remain fixture gaps. |
| PendingState | ADAPTED_EQUIVALENT | PendingClarification + PendingResume + apply_state_event + seal/restore; Two declared Pending preconditions; typed serialization/version/scope/remaining question and resume tests pass. Does not claim history generated an ambiguity. |
| DatasetReference | MISSING | Native V1 DatasetReference roundtrip; V2 plan-only seal rejects executed result; S81-018..020 fixture complete at V1 contract; V2 executed receipt/state adapter absent. G81-091 lacks source Dataset proofs. |
| Catalog Snapshot | ADAPTED_EQUIVALENT | Actual publication/pin contracts using isolated MemoryStore/FakeRedis; Exact certified 81/[205] snapshot; production publication and persistence are not being measured. |
| Candidate Set | IDENTICAL_CONTRACT | RawTurnPlanner._candidates on frozen catalog rows; Native offered candidates and schema handles are captured. Static evaluation vectors are not live ranked retrieval. |
| Current Clock / as_of | IDENTICAL_CONTRACT | Injected aware RawTurnPlanner clock and captured model clock; 2026-09-09T09:00:00+08:00; Asia/Shanghai; gregorian-business-calendar-v1. |
| TurnResolutionInput | IDENTICAL_CONTRACT | TurnResolver.resolve and ScopedPlanSession.resolve_turn; Both provisional and final calls observed, including original failures. |
| SemanticResolutionInput | IDENTICAL_CONTRACT | ScopedPlanSession.resolve_turn semantic_resolution; Actual resolved contracts; rejected upstream stages are not substituted. |
| TaskPatchInput | IDENTICAL_CONTRACT | RawTurnPlanner._patch and actual resolution.task_patch; Current parse/edit/base evidence; inner rejection code retains precise boundary. |
| TaskSemanticState | IDENTICAL_CONTRACT | apply_task_patch and sealed next_state; Real reducer result and barriers; no reconstruction from expected labels. |
| SemanticQueryIR | IDENTICAL_CONTRACT | materialize_payload typed PlanPayload; Existing typed IR observed. No second invented SemanticQueryIR schema. Full Plan truth is absent. |
| DryPlanInput | MISSING | Supplemental lower_asl2 -> translate_pinned_catalog seam; Existing native seam runs separately on captured plans; RawTurnPlanner does not invoke it. No integrated dry-plan parity claim. |
| LogicalPlan | IDENTICAL_CONTRACT | ScopedPlanSession._compile_logical -> AuthorizedLogicalPlan; Actual scoped plan, permission proofs and state identity; no real SQL execution. |
