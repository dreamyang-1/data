# Offline reproduction

Run from the Agent root, with the existing Python environment. No dependency installation is required.

```powershell
python docs/v2_cutover/semantic_round3/verify_round3.py
python -m pytest tests/test_v2_context_round3.py tests/test_v2_context_followup_critical_slice.py -q
```

Public verification reads no private payloads, credentials or Blind Holdout. Frozen replay additionally needs the original **local** Harness captures, pinned catalog/source-observation snapshots and the two promoted private records. Their hashes are bound by the parent manifests and this round's replay receipt; they are deliberately absent from Git.

Use a fresh ignored output directory for each reproduction:

```powershell
python tools/cutover/context_round3_replay.py --output-directory .eval_private/round3-reproduce-recorded
python tools/cutover/context_round3_controls.py --output-directory .eval_private/round3-reproduce-controls
```

The first entry point verifies capture pins, replays actual RawTurnPlanner calls and performs the recorded relation/delta interventions. Only PV81-002 and PV81-003 are loaded from Private; their prior promotion is recorded in this directory. The second uses the existing controlled catalog and context cases. Both write detailed evidence locally; neither calls a real model or writes production state. The rule arm pins the existing classifier date to the corresponding frozen clock. An existing output directory is rejected to protect evidence.

These scripts reproduce diagnostic observations, not new Gold scores. A/B hold downstream recorded edits fixed after a possible context change. The report's comparisons therefore use per-turn fixed preconditions and explicit observed label axes. They must not be described as complete V1/V2 orchestration or independent LLM-only evaluation. The production Prompt/Schema and frozen evaluator remain unchanged.
