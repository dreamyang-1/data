# Stale-test decisions and retained uncertainty

No old failing expectation was changed to reduce the failure count. Baseline categorization was reviewed: pytest-07/13/14/25 are downgraded from provisional STALE_TEST to UNKNOWN because current behavior alone does not prove the business contract. Their evidence needs remain in the inventory.

## ST-0B-01 — readiness is not turn reference ambiguity
Old: test_turn_admission.py::test_incomplete_unreferenced_turn_exposes_relation_clarification_state expected AMBIGUOUS_RELATION and needs_clarification=True solely because time was missing. Current requirement sections 18.15 and 21 explicitly separate referential completeness from execution readiness. New: STANDALONE_NEW_TOPIC, needs_clarification=False; no-inheritance assertions retained. This test was already passing before the contract correction; source change is disclosed rather than hidden in zero regression accounting.

## ST-0B-02 — make the pending-time fixture actually pending
Old: test_orchestrator.py::test_time_clarification_preserves_ranked_comparison_execution_contract entered normal handle with a stub requiring time, while the existing deterministic rule layer had already supplied a safe period. The new clarification gate correctly suppresses this artificial question. New fixture constructs a genuinely missing-time request and calls _request_clarification before the unchanged real handle(reply); application and filters are normalized by the existing rule classifier. Ranked comparison/time-preservation assertions are unchanged. Evidence: current requirements 20-21 and stable auditable-default tests.

## Retained old failing tests

### pytest-01
Node: `tests/test_clarification_flow.py::test_clarification_echoes_normalized_date_and_only_asks_for_metric`

Old expectation / actual assertion:
```text
E       AssertionError: assert {'allow_free_...ons': [], ...} == {'allow_free_...分析哪个指标？', ...}
E
E         Omitting 6 identical items, use -vv to show
E         Left contains 1 more item:
E         {'option_details': []}
E         Use -v to get more diff
```
Authoritative support: Phase 2.5.1 committed ClarificationItem additive option_details schema; existing model contract tests. Decision: expectation retained for separately reviewed fixture migration; no source business change justified by this assertion alone.

### pytest-02
Node: `tests/test_clarification_flow.py::test_natural_date_reply_completes_the_existing_metric_request`

Old expectation / actual assertion:
```text
E       AssertionError: assert [] == ['time_range']
E
E         Right contains one more item: 'time_range'
E         Use -v to get more diff
```
Authoritative support: Current requirement 20 permits safe deterministic defaults; stable test_intent default-time/all-history regressions and test_unqualified_sales_metric_uses_auditable_default_time_range. Decision: expectation retained for separately reviewed fixture migration; no source business change justified by this assertion alone.

### pytest-03
Node: `tests/test_clarification_quality.py::test_only_ambiguities_for_actually_missing_slots_are_kept_and_deduplicated`

Old expectation / actual assertion:
```text
E       AssertionError: assert [] == ['time_range']
E
E         Right contains one more item: 'time_range'
E         Use -v to get more diff
```
Authoritative support: Current requirement 20 permits safe deterministic defaults; stable test_intent default-time/all-history regressions and test_unqualified_sales_metric_uses_auditable_default_time_range. Decision: expectation retained for separately reviewed fixture migration; no source business change justified by this assertion alone.

### pytest-04
Node: `tests/test_clarification_quality.py::test_model_metric_must_be_grounded_in_the_users_words`

Old expectation / actual assertion:
```text
E       AssertionError: assert ['metric'] == ['metric', 'time_range']
E
E         Right contains one more item: 'time_range'
E         Use -v to get more diff
```
Authoritative support: Current requirement 20 permits safe deterministic defaults; stable test_intent default-time/all-history regressions and test_unqualified_sales_metric_uses_auditable_default_time_range. Decision: expectation retained for separately reviewed fixture migration; no source business change justified by this assertion alone.

### pytest-11
Node: `tests/test_pending_execution_transition.py::test_closed_form_pending_reply_skips_async_intent_model_path`

Old expectation / actual assertion:
```text
E       AssertionError: assert 'COMPLETED' == 'NEEDS_CLARIFICATION'
E
E         - NEEDS_CLARIFICATION
E         + COMPLETED
```
Authoritative support: Current requirement 20 permits safe deterministic defaults; stable test_intent default-time/all-history regressions and test_unqualified_sales_metric_uses_auditable_default_time_range. Decision: expectation retained for separately reviewed fixture migration; no source business change justified by this assertion alone.

### pytest-12
Node: `tests/test_pending_execution_transition.py::test_filled_slot_can_transition_to_asl_clarification_then_complete`

Old expectation / actual assertion:
```text
E       AssertionError: assert 'COMPLETED' == 'NEEDS_CLARIFICATION'
E
E         - NEEDS_CLARIFICATION
E         + COMPLETED
```
Authoritative support: Current requirement 20 permits safe deterministic defaults; stable test_intent default-time/all-history regressions and test_unqualified_sales_metric_uses_auditable_default_time_range. Decision: expectation retained for separately reviewed fixture migration; no source business change justified by this assertion alone.

### pytest-16
Node: `tests/test_runtime_regressions.py::test_relationship_wording_does_not_deduplicate_order_facts`

Old expectation / actual assertion:
```text
E       AssertionError: assert '| 订单号 | 商品 |' in '共查询到 2 条明细。\n\n| 订单号 | 商品名称 |\n| --- | --- |\n| A-1 | 甲 |\n| A-1 | 甲 |'
```
Authoritative support: Governed business display label 商品名称; stable business-table rendering regressions. Decision: expectation retained for separately reviewed fixture migration; no source business change justified by this assertion alone.

