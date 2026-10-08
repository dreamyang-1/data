# CURRENT_CONTEXT_PROPOSAL_DATAFLOW

Baseline: `466482b67703bf19214ef54c852c606ee14be109`. Production source is unchanged in Round 4. Findings below are PROVEN by the listed current source; the implementation is not inferred from earlier reports.

| Question | Current production V2 |
|---|---|
| Which model supplies relation meaning? | RawTurnPlanner calls RecognitionModelClient for `v2_current_turn`; it emits acts, references, follow-up and topic-shift signals. Configured model is qwen3.7-max. |
| What task context does it see? | Only current question, turn ID, clock and slot registry. No active/recent/historical task summaries or Pending summary. |
| Can it choose TargetTask directly? | The first output cannot. The second semantic draft can choose an offered historical handle; it cannot make an independent ordinary relation/active-target proposal. |
| When is history offered? | Only when first-parse reference_signals contains HISTORICAL and topic_shift_signals is empty. All restored tasks are then offered; no small task cap exists. |
| Who controls exposure? | RawTurnPlanner._task_context and semantic_task_schema, using first-parse signals and an early TurnResolver result. |
| Can soft policy override model signals? | Yes. Fixed topic-shift → historical → dependency → new precedence resolves competing model fields. Non-destructive act-only signals can be ignored. These are semantic policies, not scope invariants. |
| Who decides final relation/target? | TurnResolver derives both; RawTurnPlanner and ScopedPlanSession validate handles, scope and state and invoke it again around patch/plan construction. |

Evidence: `app/semantic_v2/recognition.py` (schema, `_run`, `_task_context`), `pipeline.py` (CurrentTurnSemanticParse, TurnResolver), `catalog_bridge.py` (`restore`, `resolve_turn`). Source hashes are pinned in `round4_manifest.json`.

Current chain:

`scope-checked restore → contextless first LLM parse → deterministic exposure/relation policy → semantic draft with gated history → TurnResolver → native patch/reducer → resolution validation`.

Therefore **STRUCTURAL_CONTEXT_PROPOSAL_GAP_CONFIRMED**. The independent relation proposal is absent; relation is reconstructed from other fields. `missing_slots`, sentence length and confidence-score competition do not decide the V2 resolver. This does not excuse exposure gating or soft-field precedence.

Experimental chain, isolated in tools:

`scope-checked restore → bounded candidate discovery independent of parse → existing first LLM call jointly emits current surface parse + relation/target proposal → hard validation → diagnostic native resolver/patch/reducer`.

The first call is reused; no additional context-resolution round trip is proposed. Task summaries include status, labels, filters, time, known shape, barriers and task version order. Pending includes expected answer type and governed options. They are data, not model instructions or authority. Last semantic delta is not stored by current TaskVersion and is marked unavailable. Scope comes only from the current request/session; all candidate state must pass native restore before exposure.

Hard vetoes: scope mismatch, state snapshot/version drift, forged/unoffered target, invalid task version/status, relation/target inconsistency, Pending identity/version or answer mismatch. Native guards retain Catalog validity, explicit delta consumption and CLEAR/REMOVE barriers. Soft evidence never changes an accepted experimental proposal; no 0.x model-versus-rule competition is added.

Production still lacks a consolidated arbitration event. The experimental trace records MODEL_RELATION_PROPOSAL, MODEL_TARGET_PROPOSAL, CONTEXT_CANDIDATES, HARD_CONSTRAINT_RESULTS, SOFT_EVIDENCE, FINAL_RELATION, FINAL_TARGET, OVERRIDE_DIRECTION and OVERRIDE_REASON. Summary hashes replace full prompt/CoT storage in published evidence. `CURRENT_DELTA_AND_BARRIERS` explicitly identifies checks delegated to the native pipeline, so relation validation is not misrepresented as full state safety.
