# Oagnet source snapshot

This import publishes the current `E:\YouoAgent\Oagnet` working tree under
[`Oagnet/`](../../Oagnet) in `dreamyang-1/data`. It contains all 49 selected source,
test, script, dependency and formal documentation files, including locally staged,
unstaged and previously untracked code. The source repository HEAD alone does not
represent this working version.

The import branch is `oagnet-source-import-20260908-01`, based on
`e406029c83692d6e785b0880815d2a64773157c7`. The original Oagnet repository stays on
`main` at `656637304eb1c7dbdf8ddb8714ea3b8df6b0990c`; its index and source files are
preserved. Publication uses an independent branch and does not merge into `main`.

[`source_manifest.json`](source_manifest.json) records every published path,
original and published SHA-256, the original staged and unstaged diff fingerprints,
and the source Git baseline. Seven files have CRLF converted to LF to follow the
destination repository's `.gitattributes`. There are no application behavior edits
in this import.

Excluded files are Git metadata, environment files and credentials, logs, caches,
backups, virtual environments and local vector database contents. Inventory used
`rg --files --hidden --no-ignore` with explicit exclusions so that source files
ignored by the original Git configuration were also considered. Credential token,
private key, authenticated URL and Python credential literal scans found no matches
in the selected files.

## Configuration and operation

Oagnet remains a separate Python service with its own
[`requirements.txt`](../../Oagnet/requirements.txt),
[`config.py`](../../Oagnet/config.py) and
[`start_lan.ps1`](../../Oagnet/scripts/start_lan.ps1). Its configuration reads process
environment variables and an optional `.env` in the parent of the Oagnet directory
(the repository root in this layout). No environment values or credentials are
provided by this snapshot. Consult `config.py` for the supported variable names.

Database and vector migration scripts and the firewall script are included as
source only. They were not executed during publication. This import does not
resolve the external Oagnet semantic scope issue recorded in the Phase 0C closure.

## Verification

This is the frozen import evidence for commit `813073ff3d34a5935e98c5433207ab38d55ccf6b`.
The verifier checks that original snapshot and is expected to report drift after
later source changes. Current scope changes and their verification are documented
in [the scope root-cause report](../phase0c_root_cause/closure_report.md).

From the repository root at the original import commit:

```powershell
python docs/oagnet_import/verify_snapshot.py
```

The verifier checks all 49 published hashes, compiles all 41 Python files without
executing their application code, and runs the capacity control and Redis job store
unit tests with network access disabled. The job store tests use `FakeRedis`.
Dotenv loading and pytest plugin discovery are disabled for this run.

Recorded result in [`verification.json`](verification.json): **12 passed, 0 failed,
0 collection errors, 0 network attempts** on Python 3.12.8 / pytest 8.4.2. No real
model calls or external production writes occurred. This validates the snapshot
and selected offline behavior; the full integration suite was not run.