### pytest-17
Node: `tests/test_semantic_clarification_session_recovery.py::test_explicit_day_range_resolves_report_time_without_model_reinterpretation`

Old expectation / actual assertion:
```text
E       AssertionError: assert 'COMPLETED' == 'NEEDS_CLARIFICATION'
E
E         - NEEDS_CLARIFICATION
E         + COMPLETED
```
Authoritative support: Current requirement 20 permits safe deterministic defaults; stable test_intent default-time/all-history regressions and test_unqualified_sales_metric_uses_auditable_default_time_range. Decision: expectation retained for separately reviewed fixture migration; no source business change justified by this assertion alone.

### pytest-18
Node: `tests/test_semantic_clarification_session_recovery.py::test_three_turn_subject_clarification_recovers_pending_request[False]`

Old expectation / actual assertion:
```text
E       AssertionError: assert 'COMPLETED' == 'NEEDS_CLARIFICATION'
E
E         - NEEDS_CLARIFICATION
E         + COMPLETED
```
Authoritative support: Current requirement 20 permits safe deterministic defaults; stable test_intent default-time/all-history regressions and test_unqualified_sales_metric_uses_auditable_default_time_range. Decision: expectation retained for separately reviewed fixture migration; no source business change justified by this assertion alone.

### pytest-19
Node: `tests/test_semantic_clarification_session_recovery.py::test_three_turn_subject_clarification_recovers_pending_request[True]`

Old expectation / actual assertion:
```text
E       AssertionError: assert 'COMPLETED' == 'NEEDS_CLARIFICATION'
E
E         - NEEDS_CLARIFICATION
E         + COMPLETED
```
Authoritative support: Current requirement 20 permits safe deterministic defaults; stable test_intent default-time/all-history regressions and test_unqualified_sales_metric_uses_auditable_default_time_range. Decision: expectation retained for separately reviewed fixture migration; no source business change justified by this assertion alone.

### pytest-20
Node: `tests/test_semantic_clarification_session_recovery.py::test_semantic_clarification_keeps_explicit_interrupt_semantics[\u4f60\u597d-CHAT-COMPLETED]`

Old expectation / actual assertion:
```text
E       AssertionError: assert 'COMPLETED' == 'NEEDS_CLARIFICATION'
E
E         - NEEDS_CLARIFICATION
E         + COMPLETED
```
Authoritative support: Current requirement 20 permits safe deterministic defaults; stable test_intent default-time/all-history regressions and test_unqualified_sales_metric_uses_auditable_default_time_range. Decision: expectation retained for separately reviewed fixture migration; no source business change justified by this assertion alone.

### pytest-21
Node: `tests/test_semantic_clarification_session_recovery.py::test_semantic_clarification_keeps_explicit_interrupt_semantics[\u53d6\u6d88-REPORT_GENERATION-CANCELLED]`

Old expectation / actual assertion:
```text
E       AssertionError: assert 'COMPLETED' == 'NEEDS_CLARIFICATION'
E
E         - NEEDS_CLARIFICATION
E         + COMPLETED
```
Authoritative support: Current requirement 20 permits safe deterministic defaults; stable test_intent default-time/all-history regressions and test_unqualified_sales_metric_uses_auditable_default_time_range. Decision: expectation retained for separately reviewed fixture migration; no source business change justified by this assertion alone.

### pytest-22
Node: `tests/test_semantic_clarification_session_recovery.py::test_semantic_clarification_keeps_explicit_interrupt_semantics[\u6362\u4e2a\u95ee\u9898\uff0c\u67e5\u8be2\u672c\u6708\u9500\u552e\u989d-METRIC_QUERY-COMPLETED]`

Old expectation / actual assertion:
```text
E       AssertionError: assert 'COMPLETED' == 'NEEDS_CLARIFICATION'
E
E         - NEEDS_CLARIFICATION
E         + COMPLETED
```
Authoritative support: Current requirement 20 permits safe deterministic defaults; stable test_intent default-time/all-history regressions and test_unqualified_sales_metric_uses_auditable_default_time_range. Decision: expectation retained for separately reviewed fixture migration; no source business change justified by this assertion alone.

### pytest-23
Node: `tests/test_semantic_clarification_session_recovery.py::test_semantic_clarification_keeps_explicit_interrupt_semantics[\u67e5\u8be2\u6628\u5929\u8ba2\u5355\u660e\u7ec6\uff0c\u663e\u793a\u8ba2\u5355\u53f7\u548c\u91d1\u989d-DETAIL_QUERY-COMPLETED]`

Old expectation / actual assertion:
```text
E       AssertionError: assert 'COMPLETED' == 'NEEDS_CLARIFICATION'
E
E         - NEEDS_CLARIFICATION
E         + COMPLETED
```
Authoritative support: Current requirement 20 permits safe deterministic defaults; stable test_intent default-time/all-history regressions and test_unqualified_sales_metric_uses_auditable_default_time_range. Decision: expectation retained for separately reviewed fixture migration; no source business change justified by this assertion alone.

### pytest-27
Node: `tests/test_workflow.py::test_workflow_clarify_then_complete`

Old expectation / actual assertion:
```text
E       AssertionError: assert 'COMPLETED' == 'NEEDS_CLARIFICATION'
E
E         - NEEDS_CLARIFICATION
E         + COMPLETED
```
Authoritative support: Current requirement 20 permits safe deterministic defaults; stable test_intent default-time/all-history regressions and test_unqualified_sales_metric_uses_auditable_default_time_range. Decision: expectation retained for separately reviewed fixture migration; no source business change justified by this assertion alone.
