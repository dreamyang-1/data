# Business surface extraction and ASL binding migration

Status: foundation implemented; live route NOT switched. This is not end-to-end
acceptance of the requested architecture.

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

## Remaining work before enabling

Oagnet now accepts optional bounded `surface_evidence` separately from
`intent_asl_contract`. It is generation-only advisory data; retrieval still sees
the unchanged business question. The Agent caller is not wired yet. This does
not complete or enable the migration.

1. Define a bounded advisory evidence envelope separate from confirmed
   constraints. Model-extracted fields must not acquire user-confirmed status.
2. Pass the exact completed question to ASL retrieval and generation; remove
   conflicting text rewriting for this route without losing explicit choices.
3. Update both Oagnet's contract repair and the HTTP adapter's post-processing:
   neither may silently overwrite a catalog-grounded ASL with upstream guesses.
4. Preserve user-confirmed candidates and scope as hard constraints; validate
   omission of requested grouping/filter/time using evidence, not guessed roles.
5. Publish context after successful ASL binding; exercise new question, spoken
   follow-up, pending option selection and a different business domain.
6. Only then enable the bridge path and complete real platform replay. No .env
   switch, service restart or live cutover was performed for this migration.

Integration: A's pending event-lifecycle candidate changes the bridge and
orchestrator. Merge by reviewed diff; do not overwrite these changes wholesale.
