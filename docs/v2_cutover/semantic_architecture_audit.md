# Current semantic execution boundaries

Reviewed against the current source and the frozen 81/[205] receipts. This audit
extends existing evidence; it does not repeat the completed identity/Redis audits.

| Stage | Actual implementation/evidence | What is still unproved |
| --- | --- | --- |
| Current turn | recognition.py -> CurrentTurnParser; exact spans and strict mention references | Role hypotheses and selected-axis purity are not final role/binding F1. |
| Task target | pipeline.py TurnResolver; current pointer, explicit history selection, new-topic precedence | Full unlabeled task-recall and Pending scenarios are not covered by the 20-case live runner. |
| Task context | recognition.py _task_context; only scoped restored current state or history candidates | Context is descriptive, never authority. Later target/scope guards still execute. |
| Candidate offers | recognition.py _candidates enumerates pinned catalog rows for each offered role | This is not a ranked native vector search. Recall@1/3/5 requires explicit ranking observations; no scores are invented. |
| Entity values | source_value_recognition.py and Oagnet catalog_value_sources.py; bounded exact read with current scope and mapping proof | Frozen source-value observations are missing from the live evaluation harness. Names/suffix aliases and probe fallback are not silently assumed. |
| Binding | catalog_bridge.py scope pin, proof-backed binding, exact offered handle resolution | Candidate/role selection still requires independent Gold/Oracle truth. |
| State | TaskPatch and deterministic slot reducer; scoped task versions and clear barriers | Whole-state safety observations remain incomplete in the live harness. |
| Logical plan | catalog payload validation, scope/provenance checks, payload materialization, LogicalPlanCompiler | Selected-plan executable flags/constant scores in _resolution do not prove SQL, result correctness or complete Dry Plan coverage. |
| Executable artifact | compile_executable_plan creates SHADOW_ONLY backend contract and adapter assessment | RawTurnPlanner calls compile, not compile_asl2, and executes no SQL. The type name is not production execution evidence. |
| ASL / SQL planning | existing separate compile_asl2 -> lower_asl2 -> pinned SQL planner, acceptance fingerprints | Must be connected to full semantic evaluation without production SQL; previous supported-shape limitations remain. |
| Identity / grain / fanout | completed identity matrix and existing catalog/ASL/translator tests | Provisional keys are snapshot evidence. Two relation endpoint and metric-subject ambiguities remain shape-specific; global grain/fanout coverage is not declared PASS. |
| Gold / Oracle | reuse existing 100 parser axis records, 20 transition records and deterministic control tests | Private validation/holdout, full mention labels, role Macro-F1, candidate ranking, full Oracle/whole-plan labels remain open. |
| Redis / publication | existing read-only runtime/deployment receipts and prepared native publication candidate | Production restoration/publication gates Canary/Cutover, not these offline activities. |

V1 remains production. Public API/SSE formats, current authorized scope and the
user-selected model configuration are unchanged. `.eval_private/` is excluded
from Git; existing temporary private captures are not moved or published.
