# Live, lower-priority semantic general rules

## Cause and requested contract

PROVEN: platform general rules are stored in `semantic_model_general_rule`,
with `semantic_model_id`, `business_domain_ids`, `start_status` and `description`.
The existing Agent prompt store only reads configured agent-version text; it did
not load these rules. Planner remote-description precedence could also discard
an appended rules block unless that block was carried separately.

The requested correction is runtime system-prompt composition, not a database
rewrite of the agent's role settings and not a hardcoded copy of a screenshot.
Enabled, nondeleted rules are freshly selected for the current model and exact
authorized business-domain membership. An empty requested domain scope remains
model-wide; explicit multi-domain requests are not expanded. No implicit shared
domain is added. Missing or malformed rule membership is not treated as global.

Existing system rules, agent instructions, catalog bindings and explicit user
constraints take precedence. General-rule text only supplies nonconflicting
supplements. This precedence is expressed in the model system prompt; it is not
a new compiler for arbitrary natural-language business rules. Deterministic
authorization, ASL, SQL, output schema and stage contracts remain authoritative.
For example, a configured default period cannot override an existing prohibition
against adding an unspecified period.

## Implementation and update semantics

- Reuse the configured read-only platform connection in AgentPromptStore.
- The rules lookup has no TTL cache: additions, edits, deletion and disabling
  affect the next newly processed external request without a restart/redeploy.
- Preserve one private immutable rules snapshot per turn and scope. Re-check if
  a request scope changes. Never expose rule text as a new transport field or
  persistently change a user's configured prompt. Message replay remains under
  the existing idempotency contract, not a reason to rerun old requests.
- Append the lower-priority block after the existing concise/role/background
  prompt, including when the backend already supplies that prompt.
- V1 and the context bridge attach rules before recognition. Existing prompt
  consumers carry them to recognition, classification and result synthesis.
  Single/compound planning extracts the block separately, so a remote semantic
  description or bounded catalog-text truncation cannot erase it or turn it
  into a new metric/field catalog.
- A rule-store outage omits the optional supplement, retains existing behavior,
  does not reuse stale rules and logs only the failure class, not credentials.

No Java/UI change, rule mutation, semantic backfill, index rebuild, permission
change, API/SSE field change or production runtime-mode cutover was performed.
The preceding shipping-date handoff diagnosis is a separate issue, not silently
included in this change.

## Verification

- 32 new offline cases pass, covering add/edit/delete/enable state, exact
  membership, invalid scopes, missing metadata, provided prompts, concise
  priority, a frozen turn, fresh next turn, narrowed scope, outage behavior,
  V1/bridge attachment and actual mocked system payloads for single/compound
  planning with a remote description.
- Focused critical suite: 60 passed, including existing prompt, critical business,
  parsing-stage and isolated telemetry checks. One 30 ms telemetry assertion
  flaked in a concurrent broad run; the unchanged isolated test passes.
- Full canonical offline baseline: 4745 passed/124 failed/6 existing collection
  errors. Final: 4777 passed/the same 124 failures/the same 6 collection errors.
  Existing-test outcome changes, old-pass/new-fail and old-fail/new-pass are zero.
  No existing test assertion was changed. These suites are not all-green.
- Server candidate scoped comparison: baseline 164 passed/20 existing failures;
  candidate 196 passed/the exact same 20 failures. All new 32 cases pass. Runtime
  source drift is preserved by bounded merge hunks, not whole-file replacement.
- Candidate live registry/prompt checks pass for explicit and model-wide forms
  of the established model and the affected receivables model. Rules are selected
  dynamically; a model with no rules remains without a rules section. Only rule
  counts/IDs and assertions are reported; no production rule text is committed.
- Earlier candidate-runner attempts imported original package modules before
  installing candidate paths or lacked a relative planner template. Neither
  replaced live source. The verified runner installs an import finder before
  loading packages and uses the unchanged server template.
- Released to the authorized server after repeating the baseline/candidate
  comparison and real scoped rule injection checks. Only Agent was restarted;
  readiness is READY and protected configuration/source hashes are unchanged.
- Post-release native established-model hospital sales quantity and receivables
  shipping amount questions both completed with result tables, no transport/query
  error, and the unchanged seven public stages. The configured runtime prompt
  store is active. These successful queries verify the integration does not
  break the existing execution route; they do not claim exhaustive semantic
  conflict resolution for every possible arbitrary rule description.

## Explicit change manifest

`app/services/general_rules.py`, `app/services/agent_prompt_store.py`,
`app/domain/models.py`, `app/services/orchestrator.py`,
`app/semantic_v2/context_v1_execution.py`, `app/planning/task_dag.py`,
`app/planning/compound_question.py`, `tests/test_general_rules.py`, this report.

Only these files are synchronized to the canonical repository with per-file
SHA-256 verification. Environment/configuration, service units, unrelated
adapters/API and other service source are protected by before/after hashes.
Recoverable private deployment backups are retained outside Git. A concurrent
target/config change or merge conflict stops publication.

Current stage: bounded V1/current-context rule integration. Global V2 catalog,
evaluation, shadow and replacement-readiness blockers are unchanged. No V2
replacement approval is claimed.
