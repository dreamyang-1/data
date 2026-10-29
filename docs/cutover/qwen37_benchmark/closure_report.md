# qwen3.7-max benchmark and collection edit closure

Baseline: `037839d7df9127fa946b1580363ddf84d32c50bb` / Draft PR #41.
Branch: `cutover-qwen37-benchmark-20260909t031600z`.
Final commit: the commit containing this report.

Current Stage: QWEN37_COMPONENT_BENCHMARK_AND_COLLECTION_EDIT_CLOSURE.
V1 Replacement Readiness: **NOT_READY**. Production-acceptance groups remain
**8 P0 / 4 P1**; their count is not a count of independently reproduced defects.
V1 routing, public API/SSE, the user-selected default model and scope rules are
unchanged. Native catalog publication and production writes were not performed.

## Complete current-turn component measurement

The unchanged 100-case Gold, frozen 81/[205] catalog, current-turn prompt and
generation schema were run against qwen3.7-max with thinking disabled, temperature
0, concurrency 2, timeout 30 seconds and no retries. All 100 requests returned
HTTP200. Typed current-turn parsing accepted 98; G81-072 and G81-083 each retained
one missing mention reference and were rejected. Their rejected reference tokens
were not captured, so no guessed correction is made.

| Axis | Previous qwen3.6-plus run | Current qwen3.7-max run |
| --- | --- | --- |
| Accepted parses | 94/100 | 98/100 |
| Selected mention recall | 73/89 | 80/89 |
| Selected role recall | 73/89 | 80/89 |
| Selected role purity | 67/89 | 74/89 |
| Operation | 11/13 | 9/13 |
| Query shape | 49/56 | 40/56 |
| Turn relation | 70/72 | 67/72 |

There are **29 cases with at least one failed labeled axis**, not 29 independent
business bugs. All expectations and denominators remain unchanged. Previous
qwen3.6-plus evidence includes four HTTP401 responses and used another credential;
single runs and partial-axis labels do not establish a quality winner. The
current default remains the model explicitly selected by the user.

Existing repairs were applied 44 times for exact spans and four times for temporal
mention references. Mean latency was 7.394 seconds; P95 was 11.234 seconds.
`parser_evaluation.json` and `parser_predictions.jsonl` preserve the complete run;
`parser_comparison.json` records shared hashes and limitations. The 100-call
measurement predates the collection fixes below, which affect the subsequent
semantic-edit stage only. No current-turn prompt/schema/parser behavior changed.

## Two verified collection representation problems

The first actual full-planner probe used the exact frozen source snapshot, actual
CatalogPublication/ScopedPlanSession/RawTurnPlanner code, and existing in-memory
test store/registry implementations. Dummy embeddings support the local catalog
publication contract; they do not establish semantic vector search quality.
Only the validated configured model transport could connect. Source-value reads
were explicitly denied. This was an isolated evaluation, not production shadow.

The initial history request was “含税销售总额和销售总数量”. Its two model-generated
ADD edits each carried one offered handle. The first divergence was
`RawTurnPlanner._patch`: it validated a list-typed slot before the TaskPatch
contract's existing single-item ADD/REMOVE semantics could apply. **PROVEN**:
the captured output and five red regressions reproduce the rejection.

The entry boundary now validates a singleton as one collection item only for
ADD/REMOVE on registry SET-cardinality slots. The operation and its value are
not rewritten; the existing reducer executes them. SET/REPLACE still require a
complete collection. Slot compatibility, types, roles, current evidence and
canonical binding are still checked. Metrics and dimensions are tested against
equivalent array edits and preservation of other state.

After that fix, the recorded request exposed a second divergence: the quantity
edit declared mention m-1 but copied the quantity handle offered for mention m-0.
The selected canonical metric and role were correct; its mention association was
not. **PROVEN** by the captured draft and reconstructed offered-handle mapping.
The existing evidence guard correctly rejected it; that guard remains intact.

A deterministic repair may now select another already offered handle only when:

- The slot is metrics or dimensions and the selected role matches the slot.
- One current mention exactly equals the selected candidate's display name.
- That mention is already declared by the edit and is not unresolved.
- Exactly one offered identity matches that name/role for that mention.
- The replacement handle has the same selected catalog identity and role.

