# Completed-question presentation boundary

Current stage: bounded V1 presentation fix; no V2 cutover or query-policy change.

## Root cause (PROVEN)

The context bridge publishes `V2_RESOLVED_INTENT_CONTEXT_READY` with internal
`INTENT_RECOGNITION/RUNNING`. The public stream previously waited for a later
classification-completed event to open planning. During the intervening planner
call, both heartbeat and later parameter milestones could remain in intent.
The deployed early-planning patch still waited for the same late signal.

## Exact change and preservation

- Runtime: `app/api.py` only. Tests: `tests/test_api.py`.
- Use the existing context-ready milestone as the public boundary: finish the
  completed-question display, then immediately open planning. Internal business
  events, model calls, decisions and request/response contracts are unchanged.
- Later classification summaries are displayed under planning. Late intent
  events after execution begins cannot reopen an earlier public node.
- Preserve the independently deployed early-planning-running behavior. Completed
  planning summaries still wait for readiness; clarification is not advertised
  as executable. Child-intent suppression and composite ordering stay intact.
- No frontend assets, SQL/ASL logic, model prompts, scopes, configs or credentials
  changed. No edits to the unrelated NL Agent output module.

## Verification

- Pre-change API baseline: 66 passed. API after change: 70 passed.
- Critical API/semantic-choice/composite/clarification/DAG suite: 204 passed.
- Four added stream scenarios exercise normal, composite, clarification and
  confirmed-question presentation, slow-planner heartbeats, late events and the
  frozen six-node order. Existing pending restoration tests also pass.
- STALE_TEST: the old intent-document assertion now accepts planning chunks for
  post-completion parameter content, per this explicit presentation decision;
  all its content and ordering assertions remain.
- Full offline baseline is the preceding release: 4317 passed / 11 old MCP
  failures. Final fixed-clock run: 4321 passed / the same 11 old failing IDs,
  no new failures or collection errors.
- An exploratory run also hit a 30ms analysis-budget test under load; isolated
  rerun and the final full run both passed unchanged. An earlier socket-blocking
  harness prevented Windows asyncio socketpair setup; that invalid run was
  discarded and the established offline harness was used for the final delta.

The separate catalog/evaluation/shadow gaps and V1 replacement readiness are
unchanged. Next step is exact-file deployment and public streaming verification,
not semantic publication or V2 replacement.
