# Data-grounded insight without a content rejection gate

Current stage: bounded V1 insight presentation change; no V2 cutover.

## Root cause and decision

PROVEN: synthesis required algorithm lock markers, numeric allowlists, lexical
overlap, per-claim evidence kinds, selected interpretations and mandatory
limitation claims. Some failures triggered another reviewing model and a content
retry; exhausted failures discarded all paragraphs. The orchestrator then hid
even its available deterministic summary behind a generic rejection message.

User explicitly requests removal of this insight content gate, while keeping
analysis based on the current question and actual query data. Those former
rejection contracts are superseded only for insight narrative generation.

## Changes and limits

- Remove the content validator, evidence-review model call and content retries.
  The prompt now allows data-grounded comparison, arithmetic and qualified
  inference instead of restricting the model to an exact wording/number list.
- Preserve the synthesis callable and claims response shape, but accept missing
  citation/type metadata, paragraph JSON or prose. Unknown citation IDs are
  omitted, not replaced with invented evidence. Empty/malformed responses and
  transport failures still use the available deterministic summary.
- Supply the completed question, existing statistics and up to 20 actual rows,
  with explicit sample/full-result counts. This preview is a model-input copy,
  not added to persisted analysis evidence. Existing bounded context callers
  remain compatible. No raw business data is included in this report.
- Keep data-coverage warnings visible even when model wording omits them. Keep
  chart rendering solely in final output. Do not label model prose as verified
  conclusions: its generation record explicitly states no independent content
  review was performed and it is not new factual evidence.
- Permission, Semantic Scope, SQL safety, result reliability, metric definitions,
  final table/chart/export and frozen six-stage order are unchanged.
- Tradeoff: prose accuracy now depends on the model following the data-grounding
  instructions, not a second content checker. Plausible model mistakes are still
  possible; this change does not promise factual verification of every paragraph.

## Exact manifest

Runtime: `app/analysis/synthesis.py`, `app/services/orchestrator.py`,
`app/presentation/reliability.py`.
Tests: `tests/test_analysis_synthesis.py`, `tests/test_analysis_orchestration.py`,
`tests/test_api.py`.

## Verification

- Baseline scoped synthesis/orchestration/reliability: 52 passed.
- New scoped suite: 55 passed.
- Critical synthesis/orchestration/API/composite/semantic-choice/preview/context
  suite: 187 passed, including frozen public stage-order assertions.
- STALE_TEST: replace old content-rejection expectations with one-call analysis,
  grounded derived arithmetic, missing metadata/prose compatibility and honest
  failure fallback tests. Update the old verified-content footer assertion.
- Existing bounded-context forwarding was restored after regression detection;
  its original test was not relaxed.
- Full baseline: 4321 passed / 11 pre-existing MCP failures. Final: 4324 passed /
  the same 11 failing IDs; no new failures or collection errors. Removal of
  obsolete content-gate test cases is intentional under the user decision above,
  not reported as repaired test cases.

The separate catalog/evaluation/shadow gaps and V1 replacement readiness are
unchanged. No semantic publication, production configuration or V2 routing edits.

## Deployment and live check

- Runtime commit `fb7da7b`: exact three-file deployment after known-version
  hash checks and backup; protected configuration hashes unchanged.
- Remote scoped tests: 128 passed. DataAnalysis restarted at
  2026-09-24 17:59:27 CST; process change and readiness HTTP 200 confirmed.
  Oagnet and SQL processes were not restarted or changed.
- A fresh-conversation hospital-total query completed in 60.9 seconds and
  produced six model paragraphs. Insight-stage text contained 1031 characters,
  without the hidden-analysis fallback or chart output. The generation record
  explicitly reported no independent content validation. All six public stages
  appeared in order; final query result remained available.
- This confirms pipeline behavior and report availability, not independent
  verification of every generated sentence. No raw business rows are committed.