It never chooses a different metric, dimension, operation, evidence ID or scope.
Aliases, normalized/fuzzy names, duplicate mentions, duplicate names across
identities, missing handles, role mismatches and extra authority fields are not
repaired. All subsequent strict validators still execute. Trace records contain
reason codes, field positions and mention IDs, not user utterances or catalog
text. No prompt rule, regex or frozen TaskPatch/CurrentTurnParser change was made.

Self-review checked this against the current user ADD/REMOVE contract, the
existing TaskPatch validator/reducer, scoped candidate creation and binding
provenance. The scoped integration replay preserves two initial metrics, adds a
third on the same task and stays SHADOW_ONLY. Negative tests assert specific
binding/role/evidence rejection reasons, rather than accepting arbitrary errors.

## Full-planner evidence remains incomplete

Seven real model calls were made across three recorded probe attempts: two before
the fixes, two after singleton validation, and three after both fixes. The exact
captured bootstrap output now reaches a correct two-metric plan in a zero-call
recorded replay. The final live attempt also completes that history request.

Its next utterance, “再加订单笔数”, failed in `v2_current_turn` with
`V2_MODEL_OUTPUT_INVALID`. The rejected raw response was not retained; its precise
schema defect is **UNKNOWN_NEEDS_CAPTURED_RESPONSE_SCHEMA_EVIDENCE**. No repair is
guessed and no live transition is counted as passed. A successful bootstrap is
not successful ADD acceptance. See all attempts in `full_plan_probe.json`.

The bootstrap replay is explicitly RECORDED_MODEL_PIPELINE. An initial temporary
replay inherited the live probe's tag; review corrected it and reran the replay
with no model calls. An earlier Windows event-loop initialization was denied
before any model call; initializing its internal socketpair before applying the
task network guard resolved that harness setup issue. Neither is hidden as a
successful business test.

The full snapshot remains private; its SHA-256, catalog version, scope and
127-fact public evaluation projection are verified before the probe. The committed
captured failure contains only curated utterance/model output and candidate
metadata already represented by that frozen catalog, with no credential, source
locator or business result rows. The new regressions replay the representation
and contrast semantics with independent synthetic catalogs; they are not model
accuracy labels. A reusable complete transition runner remains necessary.

## Verification and remaining gates

The five new positive singleton regressions failed before the code change; ten
negative controls already passed. The final focused group has 128 passing tests.
Twenty-nine regressions were added in total. Complete offline results:

| Service | Baseline passed / failed | Final passed / failed |
| --- | --- | --- |
| DataAnalysis | 2799 / 27 | 2828 / 27 |
| Oagnet | 663 / 8 | 663 / 8 |
| SQL Translator | 381 / 0 | 381 / 0 |

Final old-pass → new-fail: 0. Removed tests: 0. Collection errors: 0.
Critical Suite: 160/160. Public clarification reason traces: 89/89.
The initial SQL full run had one test-owned loopback connection abort (WinError
10053). The same node passed twice independently; the full suite then passed
381/381. Source and expectations were unchanged. The original observation and
unknown exact OS cause remain in `test_delta.json`; this is not concealed as an
uninterrupted green run.

Total real model requests for this delivery: **107** (100 parser + 7 planner).
Offline regression model calls: 0. Source SQL executions: 0. Production external
writes: 0. Cross-service patches: NO. Production V2 routing changes: NO.
All initiated model, replay and regression processes are terminal before release.

Catalog Gap: shape-specific relation/grain issues and native production publication
remain. Evaluation Gap: typed-output/reference failures, full state/canonical and
clarification labels, paired V1/V2 and outstanding model/Thinking comparisons.
Shadow Gap: evaluation acceptance is still missing; no real shadow/canary enabled.
Redis recovery remains a Canary/Cutover gate, not a global evaluation blocker.

Next shortest blocking path: capture bounded schema diagnostics for failed
current-turn outputs and complete the real scoped transition evaluator, retaining
history preconditions and failed/missing denominators. Then progress to isolated
plan-only shadow only after its evaluation gates pass. No acceptance standard is
relaxed; this report does not request or authorize production replacement.
