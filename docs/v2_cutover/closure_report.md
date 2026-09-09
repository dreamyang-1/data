# V2 replacement readiness — current task and destructive edits

Current Stage: CURRENT_TASK_CONTEXT_AND_DESTRUCTIVE_EDIT_P0.
Baseline: `a87df7d37756d160f93db2acf9b9de5af928f8a3` / Draft PR #43. Branch: `cutover-task-context-20260909t042538z`.
Final commit: the commit containing this report. **NOT_READY** for replacement.
The inherited production acceptance groups remain 8 P0 / 4 P1; fixed code defects
and those broader acceptance groups are different counts.

## Proven root cause and bounded fix

The live REMOVE parse already supplied a single REMOVE dialogue act and current
REMOVE markers, with empty followup/reference arrays. TurnResolver ignored the
act/marker agreement and started a new task. REMOVE/CLEAR are not options in the
followup_signals enum, so requiring a second signal was an avoidable dependency
on redundant model output. Deterministic regressions reproduce the wrong target.

The resolver now recognizes dependency only when a single REMOVE/CLEAR act agrees
with every current marker. It uses no missing execution slots or text regex.
Conflicting acts, bare hints, absent markers and mixed SET/query operations do not
gain inheritance. Explicit new-topic signals still win. With no current task the
result is unresolved, not an invented default task. The frozen contract receives
this minimal proven multi-turn P0 correction; V1 remains unchanged.

Before this, the semantic-edit model also received all restored task labels and
historical handles during ordinary continuation and new tasks. It repeatedly
copied a current handle into the historical selector, correctly triggering the
runtime guard. The context now offers only deterministic current-task state for
ordinary continuation, no old task state for complete new tasks, and scoped
historical candidates only for an explicit historical return. Actual recorded
payload type and clear barriers are included. Current-state labels omit the
historical handle. Runtime rejection remains and now rejects conflicting explicit
new-topic/history handles too. No model-selected identity, scope or plan is trusted.

No prompt text, regex, model/key default, public request/response/SSE, native
publication or cross-service source changed. The source change is confined to
recognition.py and pipeline.py. Private evaluation artifacts are now gitignored.

## Measured evidence, without changing denominators

| Full 20-transition run | Observable plans | Failed | Not run | Actual model requests |
| --- | ---: | ---: | ---: | ---: |
| PR #43 baseline | 1 | 15 | 4 | 48 (previous delivery) |
| Current task context | 2 | 14 | 4 | 48 |
| Context plus REMOVE/CLEAR dependency | 4 | 12 | 4 | 46 |

All **94** current-stage model requests returned HTTP200. Prompt v8, Gold,
catalog/hash, scope, model, thinking mode and current-turn schema stay the same.
All attempts and failed histories are retained. Live generations vary; the red/
green tests isolate the resolver defect instead of attributing every score change
to code. S81-012 fails at different history/current stages across the two runs.

The final observed cases are S81-001 metric ADD (all three metrics retained),
S81-003 metric REMOVE (only sales amount remains), S81-011 dimension ADD (hospital
and city), and S81-013 dimension REMOVE (only city remains). Six cases still stop
at missing frozen source-value observations. One Pending and three Dataset entry
fixtures remain NOT_RUN. None are removed from denominators or called model bugs.

These are selected state/operation/target observations, **not** whole-plan exact
match or complete hard-safety passes. Required unobserved safety stays
NOT_EVALUATED. Source SQL and production external writes are both 0. No parser
100-case rerun was needed because this stage changes task context/resolution,
not the current-turn parser component; the earlier parser measurements retain
their original timestamp and limitations.

## Tests and review

Final focused/contract tests: **409 passed**. New tests: **17**. Full Agent:
**2862 passed / 27 historical failures**; Oagnet **663 / 8**; SQL Translator
**381 / 0**. Old-pass -> new-fail 0; collection errors 0; removed tests 0;
Critical 160/160; clarification traces 89/89. No old expected outcome was relaxed.

Six new context regressions failed before the context fix. Four valid destructive
edit regressions demonstrated the resolver failure before its fix; one additional
empty-state test initially lacked required identity fields, which was corrected.
An existing adversarial test was adapted to obtain the same real prior handle
out of band because new tasks no longer receive it; its exact guard expectation
is unchanged. See test_delta.json. All initiated evaluation/test jobs are terminal.

Self-review checked actual current pointer selection, scoped restore before
context, recorded plan/version correspondence, topic-shift priority, unchanged
runtime guards, historical subtree regressions, clear barriers and absent-task
handling. Source and evidence are synced by explicit manifest and normalized
SHA-256; user-owned Oagnet HEAD/index, private .env and unrelated files are preserved.

Catalog Gap: shape-specific endpoint/grain ambiguities and production publication.
Evaluation Gap: singleton assignment representation, operation/shape conflicts,
frozen entity values, complete Pending/Dataset observations, Oracle/candidate and
whole-plan labels. Shadow Gap: full offline acceptance still missing. Redis and
native publication do not halt available offline work.

Next shortest path: diagnose the exact remaining assignment/operation cases using
captured evidence, and extend existing evaluator/controlled value observations.
Then complete controlled model comparisons and plan-only shadow once their gates
pass. No production replacement approval is requested at this stage.

Current evidence: task_context/comparison.json, first_divergence_report.json,
semantic_architecture_audit.md and cutover_readiness_matrix.json. Previous evidence
and the completed identity/Redis audits remain in their referenced original files.
