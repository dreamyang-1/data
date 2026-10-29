# User-selected qwen3.7-max configuration

Baseline: `2cd4d93de8ce151f0b859e8252db3f0ed7ce141c` / Draft PR #40.
Feature branch: `cutover-qwen37-config-20260909t031200z`.

The user explicitly requested the new API key from the pictured database record
and selected qwen3.7-max. This authorizes the model configuration change and
supersedes the earlier instruction to preserve the default model for benchmarks.
It does not authorize V2 takeover or change the public request/response/SSE format.

## Verified configuration

A read-only transaction selected exactly the named active record from
`info_sec_kb_v3.sys_api_model`, ID `2073009136548741121`. Its alias and API model ID
are both `qwen3.7-max`; type is llm, available is 1, deleted is 0, and the configured
HTTPS endpoint is the existing official DashScope compatibility endpoint.
The transaction was rolled back; no database record was modified.

The credential is different from the previously effective shared API_KEY.
It is stored only in the untracked DataAnalysis `.env`, under the existing
`DATA_AGENT_INTENT_MODEL_API_KEY` setting. The service-specific override avoids
modifying the shared platform or New_Agent credentials. No secret, partial secret,
credential hash, environment file or private raw record is included in Git.
All unrelated dotenv values were compared before/after and preserved.

Intent, analysis synthesis and chat all use the existing shared endpoint/key.
Their local settings and tracked defaults/templates now select qwen3.7-max.
Thinking, response format, timeouts and retries are unchanged. A fresh Settings
instance confirmed all three effective model names and the exact database key
without outputting it. No matching running DataAnalysis/uvicorn process was found
in the local process scan; no process or deployed service was restarted. This
configuration is verified for new local processes, not remote deployment state.

## Validation and limits

One real call through the existing V2 current-turn client used frozen scope
81/[205] case G81-096. HTTP200 returned model qwen3.7-max; typed parsing and all
three labeled mention/role checks passed. Tokens: 2,116 input + 378 output.
No SQL, Redis or production state was touched. See `smoke.json`.
This is connectivity and one-case parsing evidence, not full model accuracy,
V1/V2 comparison, or production replacement acceptance. The benchmark runner
itself did not mutate settings; configuration changed separately beforehand.

The complete existing offline suites retain every baseline node outcome:
DataAnalysis 2,799 passed / 27 failed; Oagnet 663 passed / 8 failed;
SQL Translator 381 passed / 0 failed. Collection errors: 0. New regressions: 0.
Critical regressions: 160/160. Clarification trace coverage: 89/89.
No new expectation or test was needed for three literal default changes.
Full test delta and receipt hashes are recorded in `test_delta.json`.

Self-review verified the precise DB record and model_id (not merely its alias),
active/deleted flags, credential source precedence, endpoint provenance, scoped
smoke request, existing transport contracts and preservation of unrelated config.
The pre-existing offline full-entrypoint audit remains temporary observation work;
no evaluator/runtime edits from that work are mixed into this configuration commit.
All configuration/smoke/regression scripts completed before release.

Current Stage: multi-turn state Gold / component evaluator; model access restored.
Cutover Blocker P0/P1: 8 / 4, unchanged. Catalog Gap: shape-specific relation/grain
issues and native production publication remain. Evaluation Gap: complete labeled
V1/V2 comparisons and model-quality evidence remain. Shadow Gap: evaluation
acceptance is pending; no live shadow or Canary was enabled.
V1 Replacement Readiness: NOT_READY. Next shortest blocking path: benchmark the
user-selected model on the frozen corpus, then complete full state/canonical
evaluation before isolated plan-only shadow. No gate was relaxed.

Rollback: revert the tracked configuration commit after review. The local secret
is deliberately outside Git, so a Git revert cannot restore or rotate credentials;
change the existing local configuration explicitly if needed. The previous key
was not backed up by this task, and shared environment files were not modified.
