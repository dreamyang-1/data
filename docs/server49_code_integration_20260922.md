# Bidirectional source integration, 2026-09-22

## Scope and provenance

User requested publishing the local unshipped changes to server 49 and then
bringing the combined source back into the development workspace. This is a
source synchronization, not a new production routing or configuration change.

- Preserved canonical commit `63ff13b` under
  `backup/before-49-integration-20260922`.
- Compared the deployed source, development source, and the previously deployed
  `e1d0a1c` baseline; staged the combined version in an isolated worktree.
- Preserved the server's planner, recognition, pending-context and catalog-cache
  changes. Copied its MCP runner and tests without claiming this unfinished
  module is wired into the production orchestrator.
- Preserved local advisory ASL matching/notices, structured reference handoff,
  phrase-level recall, source-verified values, and SQL format compatibility.
- Retained the newer tests for final-only charts, explanatory reliability labels,
  and the previously accepted SQL compatibility behavior instead of replacing
  them with older server test expectations.
- A second preflight detected seven concurrently changed Python files and
  aborted before any production overwrite. These changes were downloaded and
  merged against the previous server snapshot. The only textual conflict was
  the surface-ASL client's keyword arguments: both `structured_reference` and
  `structured_extraction` are retained. A regression checks their simultaneous
  forwarding and the copied planner payload's mutation isolation. This does not
  claim Oagnet consumes the newly introduced raw planner payload; its existing
  structured-reference input remains the implemented advisory input.

## Explicit release and safety

- Remote source release: `codex-integration-20260922-192728`.
- Uploaded 30 Python source/test files; verified all 436 Python files in the
  explicit cross-service manifest after writing. No recursive tree overwrite.
- Updated 30 development files and verified the same 436-file manifest locally.
- Remote pre-images are in the release-specific directory under
  `/root/.codex-deploy-backups/`; local pre-images and both remote snapshots are
  retained in the workspace's `.tmp` synchronization directories.
- Did not copy environment files, credentials, database rows, runtime logs,
  caches, backups or the runtime-generated business-question document into Git.
- Did not execute database writes, vector rebuilds, process kills, service
  restarts, environment changes or a V2 routing switch.
- Startup ownership was changing independently during synchronization. Initial
  inspection found a developer-launched process on the data-agent port and a
  duplicate managed service with different runtime settings. A later inspection
  found an externally initiated service stop in progress. Source hash equality
  is not proof that every running process loaded the new source.

## Verification

- Oagnet: **777 passed**, using an offline harness with mocked Milvus and MySQL
  clients and dummy configuration, without production database/index access.
- SQL Translator: **455 passed**.
- Critical/node-order/ASL-notice/API/insight/reliability slice: **119 passed**.
- After the second merge, expanded integration slice: **339 passed, 1 failed**.
  The failure already exists in the server baseline: the all-time pending test
  expects a brand/role display in the intent text; the server's current display
  emits only the original and completed questions there. No assertion was
  weakened to hide it.
- Initial server snapshot full suite: **4102 passed, 107 failed**; first combined
  suite: **4141 passed, 79 failed**, no new failed identities or collection errors.
  These counts are not the earlier canonical 52-failure baseline: the server has
  additional unfinished tests and the snapshot excludes private fixture content.
  Restored fixture/test versions also explain part of the apparent improvement;
  it must not be presented as 28 production bug fixes.
- Final post-refresh full suite: **4145 passed, 79 failed**, no collection
  errors. The failed identities are unchanged from the first combined suite;
  the final merge adds no regression relative to the captured server baseline.
- Read-only health checks observed Oagnet and SQL returning HTTP 200. After an
  independently initiated restart, the data-agent port was listening again under
  its managed service. No restart was issued by this synchronization task.

Known failures are retained rather than changing teammates' unfinished modules
or relaxing unrelated tests. The source integration does not certify production
readiness or completion of V2 replacement.
