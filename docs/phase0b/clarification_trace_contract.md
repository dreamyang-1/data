# ClarificationDecisionTrace / Legacy v1

Every final Legacy clarification passes `_ensure_clarification_trace`, including cache and root composite responses. `_request_clarification` evaluates the gate before writing Pending. Root DAG mapping uses the existing versioned Pending write, without introducing a new concurrency architecture.

Required fields: conversation_id, message_id, source_stage, reason_type, blocking_slot, expected_answer_type, candidate_ids, already_asked, base_task_reference, pending_reference, evidence_codes, is_user_ambiguity, system_repair_possible. Additional gate fields: safe_default_available and decision (ASK/SUPPRESS).

ASK requires a concrete missing business choice or at least two semantic choices, no safe default, no deterministic repair, and an unasked Pending key. Missing fields without a governed default is CATALOG_GOVERNANCE_GAP; backend ambiguity without options is SYSTEM_FAILURE. Supplied metric/lineage target cannot become a repeated missing metric. Duplicate Pending questions are suppressed while state remains available. Invalid shared-report time replies keep the original question without reissuing it. Root task mapping is asked once.

Reasons: MISSING_USER_SLOT, USER_SEMANTIC_AMBIGUITY, USER_REFERENCE_AMBIGUITY, CATALOG_GOVERNANCE_GAP, SYSTEM_FAILURE, REPEATED_QUESTION. Source stages use the actual guard boundary (INTENT_ASL_CONTRACT, OAGNET_ASL_GENERATION, SQL_TRANSLATOR, SESSION_STATE). These identify why the question was sent; the separate failure inventory identifies the earlier root-cause divergence.

Trace stores no raw utterance, business SQL, dataset rows or candidate display labels. Candidate labels are represented by opaque digests; task IDs are existing opaque IDs. Pending retains only hashed question keys in addition to its existing TTL-scoped request state. The final response fills real conversation/message IDs. No new long-term trace store is introduced. Diagnostic `trace-only` placeholders are never trusted execution identities.

Tests: final_clarification_has_scoped_reason, same_pending_question_is_suppressed, backend_failure_cannot_be_rephrased, real_candidate_choice_is_allowed, root_dag_mapping_question_is_not_repeated, clarification_history_is_isolated_by_trusted_scope, question_limit_does_not_mark_unshown_question_as_already_asked. Whole-suite public response coverage is recorded in final_verified_test_gate.json.
