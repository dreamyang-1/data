# Diagnostic Context Proposal Contract

`CONTEXT_PROPOSAL_SCHEMA_GAP` is confirmed in the production v8 parse. This document and `tools/cutover/context_proposal_schema.py` are an **experimental contract**, not a change to production Parser Schema, HTTP/SSE or routing. The schema is committed separately.

The experiment subclasses the existing `CurrentTurnSemanticParse` and adds exactly one `context_proposal`. It reuses existing DialogueAct names: NEW_TASK, CONTINUE, MODIFY, ADD, REPLACE, REMOVE, CLEAR, CORRECT, DRILL_DOWN, RETURN_TO_TOPIC, ANSWER_CLARIFICATION. No second full parser or new authorization type is introduced.

| Field | Meaning |
|---|---|
| status | ACCEPTED / UNRESOLVED / AMBIGUOUS |
| relation | An existing relation/act name; null for unresolved proposals |
| target_task_id | One offered task ID; null for NEW_TASK or unresolved |
| state_version | Strict integer copied from the current supplied state |
| task_version | Selected existing task version; null without target |
| pending_id | Offered active Pending ID only for ANSWER_CLARIFICATION |

NEW_TASK cannot select an old target. Current follow-up/modification selects the active candidate. RETURN_TO_TOPIC selects a non-active offered candidate. An unclear historical reference must not select a fabricated or unoffered task. Candidate membership, scope, state/task version, task usability and Pending admissibility are validated after semantic interpretation. An invalid proposal fails closed without choosing another historical task.

The model receives at most four scope-restored task summaries, active-task identity and structured Pending slot/options. Discovery does not receive a parse or relation. The cap is a bounded experiment: the frozen corpus has at most two stored tasks; existing controls have three. Active and Pending get priority, then recent task-version order with stable ID tie-breaking. There is no claim of calibrated optimal recall, no semantic search index and no unlimited history. Older omitted targets remain unresolved. Task Version stores no last semantic delta, so that field is explicitly unavailable, never inferred from chat text.

Relation resolution cannot mutate current mentions, operation markers or explicit-slot declarations. The compatibility adapter used only by native diagnostics projects four legacy relation-signal fields from a validated proposal. It does not modify slot values or relax TaskPatch/Reducer guards. This adapter is not imported by production.

The 12 live proposal calls used `context-proposal-experiment-v1`. One response proposed AMBIGUOUS with a RETURN_TO_TOPIC hypothesis and no target; Python rejected its non-null relation. Self-review found that these cross-field predicates were implemented by a Pydantic validator but absent from the exported JSON Schema. **This is an experimental schema-export gap, not proof of a production model limitation.**

`context-proposal-experiment-v1.1` exports the same existing consistency predicates as JSON Schema conditions. It does not change the validator, loosen acceptance, alter labels or turn the rejected v1 response into PASS. The final exporter is covered by offline tests; v1.1 has not been live tested. The v1 request bodies and all original outcomes remain frozen privately. No additional prompt rules or model calls were used to tune that individual input.

An original-schema raw turn and this experimental-schema turn are different versioned inputs. Their metrics cannot be merged into production Gold accuracy. Prompts contain no case IDs or expectations. Transient contexts and raw model responses remain private; published arbitration evidence contains bounded IDs, reason codes, scope metadata and hashes, not prompts or chain-of-thought.
