# V2 replacement readiness — current delivery

Current Stage: RAW_TRANSITION_EVIDENCE_AND_SCHEMA_GUARD.
Baseline: `b42fdaf913e6e54d7de9bdb8a343183536703586` / Draft PR #42. Branch: `cutover-transition-runner-20260909t034400z`.
Readiness: **NOT_READY**. No quality gate or production replacement is approved.
The inherited production acceptance inventory remains 8 P0 / 4 P1 groups;
the current gate matrix keeps Redis/native publication separate from offline work.

The interrupted work was preserved and both existing model jobs were recovered
from terminal receipts, without duplicate evaluation calls. Git was clean at
the baseline; the only prior tracked development differences were recognition.py
and its tests, plus the new runner and runner tests. PR #42 is open, draft and
unmerged. All mapped tracked sources were compared before the new work.

## Root cause and changes

The reusable opt-in runner exercises real RawTurnPlanner, catalog publication,
scope pin, reducer and compiler code with the existing in-memory catalog store
and FakeRedis. It verifies the exact frozen 81/[205] snapshot projection/hash.
Dummy embeddings do not measure native vector recall. It performs no source SQL,
native catalog publish, production state write or user-visible response change.
Only the validated configured model transport can connect; budgets, access failures
and missing histories cannot silently remove cases from denominators. Diagnostics
record bounded schema field paths/codes. Raw failed model responses remain private.

The generation schema now offers historical task handles only with an explicit
HISTORICAL signal; otherwise that field is null-only. The original runtime guard
is unchanged. Live results demonstrate that the model still sometimes ignores
the schema and is correctly rejected. This change is a generation constraint,
not a completed target-task fix. Five positive/negative regressions verify scope
offers, immutable base schema and retained runtime rejection.

An independent v8 current-turn prompt experiment clarifies the existing mention
and reference representation contract. Captured failures had an operation cue
with no semantic role, or a literal negation cue in a mention-ID array. Assigning
an invented role, discarding arbitrary mentions or guessing a target would be
unsafe deterministic repairs. The four general contract lines do not add business
keywords, regexes, new roles, defaults or weaker validation. The DRAFT instruction
text is unchanged; its dynamic schema hash is recorded for every call.

Before/after S81-001, S81-003 and S81-013 parse representations improved, but later
task-handle failures remain. Contrast coverage includes the unchanged 100-case
corpus and 145 focused tests. This is not a claim that v8 is a better final model
policy: selected role purity decreased, and semantic regressions are recorded.
Prompt changes are committed independently from the runner/schema changes.

## Evaluation

The original 35 failures were 30 ValueError and five ValidationError observations
at current-turn representation validation, with no original raw responses. The
previously completed ten captured replays prove span/reference problems for those
captures; they do not retroactively prove the root cause of all 35. No repeat
Identity/Redis audit, Gold recreation or duplicate evaluator was performed.

| Measurement | Before v7 | Current v8 |
| --- | ---: | ---: |
| Accepted parser structures | 98/100 | 100/100 |
| Selected mention/role recall | 80/89 | 79/89 |
| Selected role purity | 74/89 | 70/89 |
| Operation | 9/13 | 11/13 |
| Query shape | 40/56 | 41/56 |
| Turn relation | 67/72 | 67/72 |

These are unchanged selected-axis denominators, not full mention F1 or whole-plan
accuracy. Current observed first-disagreement distribution is in
`first_divergence_report.json`; joint-output inspection order is not model causal
reasoning. There are 3 old-pass/new-fail labeled case-axis observations
and 5 improvements. A single run per prompt does not prove causation.

The initial raw transition run made 45 HTTP200 model requests: 16 FAILED, four
NOT_RUN. The final run made 48 HTTP200 requests: 15 FAILED, one observed plan,
four NOT_RUN. Both keep the full 20-case denominator. Six cases in each run stop
because frozen source-value observations are absent. The Pending fixture and
three Dataset entry fixtures are not implemented by this runner. Those are
evaluation coverage gaps, not 10 model bugs. S81-011 observes dimension ADD and
retained metrics; safety and complete whole-plan labels remain unmeasured.
Failed history never becomes fabricated successful state. Region/time observer
axes remain incomplete, and required safety observations stay NOT_EVALUATED.

Total requests this stage: **193**, all HTTP200 (100 parser + 45 + 48 planner).
Real source SQL: 0. Production writes: 0. Native publication: 0.
The initial raw receipt called its storage mode `scope_mode`; its nested authorized
scope was correctly EXPLICIT_DOMAINS. The runner now calls that metadata field
`catalog_storage_mode`; the initial receipt is preserved with this correction note.

## Validation and review

Focused: 145 passed. New regressions: 17. Agent: **2845 passed / 27 historical
failures**; Oagnet: **663 / 8**; SQL Translator: **381 / 0**. All previous test-node
outcomes unchanged; removed tests 0; collection errors 0; Critical 160/160;
clarification traces 89/89. The previous stage's SQL loopback fluctuation remains
in its historical evidence; this run has no such failure. All scripts are terminal.

Self-review checked exact scope/hash verification, no Gold label input, fail-closed
history, bounded model budgets, network teardown, source-read denial, private
diagnostics, fixed denominators and strict runtime target validation. The runner
tests exercise the real scoped planner under scripted HTTP, including poison-label,
unknown snapshot/scope, authentication, missing fixture and network-denial contrasts.
No production I/O, routing, user-selected model/key or cross-service source changed.

Catalog Gap: two relation endpoints and shape-specific metric grain facts remain;
production publication is separate. Evaluation Gap: real semantic errors, frozen
entity values, full state/safety/whole-plan labels, candidate/oracle and controlled
model/Thinking comparisons. Shadow Gap: offline acceptance is not achieved.
Next shortest path: close evidenced task-reference and operation/shape failures,
extend the existing observation and candidate/Oracle coverage, then compare models
under identical inputs. No cutover approval is requested while these gates fail.

New current evidence is concentrated under `docs/v2_cutover/`. Historical catalog,
identity, Redis, Gold and evaluation files stay at their original referenced paths.
Secrets, private snapshots, raw business records, logs and local .env are excluded.

The independent runner/schema commit is `499d0550910759833fa57e1f3db766f18f0e1ae6`. Its v7-focused verification also passed 145 tests. One initial verification launch omitted PYTEST_DISABLE_PLUGIN_AUTOLOAD and stopped at duplicate plugin registration before collection; rerunning with the established offline environment passed. No source or expectation change was needed.
