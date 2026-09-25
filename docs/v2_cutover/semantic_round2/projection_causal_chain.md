# Projection Causal Chain — current HEAD 4d12cf6

Scope: only the original 13 cases and existing positive controls. CUTOVER_RELEVANCE is BASE_TASK_BLOCKER; five history prefixes indirectly prevent later follow-up execution. No evidence yet that this projection obligation mutates an existing target/state incorrectly.

```text
User Surface
 -> model CurrentTurnSemanticParse.mentions (exact surface, candidate_roles)
 -> the SAME model artifact separately declares explicit_slot_mentions/operation_markers
 -> repair_model_parse / CurrentTurnParser (span/ref validation, no role->marker rule)
 -> RawTurnPlanner._candidates (each role offers scoped catalog handles)
 -> SemanticTaskDraft selects canonical subject/metric; no projection edit selected
 -> _patch hydrates selected handles and validates their role/current evidence
 -> unused projection marker rejects V2_EXPLICIT_OPERATION_DROPPED
 -> if marker alone removed, explicit_slot_mentions rejects V2_EXPLICIT_SLOT_DROPPED
```

This is the actual temporal order: the explicit obligation is model-declared before binding. It is not synthesized by the candidate generator after seeing a possible role. The original raw artifact and the runtime-normalized artifact must be compared, not inferred from the final reason code.

| Concept | Existing representation / consumer | Interpretation |
|---|---|---|
| A: probabilistic role hypothesis | Mention.candidate_roles, candidate enumeration | Alternatives do not themselves require edits. A mention may have multiple roles. |
| C: asserted explicit, not yet grounded | Parse.explicit_slot_mentions + OperationMarker | Model's assertion of a current user request, checked for consumption even if draft omits it. Not canonical authority. |
| B: bound canonical fact | ScopedPlanSession.bind; typed SlotOperation / ProjectionItem | Must use offered handles, role, current evidence and current scope. |
| D: derived default display | complete_catalog_defaults -> default_projection | After _patch/query-shape checks; is_main_attribute plus owned attribute/mapping validation. CURRENT_REFERENCE_RESOLUTION / PINNED_CATALOG_MAIN_ATTRIBUTES, not CURRENT_EXPLICIT. |

Potential conflation is within model output semantics (role possibility simultaneously asserted as user-explicit projection), not an established automatic runtime role-to-marker conversion. `SEMANTIC_STATE_CONFLATION_CANDIDATE` needs Oracle and explicit-unresolved contrast; no production conclusion before those controls.

DETAIL_ROWS is already a registered ordinary entity-row query shape. RELATION_LIST requires a declared relationship and endpoints. Missing relationship after a projection Oracle is NEXT_DIVERGENCE, not authority to invent a relation or new shape. Default-display evidence for the six involved entities is reused from Round 1; zero-main or conflicting-main metadata guards remain existing local Catalog reasons, with no governance work in this round.

Read: recognition.py parse/draft/schema, _run, _candidates, _hydrate, _patch; pipeline.py CurrentTurnSemanticParse/CurrentTurnParser; models.py Mention/ProjectionSpec; recognition_repairs.py; catalog_mentions.py; catalog_bridge.py binding; catalog_plans.py defaults/validation; registries.py payload/slot contracts; existing raw/catalog-plan/structured/Pending tests.
