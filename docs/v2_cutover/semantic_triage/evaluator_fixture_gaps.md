# Evaluation infrastructure gaps before further semantic changes

Audited HEAD: `0e408509126aae0a3a324aff02aef848fd0d3c56`.
No production code, Gold expectation or acceptance threshold changed here.

| Gap | Current evidence | Required next-stage work |
| --- | --- | --- |
| Parser proxy vs actual turn/state entry | `run_parser_benchmark.py:25` projects references/follow-up signals into a turn label. It receives no history and does not call TurnResolver. The runtime also recognizes consistent REMOVE/CLEAR evidence. 100 records are component Gold; 42 even contain history that the parser runner does not execute. | Reuse the real RawTurnPlanner/current-scope restore/TurnResolver/reducer path to measure the requested runtime axes. Keep parser scores as separate diagnostics and original 100 labels unchanged. Do not “fix” runtime from this proxy alone. |
| Pending/Dataset initial state fixture | `run_raw_transition_benchmark.py` explicitly skips `initial_pending` or non-null `dataset`; S81-016/018/019/020 are all NOT_RUN. This is one shared entry gap with Pending and Dataset fixture variants. | Build reviewed scoped Pending/task/plan/Dataset fixtures through existing seal/restore APIs. Preserve incompleteness/ownership and candidate authenticity. Execute the same entry, including new-topic admission and local-vs-global ranking. Do not inject successful history or relax scope/digest checks. |
| Region observation | V2 observation adapter emits metric/dimension/time axes but not region_values/region_operations. Seven region-value cases and three region-operation axes are unobserved in the latest run. Several currently halt before reaching their target turn. | Observe actual canonical predicates and operations, with an explicit label-to-canonical comparison contract. Do not take region answers from Gold or turn a readable label into an authorized field choice. |
| Safety observation | All 7 semantic safety gates remain NOT_EVALUATED. Wrong inheritance has 17 required observations and 17 missing; Pending has 1 missing; truncated ranking has 3 missing. Offline Critical 160 passed is a separate regression denominator. | Compare actual prior/current state, unrelated slots, source scope, Pending detach/restore, dataset completeness and chosen execution route. Add negative observations instead of constant `false` flags. Missing observations cannot be a zero-violation PASS. |
| Complete Gold/Oracle labels | The 100-case evaluator reports only selected mention/role, shape, operation and turn proxies. Canonical binding, full task state, whole IR/dry plan and complete role/mention precision are unlabeled. Original output-only Oracles reuse later recorded model outputs. | Add reviewed labels to the existing inventory and reuse the existing runners. Report Oracle Mention/Role/Candidate/Relation separately; regenerate downstream model stages where an intervention changes their inputs. Until then no end-to-end accuracy denominator exists. |
| Candidate ranking and snapshot | `_candidates` enumerates all compatible pinned catalog rows. Evaluation uses constant 2D vectors; selected-plan scores are not ranked retrieval evidence. Current private candidate contexts are hashed but cover only executed turns. | Freeze an offered candidate inventory and real ranking observations on a reviewed corpus before comparing models. Report Recall@1/3/5 and missing candidate truth explicitly; do not infer recall from a selected candidate or vector placeholder. |
| IR and Dry Plan boundary | RawTurnPlanner uses `compile`, not the separate `compile_asl2`/pinned SQL planner path. `SHADOW_ONLY` executable artifacts are not dry SQL planning evidence. | Connect the existing supported-shape lowering and pinned translator to evaluation without executing production SQL. Record unsupported shapes and first rejecting boundary, rather than counting adapter flags as executability. |
| Blind holdout and freeze receipts | All current 100 + 20 records have been inspected during development. No protected holdout/access provenance or accepted joint prompt/schema/evaluator freeze was found. | Define independently reviewed, unexposed holdout provenance and versioned freeze/change control. Do not rename the development corpus into a blind set. |

Observed evaluator/fixture first divergences affect 10 records: five turn proxies,
one dataset-shape proxy, and four skipped state fixtures. These are counted once
per record in the case inventory. The broader coverage gaps above can affect
otherwise passing records; they are acceptance gaps, not additional independent
bugs to be added to the 41-case distribution.

The next stage must prioritize this infrastructure. Critical NOT_RUN cases cannot
be carried indefinitely. This checkpoint neither creates those fixtures nor
continues business-semantic patches; it ends with the requested triage report.

Redis recovery and native catalog publication remain Canary/Cutover dependencies.
The certified 81/[205] snapshot supports offline evaluation. No current Gold case
is classified BLOCKED solely because Redis recovery/publication is incomplete.
Identity/relation/catalog gaps remain specific to strict query shapes and do not
globally block mention/role/turn evaluation.
