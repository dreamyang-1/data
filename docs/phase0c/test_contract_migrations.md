# Test-input and expectation decisions

Authority: the user's current Phase 0C contract takes precedence over older permissive API fixtures. No production classifier/prompt/slot semantics were changed to make fixtures pass.

`test_input_migration.json`, `state_fixture_migration.json`, and `candidate_fixture_migration.json` record the initial mechanical migrations. Their counts describe those operations, not the final patch size. The initial generic dictionary migration also touched a collector fixture; that unrelated change was reverted before the final run.

- API and ChatRequest fixtures now provide an explicit model. Business API fixtures provide a service test token and stable state headers. The raw client is used for negative authentication tests; authorization dependencies are not mocked out.
- Seeded Pending/task/last-request and DAG fixtures now identify the same backend model as the current request. Dataset fixtures carry model/domain and scope provenance, so truncation and composition tests continue to exercise their original behavior.
- Entity/display fake candidates now declare their fixture domain. Current production candidates outside the grant or lacking required explicit-domain provenance are rejected.
- Existing semantic/turn/slot expectations remain in place. The following expectation changes are required by the new contract:

| Test | Old expectation | Current expectation and reason |
| --- | --- | --- |
| test_development_can_temporarily_use_fallback_identity | Anonymous development request succeeds | 401 STATE_NAMESPACE_REQUIRED; enabling a development flag cannot share users' state |
| test_application_header_is_ignored_and_body_scope_is_used | Conflicting application header succeeds | 403 STATE_NAMESPACE_MISMATCH; trusted namespace must be coherent |
| test_legacy_application_header_setting_no_longer_requires_header | Required-header setting is ignored | 401 STATE_APPLICATION_REQUIRED; enforce the configured state boundary |
| test_auto_domain_still_searches_across_the_semantic_model | Search receives null domain list | Search receives normalized []; still model-wide for the supplied model |
| test_dag_resume_fails_closed_when_semantic_scope_changes | Message suggests keeping the old scope | Message asks to resubmit within the current scope; do not encourage scope expansion |

Full-process runs initially completed, but later host memory exhaustion caused MemoryError/internal aborts, including an unsuccessful Git archive. Aborted runs are not gates. The final complete regression is run in serial module batches using the same offline runner and fixed business clock. No tests are deselected; the exact partition, node IDs, outcomes and batch hashes are retained. This proves the partitioned suite, not single-process order/isolation equivalence. Original failure details remain in the preceding evidence; final batches suppress expensive traceback rendering to stay within available memory.
