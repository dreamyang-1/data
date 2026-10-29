# Composite execution and filter ownership deployment

## Release

- Runtime commits: `d1255a5` (composite child execution/display) and `613f25b`
  (filter ownership and grouping separation).
- Release identifier: `recent-613f25b-20260924-150918`.
- Deployed only five reviewed Python files: `app/api.py`,
  `app/services/orchestrator.py`, `Oagnet/agent.py`, `Oagnet/prompt_build.py`,
  `sql-translator/sql_translator_prod.py`.
- All previous files backed up before replacement. Before/after SHA-256 and
  service states recorded in the server-side release manifest. Post-deploy
  checks matched all expected hashes. Environment/configuration hashes unchanged.
- No database changes, semantic publication, index rebuild, or V2 cutover.

## Remote-only changes preserved

The remote API includes a colleague's early release of planning RUNNING events.
A reviewed three-way merge against the deployed base `e1bdd6e` preserved those
changes while applying the composite progress patch. Other files matched known
committed versions before deployment. No directory-wide overwrite was used.

API hash before: `0d2c9b5d6382b5d06e4ec737b890470fb69cc5e754fb2135a2aa99949a1032f2`.
Merged API hash: `ae976f49947eb97bf23913f92c437c58974f824a5667df55d38525605bc8382d`.
The merged remote API intentionally differs from canonical Git; do not replace
it wholesale during a future deployment without reconciling this remote patch.

## Restart and checks

All times local server time on 2026-09-24.

| Service | Port | Restart | New PID | Health |
| --- | --- | --- | --- | --- |
| SQL Translator | 48000 | 15:09:41 | 3449933 | HTTP 200, ok |
| Oagnet | 18022 | 15:09:43 | 3450035 | HTTP 200, UP |
| DataAnalysis | 8808 | 15:09:47 | 3450433 | HTTP 200, READY |

All services active, PIDs changed, restart counters zero; vector health passed.
Remote dependency-mocked regression tests before restart:

- SQL: 56 passed, including filter-role routing and translator hardening.
- Oagnet: 83 passed, including surface roles and intent/ASL contract.
- DataAnalysis: 121 passed, including composite children, API/SSE ordering and
  semantic choice contract; one existing framework deprecation warning.

## Live SSE verification

Ran the user's two-part dealer cooperation / regional hospital count question
with a fresh deployment-test conversation in the selected model/domain.
Two executions completed in 84.1 seconds and 85.0 seconds respectively; both
returned task-1 and task-2 as COMPLETED with no task error codes.

The first smoke script incorrectly searched only SQL/data-retrieval text for
task labels, omitting the semantic-planning event that carries them. Fixed the
test harness to collect all execution-section events in stream order (no
runtime code change). The second execution passed all six checks:

- Exactly two task results, both completed.
- No global clarification-execution/result stage.
- Frozen main-node order through final output.
- Both execution task labels present, task 1 before task 2.

No business rows or credentials are included in this report. This verifies
execution and presentation, not the geographic ownership of the result.

## Remaining scope boundary

Successful deployment does not prove that a shared city dictionary field
encodes the intended hospital/dealer role. Semantic-model changes remain owned
by the semantic team. No geographic business result is certified by the health
checks or mock suites. V1 replacement readiness is unchanged.

## Default deployment instruction

The user's standing instruction is now recorded in AGENTS.md: after future
tested code changes, deploy to the agreed server and restart affected services
without waiting for another deployment prompt. Keep drift checks, backups,
configuration protection and health checks. Stop on conflicts, failed tests or
insufficient authority. Documentation-only changes do not require a restart.
