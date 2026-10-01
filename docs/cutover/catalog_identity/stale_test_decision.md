# Evidence-based correction of a SQL predicate assertion

Test: `Oagnet/tests/test_exact_entity_value_resolution.py::test_exact_entity_attribute_value_resolution_allows_non_main_business_field`.

Old expectation: metadata SQL must **not** contain `a.semantic_model_id = m.id`.
The old implementation comment assumed attribute model IDs were redundant and
physical membership prevented expansion.

Current formal contract: the current upstream semantic model is the only model
authorized for retrieval. Identical physical column names do not make distinct
semantic model definitions interchangeable.

Higher-priority evidence:

1. The user's Phase 0C Scope Contract prohibits cross-model semantic/entity-value retrieval.
2. Java `SemanticModelServiceImpl.copyVersionData`, inspected lines 396–427,
   selects attributes by model, preserves entity IDs, creates new attribute UUIDs
   and assigns the target model. Attribute deletion also requires model scope.
3. Actual 81/[205] capture had eleven bindings to current-model attributes
   overwritten by other-model newer same-code attributes. Bounded observations
   and backend file hashes are retained in this folder.
4. New SQL execution tests reproduce foreign field acceptance and interference
   with main-field uniqueness when both columns exist physically in model 81.

Decision: `STALE_TEST` for that predicate assertion. Require the model constraint;
keep the original non-main field result and absence of a main-only restriction.
No other old expected result changed. This decision is based on the current
business contract and source evidence, not convenience or failure-count reduction.
