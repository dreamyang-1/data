# Reproduce the Phase 2.5.1 evidence

Run from `E:/YouoAgent/DataAnalysis_Agent` with the existing Python 3.12 environment. No additional dependencies are required.

```powershell
python tools/phase25_1/run_gate.py --output "$env:TEMP/phase25_1_recheck.json" -q --tb=short --continue-on-collection-errors
python -m tools.phase25_1.materialize
```

The test runner refuses to overwrite an existing gate file, disables dotenv loading and denies network access. The original baseline remains `baseline_test_gate.json`; final evidence is `final_verified_test_gate.json`. Use `compare_tests('final_verified_test_gate.json')` from `tools.phase25_1.closure_audit` to reproduce the node-by-node comparison.

Artifacts:

- `current_code_baseline_audit.md`, `source_inventory.json`: current source facts and full-file static coverage.
- `preexisting_workspace_drift.json`, `final_workspace_git_drift.json`: before/after SHA-256 evidence with preserved preexisting differences.
- `plan_axis_compatibility_matrix.csv`: generated payload/route/goal/backend combinations.
- `specs/semantic_v2/0.2.1/`: Pydantic-generated stage and state schemas.
- `contrast_cases.json`, `metamorphic_cases.json`, `long_conversations/`: declared contract fixtures with actual before/after states.
- `gold_v0_2_1.jsonl`: 200 PARTIAL legacy-intent rows; unlabeled fields are null.
- `dataset_ancestry_fixture.json`: frozen current-slice versus original-root answers.
- `contract_boundaries_and_rollback.md`: compatibility limits, proof requirements and rollback boundaries.
- `phase25_1_closure_report.md`: final disposition, remaining blockers and Git delivery record.

The Git checkout intentionally retains the seven preexisting workspace differences. The full suite was executed in the development workspace as requested; its legacy result must not be represented as a test run on an identical Git checkout.
