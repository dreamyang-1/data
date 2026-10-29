# Operation Dataflow Map — Round 1, before production edits

Baseline: `6cc55e217574a68d7faa66e6d4c4de4c101341c9`. The map was completed before changing production code. It describes the actual `RawTurnPlanner.run` entry, not V1 orchestration or the separate legacy slot compatibility reducer.

```text
Current User Evidence (ChatRequest.question + current AuthorizedSemanticScope)
  -> CurrentTurnSemanticParse (recorded v2_current_turn model stage)
  -> CurrentTurnParser.parse (current-turn spans, IDs and slot registry)
  -> OperationMarker + explicit_slot_mentions (operation evidence)
  -> mentions / scoped candidates / selected handles (operand evidence)
  -> TurnResolver.resolve skeleton (current or explicitly referenced target)
  -> SemanticTaskDraft edits + source/structured edit resolution
  -> RawTurnPlanner._patch (marker consumption, typed canonical operations)
  -> TaskPatch.compile (declarative phases and conflict validation)
  -> apply_task_patch (pure semantic reduction + clear barriers)
  -> TaskSemanticState
  -> materialize_payload (typed SemanticQueryIR)
  -> scoped validation / LogicalPlan compilation / catalog acceptance
  -> apply_state_mutation or NEW_TOPIC (versioned state publication)
```

| Artifact | Creates | Consumes / may reject | Permitted transformation |
|---|---|---|---|
| Current request and scope | Trusted upstream `ChatRequest`; `ScopedPlanSession.__init__` revalidates | `restore` rejects namespace, scope, pin or binding mismatch before model history access | History/model cannot mint or expand scope |
| Recognition artifact | `model.complete(v2_current_turn)` | `repair_model_parse`, `CurrentTurnParser.parse`, `CurrentTurnParseResult.current_turn_only` | Exact lossless span/reference repair; catalog compound metric span recovery; no arbitrary new operation |
| Operation evidence | `CurrentTurnSemanticParse.operation_markers`; `OperationMarker` stores slot, operation, mention | `_patch` and `prepare_ambiguities` require matching current evidence | Parser marker is distinct from editable operand or chosen task |
| Operand candidates | `RawTurnPlanner._candidates`, pinned catalog, current role hypotheses | `_hydrate`, `ScopedPlanSession.bind`, role/source checks | Only offered opaque handles may become current scoped canonical refs |
| Turn / TargetTask | `TurnResolver.resolve` skeleton from parse + restored state; model may select an offered historical handle | Historical-offer guard and `V2_TURN_REFERENCE_UNRESOLVED`; second scoped resolution before compile | Explicit topic shift wins; task context alone grants no target or scope |
| Edit draft | `model.complete(v2_semantic_edits)` | `prepare_ambiguities`, `_patch`, `source_filter_patch` | Collection handle representation repair preserves candidate, role and operation; no generic inference from raw text |
| Source value operand | `resolve_requests` uses offered field/current target + frozen exact lookup/probe evidence | Request-consumption, target, role, field and source proof guards | `removal_targets` may use exact restored-member evidence; no fabricated value/ID |
| Structured filter/time edits | `lower_edits`, `filter_edits`, `temporal_edits` | Target-version handles, overlap, membership and component guards | Fine-grained edit lowers to slot REPLACE/CLEAR with `StructuredEditTrace` retaining the semantic edit operation |
| TaskPatch | `_patch` builds `SlotOperation`; `TaskPatch.compile` groups phases | `_patch` rejects unused markers before compile, then uncovered slots; registry/type/conflict validators follow | Current explicit operation provenance retained. Catalog/default/comparison completion is separate and traceable |
| Reduced semantics / barriers | `apply_task_patch` creates a new data tree | Typed `TaskSemanticState` construction rejects illegal aggregate state | CLEAR creates barrier; explicit assignments/adds remove the barrier; INHERIT respects it; state input is not mutated |
| TaskSemanticState / IR | Reduced typed state; `materialize_payload` constructs payload | Query-shape agreement, `_check_semantic_coverage`, catalog payload and bound-ref checks | IR derives from state, not a second independent model answer |
| Task/state versions | `apply_state_mutation`; `apply_state_event(NEW_TOPIC)` | CAS, message/mutation idempotency, task/pointer/Pending validation | Semantic change increments task version; accepted distinct mutation increments state version. Failed pure mutation publishes nothing |
| Pending | `prepare_ambiguities`, `_create_pending`; exact answer `_answer_pending` | Current option, task/version/scope, blocker identity and admissibility | Pending consumes explicitly deferred operation evidence; source ambiguity preserves nonfilter operations. Unresolved system evidence is not automatically user ambiguity |

Actual operation types are not interchangeable across models: `SlotOperationType` has KEEP/INHERIT/SET/ADD/REPLACE/REMOVE/CLEAR/RESET_TASK; `OperationMarker` permits ADD/REPLACE/REMOVE/CLEAR/SET/INHERIT; direct `SlotEditDraft` permits SET/ADD/REPLACE/REMOVE/CLEAR. The registry excludes direct ADD/REMOVE for structured slots. No new enum is needed.

The observed `V2_EXPLICIT_OPERATION_DROPPED` boundary is the set difference `markers - used_markers` at the end of `RawTurnPlanner._patch`, before `TaskPatch.compile`. It does not prove an operation reached the reducer, nor distinguish missing operands, a model's omitted edit, an invalid slot hypothesis, or a consumer defect. Those require per-case and single-artifact causal evidence.

Reviewed sources: `recognition.py`, `pipeline.py`, `recognition_repairs.py`, `models.py`, `enums.py`, `registries.py`, `slot_reducer.py`, `state_machine.py`, `structured_edits.py`, `source_value_recognition.py`, `catalog_bridge.py`, `pending_recognition.py`; associated raw-turn, structured-edit and Phase2.5.1 contract regressions. Existing declarative CLEAR+ADD phase semantics in `test_atomic_patch_permutations_have_same_fingerprint` must be distinguished from an ambiguous user-level command; tests must not silently redefine either contract.

Dataset executed-receipt and main-chain DryPlan gaps remain unchanged and separate from this map's semantic-operation boundary.
