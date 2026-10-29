# Legacy parser freeze policy

CURRENT_FACT: StructuredIntentOutput, SYSTEM_PROMPT, RuleBasedIntentClassifier regex, production qwen3.6-plus model configuration, temperature, response_format, API routing and Redis/MinIO integration are unchanged in this phase.

PROPOSAL: Subsequent changes to the legacy parser are limited to proven P0 defects, security and compatibility repairs. Architecture gaps must not be addressed by adding prompt rule 41/42 or another regex special case. New semantic capabilities belong in semantic_v2 SHADOW_ONLY until a separate benchmark and rollout decision.

CURRENT_FACT: Field/column lineage is represented and tested in V2. The current legacy metadata adapter accepts MetricRef only; a parser-only relaxation would not prove backend support. That observed limitation is recorded rather than masked by fabricated metrics or a new regex.

CURRENT_FACT: Existing legacy test source hashes are frozen. No test was removed, skipped, renamed or weakened. The comparison report separates original passes/failures from new tests.
