# Current-turn slot-name representation repair

## Cause and evidence

- PROVEN from the reported diagnostic and matching deployed validation path:
  current-turn recognition rejects an `operation_markers[0].slot_name` enum
  before the result reaches the parser, task planning or ASL. This is not a
  missing business parameter, SQL error, or missing vector value.
- PROVEN: generation already advertises a finite slot registry, but the model
  transport retries only network/HTTP faults. Invalid slot vocabulary is a
  terminal schema failure, even when the user's question can be completed.
- UNKNOWN: the original out-of-enum string and the preceding conversation are
  not retained in the inspected recent logs/cache. Production trace content
  capture is disabled. Examples in regression fixtures are synthetic, not claims
  about that historical model response.
- The inspected deployed recognition and transport sources match the versioned
  baseline. No broad remote synchronization is needed for this repair.

## Minimal change and invariants

- `app/semantic_v2/recognition_slot_repair.py`: explicitly distinguish internal
  slot vocabulary, business role labels, query shape and concrete field names.
  Identify only slot-name enum / slot-map key errors. Offer a finite name-only
  correction schema; ambiguous meanings can return null and remain rejected.
- `recognition_client.py`: one bounded corrective model decision only for that
  current-turn representation fault. Normal requests keep their existing call
  count. The repair request cannot produce a new question, values, dates, scope,
  operation type, mention, task target, or query shape. Renames are applied by
  deterministic code, then the exact original dynamic schema is checked again.
  A failed repair preserves the original diagnostic, not a fabricated slot ask.
- `recognition.py`: add the vocabulary explanation to the shared extraction
  contract, preserving the existing lightweight/full prompt selection behavior.
- No arbitrary alias guessing, evidence deletion/merging, relaxed enum, changed
  authorization, altered time parameters, SQL execution, new public API/SSE
  fields, node reordering, or production routing change.
- Existing Pydantic, current-turn mention, context target/version, scope and
  downstream binding checks still execute after successful representation repair.

## Verification

- Initial 24 new tests against baseline: 17 failed / 7 passed. Final dedicated
  suite includes 25 passing tests, including a follow-up handoff preserving the
  completed question, previous period/region and accepted task context.
- Critical/API/SSE, current-turn, strict-schema and repair regression: 336 passed.
  Both streaming and non-streaming model responses are covered, as are multiple
  bad names, explicit-slot maps, null/invalid correction, collisions, transport
  failure, unrelated schema violations and unchanged downstream evidence checks.
- The initial full run exposed one lightweight prompt equality regression. It
  was fixed in code by placing the rule in the shared prompt constant; no old
  assertion was relaxed or deleted.
- Final DataAnalysis full regression: 4,529 passed (baseline 4,504 plus 25 new
  tests), zero remaining regressions and zero collection errors. Verification
  used a clean worktree excluding the paused full-result-return edits.
- A read-only candidate probe using the configured server model completed the
  synthetic follow-up `给出具体的销售订单明细` from a Q4 2025 Shanghai sales summary,
  retained the period and region, and selected DETAIL_ROWS. A separate bad-slot
  replay passed the real model's bounded correction. No SQL/session writes.

The paused full-result-return changes and Oagnet's seven previously disclosed
remote binding failures are outside this DataAnalysis-only release.
