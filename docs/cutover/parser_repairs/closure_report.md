# Current-turn representation repair and evaluation evidence

Baseline: `f15969dcc0f90e75a399a3d80d4a489d58139dec` / Draft PR #38.
Branch: `cutover-parser-spans-20260909t015840z`.
Final source/evidence commit: the commit containing this report.

Current Stage: **CURRENT_TURN_REPRESENTATION_REPAIR_AND_SEMANTIC_EVALUATION**.
The bounded repair is verified; overall evaluation and replacement acceptance
remain **NOT_READY**, with **8 P0 / 4 P1** production-acceptance groups open.
V1 remains the production route. No production replacement or native publication
was performed. Public request, response and SSE formats are unchanged.

## Verified change

The first divergence was the current-turn parser's representation validation.
The initial 100-case study received 100 HTTP200 responses, but rejected 35 parses.
Ten captured failures demonstrate exact span offsets and literal words placed in
mention-ID reference arrays. The frozen strict parser correctly rejected them.

The opt-in V2 planner now repairs a representation only when the same current-turn
evidence selects one unambiguous existing mention. It preserves surfaces, roles,
operation choices, current scope and state. Absent/repeated/foreign-turn mentions,
unknown operations and unknown slots still fail strict validation. See
[source review](source_review.md) and [ten recorded replays](recorded_replay.json).

The generation schema also limits slot names to the existing registry. It never
guesses whether an invalid `limit` slot means ranking or display. The frozen parse
models and validator are unchanged. Instruction text, prompt version v7, model
defaults and temperature remain unchanged. The schema embedded in model messages
does change, and its separate hash identifies that change:

- Previous generation schema: `33ff426f52da71efa99cf07a3aca1461ff5189ec4f3e7fa79974cb9f4924c2eb`.
- Final generation schema: `fe6dcfb346f5f2e260a216e3849a1556ded61a3d93a44835bf4fe984bc1985ff`.
- Instruction-text hash: `ff29f7c1ddffb368a48999dce8536231216f9f7bd1805f02b56c513668c8d1de`.

There are no new regexes or business-keyword prompt rules. Repair traces contain
reason codes, mention references and coordinates rather than full utterances.

## Fixed evaluation inputs and measured results

All studies use qwen3.6-plus, thinking disabled, temperature 0, retries 0 and the
existing 30-second deadline. No production default was changed. Scope is model
81 / explicit domain [205].

- Catalog version: `3f9589b95d376ebdf9683425dcf6c646f2394b278718b033527bd5e08b64fe21`.
- Frozen projection hash: `b72b9d421365935f9b5bc2a3a2ec07c2f277bae7e0928c00ea64ae04ba0eb05e`.
- Unchanged Gold hash: `b2db7b903c691d2f696edeba3dbb90e4e1af7da80566d2a0635e1a7c9fd381b6`.

| Observation | Baseline PR #38 | Intermediate repair | Final repair + generation schema |
| --- | ---: | ---: | ---: |
| Requests | 100 | 100 | 100 |
| HTTP200 | 100 | 100 | 96 |
| HTTP401 | 0 | 0 | 4 |
| Strict parse accepted | 65 | 93 | 94 |
| Parse validation failures | 35 | 7 | 2 |
| Critical mention recall | 46/89 | 76/89 | 73/89 |
| Critical role recall | 37/89 | 67/89 | 73/89 |
| Critical role purity | 37/89 | 66/89 | 67/89 |
| Operation | 9/13 | 11/13 | 11/13 |
| Query shape | 22/56 | 39/56 | 49/56 |
| Turn relation | 50/72 | 66/72 | 70/72 |

Missing/failed predictions remain failures in every labeled denominator. The
final run records 35 span, three temporal-reference and two negation-reference
repairs. Live generations vary and the final schema also changed; the live metric
differences alone do not prove a single cause. The ten captured outputs establish
the deterministic repair effect without model calls.

Final G81-061 and G81-082 each retain an unknown mention reference. Their exact
rejected reference was not captured by the bounded benchmark diagnostics, so no
new semantic alias or guessed fix was added. G81-096/097/098/100 had transport
failures; response receipts show four HTTP401 `invalid_api_key` responses. A
separate four-request recheck at 2026-09-09T02:20:38Z also returned four HTTP401s.
The original 100-case report is retained, with no replacement of failed rows by
later attempts. This proves a current provider-access failure, not why the key
became invalid. Credentials and provider error bodies are not in these reports.

[Benchmark summary](benchmark_summary.json) includes separate original,
intermediate, final and recheck references. The current branch made **217 real
model requests**: 6 capture + 100 intermediate + 7 capture + 100 final + 4 recheck.
All used curated evaluation utterances. Source SQL executions and production
state writes in this parser phase were **0**.

There are **21 cases with at least one failed labeled axis or failed prediction**,
listed in [remaining evaluation failures](remaining_evaluation_failures.json).
They are case observations, not 21 deduplicated production bugs. Partial role/shape
labels, unmeasured precision, full state changes, canonical binding, SQL/results
and held-out quality remain acceptance gaps. A valid JSON parse is not a correct
business answer. This is a current-turn component benchmark, not a full V1/V2 or
model-selection acceptance report.

## Regression and review

| Suite | Baseline passed / failed | Final passed / failed | New tests |
| --- | ---: | ---: | ---: |
| DataAnalysis Agent | 2735 / 27 | 2769 / 27 | 34 |
| Oagnet | 663 / 8 | 663 / 8 | 0 |
| SQL Translator | 381 / 0 | 381 / 0 | 0 |

All existing node outcomes are unchanged: **old pass to new fail 0**, old fail to
new pass 0, removed tests 0, collection errors 0. Critical acceptance is **160/160**;
public clarification reason coverage remains **89/89**. Tests run offline with
network guards and the established controlled classifier clock. No old assertion
or Gold label was changed to reduce failure counts.

The 34 new tests include ten actual failure replays, Unicode/overlap contrasts,
non-repair cases, immutable input checks, schema closure and a two-turn scoped
planner integration proving that repaired spans preserve ADD. The full suite was
run after the final implementation and test changes. Self-review traced repair
through strict validation and the actual planner/benchmark invocation. Full
receipt hashes, exact new nodeids and baseline outcome references are in
[test delta](test_delta.json).

## Remaining gates and next shortest path

The user's stage-specific gate decision is unchanged. Fourteen provisional
identities and physical/schema evidence are in the
[identity matrix](../evaluation_gates/identity_matrix.csv). They are not fourteen
missing business decisions. Exact order grain, two relationship declarations and
two metric subjects affect their own shapes. Redis production recovery and
catalog publication remain Canary/Cutover requirements, with no global offline
evaluation block; see the [gate matrix](../evaluation_gates/gate_matrix.json).

Next, continue offline full-state/canonical labels and evaluator work, examine
the measured semantic mismatches, and capture the two unknown references once
existing provider access is restored. Do not change models, keys or labels just
to obtain a passing score. Then complete bounded full V1/V2/model comparison,
accepted isolated plan-only shadow, and Canary/rollback readiness. No live shadow
has started and no canary has run. Production recovery/publication/trust evidence
is still required before those production steps.

**READY_FOR_USER_APPROVAL has not been reached.** Formal replacement still
requires all gates and the user's explicit approval.
