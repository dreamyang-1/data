# Magic-string control plane audit

CURRENT_FACT: Full-file AST scanning finds legacy occurrences of DEFAULT_TIME_RANGE, DEFAULT_TIME_GRANULARITY, SORT_DIRECTION, TIME_SCOPE and SET_RELATIONSHIP_PROJECTION. Exact source paths/lines are in `magic_string_audit.json`.

CURRENT_FACT: No occurrence of those legacy controls exists in semantic_v2 execution code. V2 execution structures use TimeSpec, RankingSpec, ScopePolicyDecision, ProjectionSpec, RelationshipSpec and DeliverySpec. Readiness uses typed blockers; diagnostic PlanEnvelope.unresolved_items and assumptions are compatibility/debug fields and do not drive V2 compilation, routing, state mutation or result proof.

CURRENT_FACT: Legacy assumption branches remain frozen and are not represented as safe V2 execution policies. Registry identifiers and error/message codes identify closed contracts or diagnostics; free display text is not reparsed to choose execution behavior.

PROPOSAL: A later production migration must replace legacy control strings at its own compatibility boundary after business/ASL support is proved. This phase intentionally does not rewrite the legacy parser or orchestrator.
