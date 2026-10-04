"""Empty-context lightweight current-turn contract; full contract everywhere else.

Recorded model responses test the branch gate and contract shape, not model
accuracy. Pending resume, user option confirmation and historical follow-ups
must keep the complete frozen contract.
"""
import json

import pytest

from app.semantic_v2.recognition import (LIGHTWEIGHT_CURRENT_TURN_PROMPT,
    BUSINESS_SEMANTIC_EXTRACTION_RULES, PARSE_PROMPT,
    SURFACE_ONLY_EXTRACTION_PROMPT)
from test_v2_raw_turn_recognition import (planner, metric_step, parse, request,
    IDENTITY, turns)
from test_v2_pending_recognition import ask, catalog


SCHEMA_MARKER = '\nJSON Schema:\n'


def split_instruction_and_schema(body):
    system = body['messages'][0]['content']
    instruction, schema_json = system.split(SCHEMA_MARKER, 1)
    return instruction, json.loads(schema_json)


@pytest.mark.asyncio
async def test_empty_state_still_calls_model_with_lightweight_contract(catalog):
    steps = [metric_step('销售额')]
    engine, transport = planner(catalog, steps)
    results = await turns(engine, steps)

    # The model is still invoked for fine-grained extraction; the lightweight
    # branch is a contract change, not a regex bypass.
    assert len(transport.calls) >= 1
    instruction, schema = split_instruction_and_schema(transport.calls[0])
    assert instruction == LIGHTWEIGHT_CURRENT_TURN_PROMPT
    # History-selection clauses that cannot apply in an empty conversation are gone.
    assert 'scope-checked summary' not in instruction
    assert PARSE_PROMPT != instruction
    proposal = schema['$defs']['ContextProposal']
    assert proposal['properties']['target_task_id'] == {'type': 'null'}
    assert proposal['properties']['task_version'] == {'type': 'null'}
    assert proposal['properties']['pending_id'] == {'type': 'null'}
    assert len(proposal['allOf']) == 2

    # The completed span/relation remain valid model output.
    result = results[0]
    assert result.context_trace['FINAL_STATUS'] == 'ACCEPTED'
    assert result.context_trace['FINAL_RELATION'] == 'NEW_TASK'
    mention = result.parse.mentions[0]
    assert mention.surface == '销售额'

    # Record branch input sizes (character counts, not timed seconds).
    full_instruction_size = len(PARSE_PROMPT)
    lightweight_size = len(instruction) + len(json.dumps(schema, ensure_ascii=False))
    print(f'lightweight instruction+schema chars={lightweight_size}')
    print(f'full instruction chars alone={full_instruction_size}')
    assert lightweight_size > 0


@pytest.mark.asyncio
async def test_second_turn_with_history_keeps_full_contract(catalog):
    steps = [metric_step('销售额'), metric_step('再看销售额', follow=True)]
    engine, transport = planner(catalog, steps)
    await turns(engine, steps)

    # Turn two has stored tasks; the full frozen contract must be used.
    second_turn_call = transport.calls[2]
    instruction, schema = split_instruction_and_schema(second_turn_call)
    assert instruction.startswith(PARSE_PROMPT)
    assert 'scope-checked summary' in instruction
    assert instruction != LIGHTWEIGHT_CURRENT_TURN_PROMPT
    proposal = schema['$defs']['ContextProposal']
    assert proposal['properties']['target_task_id'] != {'type': 'null'}
    context = json.loads(second_turn_call['messages'][1]['content'])
    assert context['task_context']['candidate_tasks']


@pytest.mark.asyncio
async def test_deferred_empty_context_uses_surface_only_extraction_prompt(catalog):
    steps = [metric_step('销售额')]
    engine, transport = planner(catalog, steps)
    engine.defer_new_task_binding = True
    results = await turns(engine, steps)

    instruction, schema = split_instruction_and_schema(transport.calls[0])
    # The deferred-binding path delegates catalog matching to ASL, so it uses
    # the short surface-only extraction contract, not the long completion
    # prompt and not the full lightweight contract.
    assert instruction.startswith(SURFACE_ONLY_EXTRACTION_PROMPT)
    assert '对下面的完整问题进行细粒度结构化提取' not in instruction
    assert 'scope-checked summary' not in instruction
    assert BUSINESS_SEMANTIC_EXTRACTION_RULES in instruction
    assert '不得创造目录中不存在的业务' in instruction
    assert '筛选值不是分组维度' in instruction
    assert '结构化\n提取必须以它为唯一业务内容来源' in instruction
    proposal = schema['$defs']['ContextProposal']
    assert proposal['properties']['target_task_id'] == {'type': 'null'}
    # Role labels are free-form: the model names what each span is (city, time,
    # product name, ...) instead of being squeezed into the governed enum.
    roles = schema['$defs']['Mention']['properties']['candidate_roles']
    assert roles['items'] == {'type': 'string', 'minLength': 1, 'maxLength': 40}
    assert 'enum' not in roles['items']
    result = results[0]
    assert result.context_trace['FINAL_STATUS'] == 'ACCEPTED'
    assert result.context_trace['FINAL_RELATION'] == 'NEW_TASK'


@pytest.mark.asyncio
async def test_free_form_role_labels_pass_through_extraction_items():
    from app.semantic_v2.pipeline import CurrentTurnSemanticParse
    from app.semantic_v2.recognition import current_turn_extraction_items

    parse = CurrentTurnSemanticParse.model_validate({
        'mentions': [
            {'mention_id': 'm0', 'surface': '南京', 'normalized_surface': '南京',
             'start_char': 0, 'end_char': 2, 'candidate_roles': ['城市'],
             'source_turn_id': 't1'},
            {'mention_id': 'm1', 'surface': '费森尤斯', 'normalized_surface': '费森尤斯',
             'start_char': 2, 'end_char': 6, 'candidate_roles': ['品牌', '厂家'],
             'source_turn_id': 't1'},
            {'mention_id': 'm2', 'surface': '销售额', 'normalized_surface': '销售额',
             'start_char': 6, 'end_char': 9, 'candidate_roles': ['MEASURE'],
             'source_turn_id': 't1'},
        ],
    })
    items = current_turn_extraction_items(parse)
    assert [item['surface'] for item in items] == ['南京', '费森尤斯', '销售额']
    assert items[0]['labels'] == ('城市',)
    assert items[1]['labels'] == ('品牌', '厂家')
    # Governed enum labels still map to Chinese display text.
    assert items[2]['labels'] == ('指标',)


@pytest.mark.asyncio
async def test_pending_resume_keeps_full_contract_not_lightweight(catalog):
    previous, _ = await ask(catalog)
    option = previous.decision.options[0]
    # Replay the exact recorded resume path while keeping the transport so the
    # actual model request can be inspected.
    text = option.option_id
    step = (text, parse(text, (), follow=True), {})
    engine, transport = planner(catalog, [step])
    result = await engine.run(request(question=text, message_id='answer'), IDENTITY,
        state=previous.next_state, pending=previous.pending_state)

    # Exactly one model call answers the Pending, and it must use the full
    # frozen contract with the offered pending identity, never the lightweight
    # branch.
    assert len(transport.calls) == 1
    instruction, schema = split_instruction_and_schema(transport.calls[0])
    assert instruction.startswith(PARSE_PROMPT)
    assert 'scope-checked summary' in instruction
    assert BUSINESS_SEMANTIC_EXTRACTION_RULES in instruction
    proposal = schema['$defs']['ContextProposal']
    assert proposal['properties']['pending_id'] != {'type': 'null'}
    assert result.resolution['dialogue_act'] == 'ANSWER_CLARIFICATION'
