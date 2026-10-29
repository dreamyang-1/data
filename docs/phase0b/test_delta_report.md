# Phase 0B test delta

Baseline `e4d3bb59026d3b219c9268ac53f87dccf58f3c28`: **1696 passed / 32 failed / 0 collection errors**.
Final `docs/phase0b/final_verified_test_gate.json`: **1763 passed / 29 failed / 0 collection errors**.
Old pass → new fail: **0**. Old fail → pass: **3** (pytest-05, pytest-06, pytest-15).
New regression tests: **64** = 15 independent critical scenarios + 49 contrasts, metamorphic and real Legacy orchestration checks; all pass.
All 32 original failed nodeids ran independently twice: **64 process runs, 32 DETERMINISTIC**, no third run required. No flaky or environmental failure was inferred merely from calendar time.

Two old passing fixtures were updated with explicit current-contract evidence; their nodeids and behavior assertions remain. See stale_test_decisions.md. Old failing expectations remain unchanged, including unresolved policy differences.
Business clock is fixed through the existing classifier.date monkeypatch seam at 2026-09-07. No dependency installation. Offline runner blocks DNS/connect operations, disables .env loading and runtime collection; real external model calls = 0, production external writes = 0.
Trace audit: `{"public_clarification_responses": 23, "with_reason_trace": 23, "untraced": 0, "unsafe_asks": 0}`. This counts public clarification responses observed across the full offline suite (including idempotent cache paths), not live traffic or all possible catalog combinations.
Intermediate debugging runs are local generated artifacts; they are not the final Gate or the original failure universe.
