"""Rebuild schema and declared contract fixtures; no model or production service access."""
from __future__ import annotations

import csv
import json
from pathlib import Path

from app.semantic_v2.models import *
from app.semantic_v2.pipeline import *
from app.semantic_v2.registries import PayloadContractRegistry, SlotDefinitionRegistry
from app.semantic_v2.state_machine import ConversationState
from tools.phase25_1.scenarios import CONTRAST_CASES, METAMORPHIC_CASES, contrast_semantics, long_conversation

ROOT = Path(__file__).resolve().parents[2]
DOCS = ROOT / 'docs/phase25_1'


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False) + '\n', encoding='utf-8')


def materialize():
    DOCS.mkdir(parents=True, exist_ok=True)
    for cls in (CurrentTurnSemanticParse, CandidateSelectionDecision, CurrentTurnParseResult, TurnResolutionResult, LogicalPlan, ExecutablePlan, ExecutionAttemptRecord, TaskSemanticState, ConversationState, PlanEnvelope):
        write_json(ROOT / 'specs/semantic_v2/0.2.1' / (cls.__name__ + '.schema.json'), {'$schema': 'https://json-schema.org/draft/2020-12/schema', **cls.model_json_schema()})
    with (DOCS / 'plan_axis_compatibility_matrix.csv').open('w', encoding='utf-8', newline='') as stream:
        writer = csv.writer(stream)
        writer.writerow(['payload_type','resolved_query_shape','service_route','required_analysis_goals','allowed_analysis_goals','execution_backend','legacy_adapter_policy'])
        for definition in PayloadContractRegistry.definitions.values():
            writer.writerow([definition.payload_type, definition.resolved_query_shape, '|'.join(definition.allowed_service_routes),
                             '|'.join(sorted(definition.required_analysis_goals)), '|'.join(sorted(definition.allowed_analysis_goals)),
                             definition.execution_backend, definition.legacy_adapter_policy])
    contrast = [dict(c, left_contract=contrast_semantics(c)[0].model_dump(mode='json'), right_contract=contrast_semantics(c)[1].model_dump(mode='json'),
                     ground_truth_scope=['DECLARED_CONTRACT_DIFFERENCE'], annotation_status='PARTIAL',
                     unlabeled_fields=['current_turn_model_parse','production_catalog_binding'], current_turn_model_parse=None, production_catalog_binding=None)
                for c in CONTRAST_CASES]
    write_json(DOCS / 'contrast_cases.json', contrast)
    write_json(DOCS / 'metamorphic_cases.json', METAMORPHIC_CASES)
    for group in ('hospital_sales','dealer_orders','product_returns','warehouse_stock','department_purchases'):
        write_json(DOCS / 'long_conversations' / (group + '.json'), long_conversation(group))
    old_gold = [json.loads(line) for line in (ROOT / 'docs/phase25/gold_v2_seed.jsonl').read_text(encoding='utf-8').splitlines() if line.strip()]
    for row in old_gold:
        missing = [key for key, value in row.items() if value == 'NEEDS_BUSINESS_REVIEW' or value == ['NEEDS_BUSINESS_REVIEW']]
        for key in missing:
            row[key] = None
        row['unlabeled_fields'] = missing
    (DOCS / 'gold_v0_2_1.jsonl').write_text(''.join(json.dumps(row, ensure_ascii=False)+'\n' for row in old_gold), encoding='utf-8')
    write_json(DOCS / 'dataset_ancestry_fixture.json', {
        'ground_truth_scope': ['DECLARED_FROZEN_DATASET'], 'annotation_status': 'COMPLETE',
        'A0': {'rows': list(range(1,23)), 'root_dataset_id':'A0','parent_dataset_ids':[]},
        'A1': {'rows': list(range(1,6)), 'root_dataset_id':'A0','parent_dataset_ids':['A0'], 'operation': {'operation_type':'LIMIT','limit':5,'preserve_existing_order':True}},
        'turns': [{'text':'展示前10条','source_dataset_id':'A1','expected_rows':list(range(1,6))},
                  {'text':'最开始22条里的前10条','source_dataset_id':'A0','expected_rows':list(range(1,11))}],
        'reference_resolver': 'FIXTURE_ONLY_NOT_PRODUCTION'})
    return dict(contrast=len(contrast), metamorphic=len(METAMORPHIC_CASES), long_conversations=5, gold_partial=len(old_gold))


if __name__ == '__main__':
    print(json.dumps(materialize()))
