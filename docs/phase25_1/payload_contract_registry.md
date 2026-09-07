# Payload contract registry

CURRENT_FACT: `app/semantic_v2/registries.py` is the single source for payload, route, goals, query shape, execution backend, result compiler, adapter policy, reuse and capability declarations. `plan_axis_compatibility_matrix.csv` is generated from that source.

CURRENT_FACT: LogicalPlan has no serialized query_shape. Its property calls `PayloadContractRegistry.resolve_query_shape`. ExecutablePlan checks its backend and deterministically compiled ResultContract against the logical plan. Invalid route/goal/payload combinations raise PLAN_VALIDATION_FAILURE and do not create Pending.

CURRENT_FACT: Chat -> CHAT_ONLY / CHAT / CHAT_RESPONSE; dataset transform -> DATASET_TRANSFORM / DATASET_OPERATION / DATASET_LOCAL; lineage -> LINEAGE_GRAPH / LINEAGE / SEMANTIC_METADATA. Forecast requires FORECAST, and root-cause report delivery remains ROOT_CAUSE plus DeliverySpec(REPORT).

CURRENT_FACT: ReportPayload is REPORT_COMPOSITION over nonempty existing source_task_ids. ControlAction has CANCEL, CONFIRM, REFRESH and REVISE only. Help and out-of-scope have dedicated payloads.

CURRENT_FACT: SlotDefinitionRegistry is the second core registry. Algorithm policy is an immutable table inside PayloadContractRegistry; SchemaMigrationRegistry is a façade over one pure compatibility function rather than another extensible runtime registry.

CURRENT_FACT: The registry's backend value declares the eventual contract destination. ExecutablePlan.backend_contract.mode is SHADOW_ONLY. Adapter capability/proof checks remain decisive: no ASL 1.0 V2 query compiler, local dataset compensation, or multi-step forecast implementation is fabricated.

UNKNOWN: Production metric/metadata browsing, governed default display policies and production catalog calibration require owner/backend confirmation. Fixture identities are explicitly declared test-catalog IDs, not production catalog IDs.
