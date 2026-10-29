# Business surface extraction and ASL binding migration

Status: integration implemented and validated behind
`DATA_AGENT_SURFACE_ASL_EXECUTION_ENABLED`. The shared environment remains an
explicit deployment switch; legacy execution stays available for unsupported or
confirmed-plan shapes.

## Implemented

RawTurnPlanner has an internal opt-in `defer_new_task_binding` (default false).
For a model-recognized, scope-validated, self-contained NEW_TASK, it preserves
the exact question, surface mentions and conversation barrier, without metric
span recovery, candidate binding or the semantic-edit model call. It does not
create an authorized plan from unbound surface evidence. Native planning and
existing follow-up paths remain unchanged by default.

The parse prompt distinguishes role hypotheses from published catalog identity,
and requests separate object/value/grouping/time evidence without replacing
the user's metric wording with an inferred canonical measure.

## Validation

- Bridge + raw recognition test files: 192 passed.
- After bypassing metric-span recovery too, the dedicated handoff regression
  passed again; both catalog-recovery and candidate-binding functions are
  replaced by failing spies to prove they are not called.
- The new test verifies one model invocation, original wording, raw metric
  mention, and unchanged authorized domain scope.

## Integrated behavior

Oagnet accepts bounded `surface_evidence` separately from
`intent_asl_contract`. The exact completed question remains the primary retrieval
and generation input. Mention text gets an additional bounded vector recall, but
role hints remain advisory and cannot authorize a field, metric, ID or scope.

The Agent orchestrator calls this planner for completed ordinary analytical
questions, then sends the unchanged validated ASL through the existing guarded
translation, read-only execution, dataset validation, analysis, table and chart
pipeline. Dataset/dependency/regeneration requests and authorized native plans
continue through their existing envelopes so confirmed constraints are not lost.

Transport validation: 167 passed (new planner, assembler and existing HTTP
adapter tests). This is mocked transport validation, not live platform replay.

SQL integration: `HttpDataRetrievalAdapter.query_surface` calls the new
planner and the same `_execute_validated_asl` execution boundary used by legacy
`query`. The execution boundary retains translator scope, read-only SQL,
relationship, database selection and dataset validation. A three-hop mock
regression confirms Oagnet -> translation -> execution with unchanged ASL.
All 168 planner/assembler/HTTP tests pass after correcting a test fixture to use
the real SQL response `data` key. Existing legacy query remains the default.
Confirmed bindings, lineage, dependencies and analysis contracts are rejected by
the surface entry and remain on the legacy/native guarded route.

Context recognition uses one model response for relation, literal surface
mentions and the standalone completed question. It no longer performs catalog
binding for this route. New tasks keep the exact current question. Accepted
contextual turns receive only the selected prior task's completed question and
produce a new standalone business question. Pending choices remain on the
existing confirmed-choice handler.

Live acceptance covered the original product/department request, city-by-month
sales, the colloquial follow-up `上海市的`, and SSE output. The product request
bound `百特` to the published parent-brand attribute and `Prismaflex M60 set` to
the published specification attribute, producing eight department rows. The
city query returned both city codes and names; its follow-up preserved year,
monthly grain and sales measure. An EAM replay proved the requested half-year
range is retained with the published time anchor; its independent published
global filter `WorkType = A` remains a catalog configuration error and is not
repaired in application code.

Deployment requires matching Agent and Oagnet builds. The flag defaults to false;
turn it on only after both services are restarted from these versions.
