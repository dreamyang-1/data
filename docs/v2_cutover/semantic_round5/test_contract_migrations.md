# Test contract migrations

Authority: Round 5 requires joint first-stage proposal, no soft-rule veto, only selected-task draft context, and explicit ambiguity/unresolved termination. No Gold labels, frozen capture content or Legacy business assertions were changed.

| Existing expectation / fixture | Round 5 change and retained protection |
|---|---|
| Mock transport supplied old current-only parse | `context_fixture_contract.py` translates test-author flags into scripted joint outputs; explicit authored proposals take precedence. This helper is test-only, not a semantic classifier. Real old captures use a separately declared migration. |
| Historical second-stage context contains two tasks | It now contains the single validated target. The first stage sees both candidates independently of parse. The old exact target assertion is retained. |
| Wrong-task subtree test reads the second currently offered task | It now obtains a genuinely existing foreign handle from the preceding native TaskState, then injects it. `V2_FILTER_TARGET_NOT_CURRENT_TASK` remains required. |
| Unmatched Pending/orphan reference rejected by old relation gate | Same refusal/no wrong task/no repeated question, now explicit `V2_CONTEXT_UNRESOLVED`; no success expectation was substituted. |
| Round 3 resolver monkeypatch changes Raw production | Its original diagnostic assertion now directly calls the legacy resolver seam. New joint production override controls separately prove actual Raw behavior. |
| Exact Pending option beats a NEW_TASK act unless shift is also set | An authored joint NEW_TASK wins regardless of old shift flags; no inheritance and two calls are asserted. Counter-control explicitly proposes Pending answer despite old NEW flag and retains one call. |
| Handwritten typed Pending mock omitted proposal | It now supplies the offered Pending/task/version in the required joint output; corruption checks stay. |
| Single malformed-mentions fixture | Add otherwise valid joint context output so the same malformed-mentions diagnostic stays isolated. Missing-proposal rejection has its own new test. |

Three initial failures revealed an actual wiring issue: requiring a separate Pending resume receipt before candidate discovery blocked otherwise legal NEW_TASK. Production now uses already scoped Conversation metadata for exposure, while answer execution still requires the resume proof. Other initial failures were fixture/observation/version migration issues or failures in newly authored tests; no assertion was loosened to excuse a semantic regression.

Old reports remain frozen historical evidence; their verifiers passed at baseline before production changes. Their source pins are not claimed to describe Round 5. Evaluator scoring/equivalence functions are unchanged; instrumentation additionally observes the actual proposal-resolution seam. The old 15 targets replay only through an explicit recorded-signal protocol migration; 13 Private captures are machine-replayed without semantic inspection or tuning, 11 of them previously unpromoted. Blind remains unopened.
