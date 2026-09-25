# Contract boundaries and rollback

CURRENT_FACT: All new runtime contracts are offline Shadow foundations. CurrentTurnParser validates only one supplied raw message and recorded small-stage parse; it does not invoke a real model. TurnResolver resolves reference dependency before readiness. LogicalPlanCompiler accepts validated stage artifacts/candidate selections and trusted scope evidence. QueryShape and backend are registry-derived; result contracts are compiled deterministically.

CURRENT_FACT: Final logical plans detach and deeply freeze nested data, and retain message references/digests rather than mandatory raw text. BoundSemanticRef has no resolution_status. Recursive checks cover payload, filter values, temporal anchors, comparison baselines, ranking, relationship, projection and compiled output refs. Authorization proof binds tenant/user/application, decision/policy, canonical identity and catalog/model version.

CURRENT_FACT: The new execution path is TaskSemanticState -> declarative TaskPatch -> one StateMutation CAS. Explicit input outranks history/defaults, CLEAR blocks inheritance, canonical ADD is idempotent, replacements remove old values, SET collections canonicalize order, ordered projection remains order-sensitive. A no-op may record a message/state audit transition but cannot create TaskVersion. REFRESH/RETRY are new ExecutionAttemptRecord values against the same semantic version; a changed semantic patch creates at most one newer version.

CURRENT_FACT: PlanEnvelope, TaskSlotState/apply_slot_operations and apply_state_event are documented 0.2 compatibility surfaces. Production V2 must use the typed pipeline and StateMutation. Compatibility event payload dictionaries are not the future execution state. Old Python constants EXECUTED/EXECUTING/FAILED/EXECUTABLE are aliases to RESOLVED for import compatibility, and never appear as distinct serialized TaskVersion statuses.

CURRENT_FACT: Pending is persisted by task/topic/version, with active/suspended/resolved/cancelled status. Answers are option IDs, must actually apply the selected typed value, and cause all existing blockers to be rechecked. Topic switches suspend rather than discard. A wrong option or CAS conflict leaves input state unchanged.

CURRENT_FACT: Numeric thresholds use Decimal. Naive datetimes are rejected; aware datetimes normalize to UTC; IANA zones and fiscal/default policy IDs are checked. Forecast horizon stores periods and grain, not an unqualified integer.

CURRENT_FACT: DatasetAncestry records root/parents/transforms/order/task version. Truncated snapshots cannot establish global aggregation or global Top-N without appropriate source/snapshot proof. The A0=22/A1=5 frozen dataset fixture differentiates current-slice and original-root requests without implementing a production reference resolver.

CURRENT_FACT: Result proof checks logical output binding, ordering, bounds, grain, cardinality/uniqueness, snapshot and numeric rules. Unknown blocking proof forbids completion; advisory unknown yields a warning. Adapter compensation remains unsafe/unsupported unless actually implemented and proved. No compensation implementation is claimed in this phase.

CURRENT_FACT: 0.2 migration preserves its source fixture and reports LOSSLESS/LOSSY/UNSUPPORTED. Implemented finite mappings are lossless; unknown algorithm/horizon or missing data is UNSUPPORTED. The LOSSY status is reserved, not used to disguise unsupported migration. Old fixtures remain readable through the migration result even when they cannot become executable 0.2.1 plans.

UNKNOWN: Complete production business gold remains unestablished: 200 migrated legacy-intent rows are PARTIAL with null unlabeled fields. Contrast and metamorphic tests prove declared contract properties. Five 20-turn cases are real stateful deterministic fixture replays, not recordings of live users or measurements of model accuracy.

PROPOSAL: Rollback is to stop consuming the Shadow package or revert only the feature branch commits. No production state/data migration, durable event store, vector memory, live N-best, real-model parser, ASL v2 rollout or production write was introduced. Preserve preexisting workspace changes during any later reconciliation.
