# Phase 0A Repository Reproducibility Closure

Phase 0A reconciles repository contents only. No development Python behavior, prompts, intent, admission, pending, slot merging, grounding, ASL or dataset logic was edited. Four existing tested source versions are now tracked together with their already tracked dependencies.

Starting commit: `6c1c6e5242a4b42b302e141f66b5886632aa21ec`.
Branch: `phase0a-repro-closure-20260907-01`.
PHASE0A_BASELINE_COMMIT: the commit introducing this report, resolved by `git log -1 --format=%H -- docs/phase0a/phase0a_closure_report.md`. Its concrete identity is also recorded in the following Phase 0B baseline manifest. A file cannot embed its own containing commit hash without changing that hash.

## Seven historical differences

| Path | Type | Authority and disposition |
|---|---|---|
| app/adapters/semantic_query.py | Source | Frozen tested workspace; synchronize exact bytes |
| app/analysis/synthesis.py | Source | Frozen tested workspace; synchronize exact bytes |
| app/domain/models.py | Source | Frozen tested workspace; synchronize exact bytes |
| app/stores/session.py | Source | Frozen tested workspace; synchronize exact bytes |
| AGENTS.md | Formal instructions | Git document restored to workspace; parent instructions still apply |
| CODE_REVIEW_ISSUES.md | Formal document with credential-bearing workspace drift | Git already-redacted document adopted; no credential copied into Git |
| ??????.md | Runtime collection | Remove tracking only; preserve local file, ignore future collection |

There are four source differences, one runtime difference and two formal documents. There is no local configuration file among the seven. One document difference contains a secret quotation, which is removed by selecting the existing redacted authoritative document; no secret value is recorded in this report. Original authorship is unknown; hashes and Phase 2.5.1 records establish provenance before this phase.

## Complete inventory and policy

The starting tracked universe has 404 files: 397 MATCH, six WORKSPACE_ONLY_DIFFERENCE and one MISSING_IN_WORKSPACE. Every tracked file is listed in preexisting_drift_inventory.json/csv. drift_disposition.csv contains the seven individual decisions and evidence. The historical baseline is read, not reconstructed from memory.

Five requested Phase 2.5.1 manifest filenames are absent from the starting commit and workspace. phase25_1_evidence_index.json records every missing name and the fully read available equivalent. No retrospective Phase 2.5.1 evidence is fabricated or changed.

The tracked text blobs use LF; 120 development/checkout artifacts used CRLF. .gitattributes pins LF, and proven newline-only copies are normalized without changing semantic content. Hash checks compare both physical workspaces and raw commit blobs (archive with core.autocrlf=false). Historical artifacts remain historical evidence; embedded old physical hashes are not assertions about this later checkout.

Runtime logs, .env, caches, backups, temporary render/tool directories and uncurated local evaluation outputs remain local and ignored. Four local instruction/reference inputs are intentional local inputs, not formal application source. All project Python modules, tests, schemas, formal documentation and configuration templates are tracked. Existing committed evaluation fixtures remain reviewed generated evidence. No untracked application/test source exists outside the ignored temporary tooling/backup directories.

## Validation and limits

Offline full suite: 1696 passed, 32 failed, zero collection errors. All results match the Phase 2.5.1 final gate node-for-node. These failures are recorded only in Phase 0A; no behavior fix is attempted. test_environment.json records installed versions; no dependencies installed and no live model/service calls or production writes.

UNEXPECTED_TRACKED_SOURCE_DRIFT target/result: 0 after manifest synchronization. workspace_git_hash_verification.json records every final tracked path except the verification file itself; the verifier separately checks that file against the raw commit blob after commit to avoid a self-hash cycle. Git working tree cleanliness and raw committed bytes are checked before Phase 0B begins.

Remaining differences are EXPECTED_LOCAL_DIFFERENCE for ignored runtime/configuration/cache/reference files. Secret content is excluded. The runtime collector remains on disk and is no longer versioned. Source code is reproducible from one Phase 0A commit, including the four previously divergent files.

Phase 0A Gate: PASS only after post-commit verification of clean Git status and every committed file. The next phase must branch from that verified commit; no automatic merge. GitHub publication uses the stacked Phase 2.5.1 base. Draft PR availability is verified separately, with no credential values printed.

Rollback: use a separate checkout of the starting commit for inspection or revert the Phase 0A commit on a new branch after preserving local runtime files. Do not reset, clean, bulk overwrite or restore runtime data from Git. No production state migration occurred.
