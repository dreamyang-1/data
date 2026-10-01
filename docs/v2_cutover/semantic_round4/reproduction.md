# Reproduction and boundaries

Public verification and focused tests require the existing project environment, no new dependencies:

```powershell
python docs/v2_cutover/semantic_round4/verify_round4.py
python -m pytest tests/test_v2_context_proposal_round4.py tests/test_v2_context_followup_critical_slice.py -q
```

The verifier invokes parent evidence verification; it does not replay models, execute SQL or read Private/Blind payloads. The complete Round4 focused-node outcomes, Critical160 and the 25 new nodes are in `test_delta.json`. Full Agent and cross-service suites were not rerun because production code did not change.

Optional controlled native experiment, into a new ignored directory:

```powershell
python tools/cutover/run_context_proposal_round4.py --mode controlled --output-directory .eval_private/round4-controlled-reproduction
```

The live entry point additionally requires explicit `--mode live --allow-model-calls`, the local frozen public Harness captures, catalog snapshot and already configured model credentials. It never accepts Blind or unpromoted Private as a corpus. It has a 15-call budget, retry0, per-call timeout60s and denies non-model network. Current code emits experimental Schema v1.1, so new results must not overwrite or be presented as exact reproduction of the stored v1 model requests. Raw v1 request/response bodies remain private and pinned by the public receipt hashes.

`context_proposal_native_diagnostic.py` uses the recorded v1 proposal/current parse and new bounded downstream model calls through the native RawTurnPlanner. It retains scope, current-evidence, Catalog and reducer checks; source observations are frozen. Its original run made 11 extra model requests. This is a diagnostic compatibility adapter with scoped monkeypatches, not a new production route or a deployment flag.

`summarize_context_proposal_round4.summarize(private_directory)` evaluates only existing records and corrects A's observer omission using its unchanged captured output. It neither calls a model nor changes frozen Gold labels. Schema rejection is excluded from valid semantic decisions and is still counted as a failed proposal attempt. Missing native axes stay unavailable. It cannot certify complete runtime safety or production non-regression.

All task summaries, raw captures, test logs and credentials stay outside Git. Only bounded identifiers, label checks, counts and hashes are published. Rollback is diagnostic-only: discard this review branch or revert its two commits on a new branch; V1 routing requires no rollback.
